# camera_watcher

Watches RTSP cameras, detects motion, and saves motion clips (including a
pre/post buffer) to disk in size-capped chunks with overlap between them.

One always-on process -- the **fleet supervisor** -- runs the whole thing:
a web UI that's reachable the moment the process starts, regardless of how
many cameras are configured or how badly any single one of them is broken,
and that lets you add/edit/remove cameras, their credentials, fleet-wide
retention, and the optional clip classifier's config, all from the browser.
There's no hand-editing YAML files or enabling systemd units per camera --
see "Configuring a camera" below.

This segment only handles capture, motion-triggered recording, and
retention. Analyzing saved clips for people/other activity is a later stage
of the larger project (see "Clip classifier (tier 1)" below).

## How it works

Everything below describes one camera's own capture/recording pipeline --
one instance of `CameraPipeline` runs per configured camera, all owned and
supervised inside the single fleet process (see `camera_watcher/fleet.py`).
Each camera is read via **two independent RTSP connections**:

- **Main stream** (`camera.main_path`, full resolution): never decoded.
  ffmpeg stream-copies (`-c copy`) it straight into short, disk-resident
  segments on a rolling basis (`recording.cache_dir`, `recording.segment_seconds`)
  -- see `camera_watcher/segment_cache.py`. This is what actually gets
  recorded, so a recorded clip's codec/resolution/bitrate exactly match
  whatever the camera sent, at effectively zero CPU cost regardless of
  resolution or bitrate.
- **Sub stream** (`camera.sub_path`, lower resolution): decoded via OpenCV,
  for motion detection, the live preview, and the mask editor snapshot.

On motion, `camera_watcher/recorder.py` opens a time window starting
`pre_buffer_seconds` before the trigger, keeps it open through a
`post_buffer_seconds` cooldown after the last detected motion (so flaky
detection doesn't fragment one event into many clips), and force-splits
anything longer than `max_chunk_seconds` (carrying `overlap_seconds` into
the next chunk so nothing is lost across the cut). Once a window closes, a
background assembler thread stitches the relevant cached segments into the
final clip via ffmpeg's concat demuxer (`camera_watcher/assemble.py`) --
also `-c copy`, so assembly is pure stream-copy too, never touching frame
content. Every clip is assembled under a temporary name and atomically
renamed to its final, timestamped name (`<camera>_<YYYYMMDD>_<HHMMSS>.mp4`)
only once fully written.

A separate pruning sweep keeps the segment cache trimmed to roughly
`pre_buffer_seconds` of lookback, and never deletes a segment that's part of
an open event's window or an assembly job that hasn't finished yet.

Retention is not part of any one camera's pipeline -- it's a single,
fleet-wide background thread in the supervisor process, sweeping every
camera's clips together against one shared budget -- see "Retention"
below. A never-decaying motion heatmap plus each clip's own metadata JSON
(`bounding_box`, among other fields) help spot zones that need a new or
larger ignore zone -- see "Tuning ignore zones" below.

### Camera setting that matters: I-frame interval

ffmpeg's segment muxer can only cut a new segment file at a keyframe, so the
camera's own I-frame (keyframe) interval sets a floor on how precisely
`recording.segment_seconds` is actually honored -- a long I-frame interval
means longer, less predictable segments, and a clip's assembled start can be
padded with several extra seconds of pre-roll it didn't ask for (harmless,
just wasteful). **Set the camera's I-frame interval to match its frame
rate** (i.e. one keyframe per second) so segment boundaries land close to
where you configured them. On Reolink cameras this is under the stream's
Encode settings ("I-Frame Interval"); set it equal to the stream's frame
rate (e.g. `15` at 15fps).

## Threading model

One process, many threads -- every camera's pipeline, plus retention, plus
the web UI, all run inside the single fleet supervisor. Every stage runs on
its own thread so a slow one can never stall another, and the web UI stays
responsive no matter what any camera is doing:

- **Sub-stream capture thread** (one per camera) only reads frames off the
  RTSP socket and appends them to that camera's shared frame buffer (live
  preview/snapshot source) -- it never touches disk.
- **Passthrough recorder** (`segment_cache.py`, one per camera): a
  supervised ffmpeg subprocess stream-copying the main stream into cached
  segments, with its own restart-with-backoff and stall detection (no new
  segment for too long while the process is still alive kills and restarts
  it).
- **Processing thread** (one per camera) pulls sub-stream frames off a
  bounded queue and does the motion detection work, decoupled from capture
  so a slow frame never backs up RTSP reads. If processing falls behind,
  the queue sheds (drops) the oldest-pending frames rather than growing
  without bound or blocking capture; a camera's `status` reports
  `dropped_frames` if this happens.
- **Clip assembler thread** (one per camera): motion-triggered ffmpeg
  concat calls run here, never on the frame-processing thread, so
  assembling a clip can never stall live motion detection.
- **Cache pruner** and **heatmap persistence** (one pair per camera) each
  run on their own timer thread.
- **Retention scheduler**: exactly one, fleet-wide background thread (not
  per camera), sweeping every camera's clips together on a schedule -- see
  "Retention" below.
- **Web UI** (Flask) runs on the main thread with a threaded WSGI server,
  and only ever touches each camera's shared, thread-safe frame buffer and
  config -- never a camera connection directly -- so the UI keeps
  responding immediately for every route, and every other camera, even
  while one camera is disconnected, reconnecting, mid-recording, or failed
  to start at all (see "Configuring a camera" below for what that last one
  looks like from the UI).

## Install

This repo is public, so it can be cloned anonymously over plain HTTPS -- no
GitHub account, login, or token needed. This work is currently on the
`claude/rtsp-motion-detection-z3819y` branch (not yet merged to `main`), so
check that branch out directly with `-b`. Pick the directory you want it in
and clone straight into it:

```bash
git clone -b claude/rtsp-motion-detection-z3819y https://github.com/clams2121/camera_watcher.git camera_watcher
cd camera_watcher
```

(`git clone -b <branch> <url> <directory>` checks out that branch instead
of the repo's default, and names the destination directory explicitly --
drop the trailing directory argument to use the repo name by default.) If
your network requires an HTTPS proxy, set `HTTPS_PROXY`/`https_proxy`
before cloning -- no other configuration is needed for a plain,
unauthenticated clone.

If you already have a clone of `main` and just want to pull this branch
into it instead of cloning fresh:

```bash
git fetch origin claude/rtsp-motion-detection-z3819y
git checkout claude/rtsp-motion-detection-z3819y
```

Once this branch merges into `main`, drop the `-b`/fetch step above and
just clone or pull `main` as usual.

## Setup

On Debian/Ubuntu, the `venv` module is packaged separately from Python and
`python3 -m venv` fails with a "No module named venv" / "ensurepip is not
available" error until it's installed (substitute your actual `python3`
version if it isn't 3.12):

```bash
sudo apt install python3.12-venv
```

Then create the virtual environment as usual:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # add -dev.txt too if running tests
```

### Configuring a camera

Start the fleet supervisor, pointed at a config directory (created
automatically if it doesn't exist yet -- nothing needs to exist beforehand):

```bash
python -m camera_watcher.main --config-dir config
```

The first run prints a generated auth token once (also saved to
`config/fleet.secrets.yaml`) -- see Authentication below. Open
`http://<tailscale-ip>:8080` (see "Network binding: Tailscale only" below
for how to find that address, or whatever `web.port` you've set in Fleet
Settings), log in with that token, and use the **Add Camera** tab: name
(letters, digits, `_` and `-` only -- it ends up in clip filenames), host,
port, stream paths, transport, and credentials. Saving starts that
camera's capture/recording pipeline immediately -- there's no separate
"apply" or restart step, and no config file to hand-edit or `cp` first.

That's the whole workflow for every camera from here on: **Cameras** tab
to see the fleet (status badge per camera, click through to a camera's own
page), each camera's own page for its Settings/Ignore Mask/Live
Preview/Recordings tabs (same four tabs this project has always had, just
now reached via `/cameras/<name>` instead of its own port) plus a Remove
button, and **Fleet Settings** for the web bind and retention. See
"Authentication" and "Retention" below for those two tabs' own sections.

**A camera that fails to start (bad host, wrong credentials, whatever)
never takes anything else down with it.** Its status badge shows `error`
with the reason, right there in the Cameras list and at the top of its own
Settings tab -- fix the settings and save, and it retries immediately. The
UI itself, and every other camera, stay completely unaffected the whole
time; there's no scenario where a single misconfigured camera means you
need shell access to fix anything.

Every camera shares one `data_root` (clips, cache, heatmaps) -- set once,
fleet-wide, in `config/fleet.yaml` -- so retention (see below) always
sweeps everyone's clips from one place without any camera needing to agree
on a path individually.

The Recordings tab groups clips into 30-minute buckets aligned to :00/:30
(newest group first); clicking a clip streams it in the browser with
play/pause and ±10 second seek buttons (native scrubbing via the video's own
controls too). Each clip has its own "Delete" button, and each group header
has a "Delete all" button for clearing a whole half-hour at once -- both ask
for confirmation first. Neither waits on retention. The Ignore Mask tab
lists drawn shapes alongside the canvas -- click one to highlight it, then
"Delete selected shape" to remove just that one.

The header's "Stop Server" button (on the fleet dashboard) asks you to type
`quit` to confirm, then cleanly shuts the whole process down -- every
camera's capture/recording, the heatmap accumulators, and the retention
scheduler all stop/flush the same way as Ctrl+C does in the terminal. Under
systemd with `Restart=always` (the default in `deploy/camera-watcher.service`),
it comes right back up -- which is also how a saved `web.host`/`web.port`/
auth-token change actually takes effect; see Authentication and Fleet
Settings below.

Configuring more than a couple of cameras is exactly the same UI flow
regardless of count -- see `deploy/README.md` for running the supervisor
as a single systemd service (`camera-watcher.service`, restart-on-failure,
sandboxing/hardening) instead of a foreground process.

## Network binding: Tailscale only

`web.host` (Fleet Settings tab, or `config/fleet.yaml`) defaults to
`"tailscale"`, which resolves this host's Tailscale IPv4 address
(`tailscale ip -4`) at startup and binds the **one, fleet-wide** web server
only there -- never `0.0.0.0`, never a LAN/public interface. If Tailscale
isn't installed, isn't logged in, or returns something that doesn't look
like a Tailscale address (`100.64.0.0/10`), startup fails loud with a
plain-English message instead of silently binding somewhere broader --
this is the one thing in the whole fleet still allowed to stop the process
at startup (no individual camera's problems ever can; see "Configuring a
camera" above). Set `web.host` to an explicit literal address (e.g.
`127.0.0.1` for local testing) to bypass Tailscale entirely. Changing
either `web.host` or `web.port` from Fleet Settings takes effect on the
**next restart**, not immediately -- the UI says so right there, and "Stop
Server" (under `Restart=always`) is the button that applies it.

Served by [waitress](https://docs.pylonsproject.org/projects/waitress/), a
production-grade WSGI server -- not Flask's own development server.

## Authentication

The fleet supervisor has exactly one auth token for its one web UI, in
`config/fleet.secrets.yaml`. Unlike per-camera secrets, **you don't
generate or set this yourself** -- the first time the process runs with
none configured, it generates one automatically and prints it once:

```
======================================================================
No auth token was configured -- generated a new one.
Log in to the web UI with this token (also saved in .../fleet.secrets.yaml):

    <the token>

Change it any time from Fleet Settings once logged in.
======================================================================
```

This is what makes "the UI should always run" actually true from a cold
start -- there's no manual token-generation step blocking your first
login. Change it any time from the **Fleet Settings** tab's "Rotate auth
token" button, which generates and saves a new one immediately and shows
it to you exactly once (also only in `fleet.secrets.yaml` after that) --
note that the *running* process keeps accepting the old token until it
restarts, so your current session and any scripts using the old token
keep working until then; the UI says this explicitly next to the button.

A present-but-too-short (< 32 character) token -- only possible via a
hand-edited `fleet.secrets.yaml`, since the bootstrap never generates a
weak one -- still fails startup loud, the same way a missing
`camera.name` does; auto-generation is a fail-*safe*, not a policy that
silently waves away a genuinely broken config.

- **Browser**: visiting any page without a valid session redirects to
  `/login`, a minimal token-entry form. `POST /api/login` compares the
  submitted token against the configured one in constant time
  (`hmac.compare_digest`) and, on success, sets a signed session cookie
  (`HttpOnly`, `SameSite=Lax`) -- nothing else about the token is ever
  stored client-side. "Log out" (header button, or `POST /api/logout`)
  clears it.
- **Scripts/automation**: send `Authorization: Bearer <token>` instead --
  works on every `/api/*` route without ever touching cookies/sessions.
- Any unauthenticated `/api/*` request gets a `401 {"ok": false, "error":
  "unauthorized"}` JSON response; unauthenticated page loads redirect to
  `/login`.

## Updating

There's no web-UI "Update & Restart" button any more (it was
unauthenticated remote code execution by design -- anyone who could reach
the UI could make it pull and run arbitrary upstream code). Updates are now
an operator-run script, `update.sh`, at the repo root:

```bash
./update.sh              # fetch, fast-forward, reinstall deps, restart
                          # camera-watcher.service on this host
./update.sh --no-fetch    # skip fetch/merge -- just reinstall deps and restart
                          # (e.g. after updating the code some other way)
```

Deliberately conservative, the same way the old button used to be:

- **Refuses a dirty working tree outright** -- commit, stash, or discard
  first.
- **Fast-forward only** (`git fetch` + `git merge --ff-only`) -- never
  creates a merge commit or discards local history. If the branch has
  diverged from upstream, it stops without touching anything and prints the
  exact `git log` commands to look into why.
- **Only restarts the service after `pip install -r requirements.txt`
  succeeds.** A failed dependency install leaves whatever was already
  running under systemd alone, and prints a rollback recipe
  (`git reset --hard <commit-before-this-run>`) rather than leaving the
  fleet mid-update.
- Restarts `camera-watcher.service` -- one supervisor for every camera --
  and prints its post-restart status.
- Also detects `clip-classifier.service` (see below) if it's deployed on
  this host, installs its extra dependencies from
  `requirements-classifier.txt` alongside the main ones, and restarts it
  the same way -- with the same fail-loud rollback-recipe behavior if
  that dependency install fails.
- Skips the restart step entirely (with a message, not a failure) on a
  host with no `systemctl`, or with no `camera-watcher.service` (or
  `clip-classifier`) registered -- `update.sh` also works for
  `git clone`-only dev checkouts that were never deployed as systemd
  services.

`shellcheck update.sh` is clean; see `tests/test_update_sh.py` for
end-to-end coverage of every path above against real throwaway git repos.

## Tuning ignore zones: drawing past the edge and the heatmap

Two features work together for diagnosing "this ignore zone isn't working"
(e.g. something at the edge of frame -- a flag, a tree branch -- keeps
triggering clips despite being outlined):

- **Draw past the image edge.** The Ignore Mask canvas is shown larger than
  the actual camera image, with a shaded margin around it (the dashed line
  marks the real frame boundary). Points placed in that margin are still
  saved and still apply -- useful for fully enclosing something that sways
  at or past the edge of the visible frame, which a shape confined to the
  visible image alone can't do.
- **Motion heatmap.** Check "Show motion heatmap" on the Ignore Mask tab to
  overlay where motion has fired, most-frequent spots most opaque. Unlike
  ordinary detection, this reflects *all* raw motion, including inside
  zones you've already marked ignored -- so it stays useful for confirming
  an existing ignore zone actually covers a spot's full range of motion.
  It **never decays or resets on its own** -- it's a running total from a
  persisted file (`motion.heatmap_path`, default `data/motion_heatmap.npy`)
  until you click "Reset heatmap."

For scripting a review of which regions keep triggering recordings over
time, use each clip's companion metadata JSON (`bounding_box`) rather than a
separate log -- see "Companion metadata for each clip" below.

## Bounding boxes: live preview only

`motion.draw_bounding_box` (off by default, toggleable from Settings) burns
a bright green box around detected motion into the **live preview stream
only**, padded `motion.box_padding_px` away from the contour so it doesn't
obscure the moving object itself. Recorded clips are passthrough copies of
whatever the camera's main stream sent (`-c copy`, never decoded) and are
never affected by this setting, at any point in the pipeline -- there's no
frame content in the recording path to draw on in the first place.

## Companion metadata for each clip

Every finalized clip gets a same-named `.json` file alongside it (e.g.
`camera1_20260117_140030.mp4` -> `camera1_20260117_140030.json`) -- the
**single source of truth** for everything known about that event (there's no
separate motion-events log any more). Written right after the video
finishes, deleted along with it (retention, single delete, and group delete
all remove both together):

```json
{
  "schema_version": 2,
  "event_id": "camera1_20260117_140030",
  "camera_id": "camera1",
  "start_time": "2026-01-17T14:00:28.512340-05:00",
  "end_time": "2026-01-17T14:00:42.881230-05:00",
  "duration_seconds": 14.437,
  "video_path": "/abs/path/to/data/clips/camera1_20260117_140030.mp4",
  "resolution": [2560, 1440],
  "bounding_box": [812, 340, 220, 180],
  "motion_confidence": {"mean_score": 812.4, "max_score": 2350.0, "motion_frame_ratio": 0.6667},
  "motion_time": 8.0,
  "detection_size": 0.1,
  "peak_motion_time": "2026-01-17T14:00:33.100120-05:00",
  "motion_timeline": [
    {"t": 0, "score": 0.0, "motion_detected": false},
    {"t": 1, "score": 1120.5, "motion_detected": true},
    {"t": 2, "score": 2350.0, "motion_detected": true}
  ],
  "sub_fps_measured": 9.8,
  "config_hash": "3f1a9c02e8b1",
  "mask_hash": "a90eddf6c123"
}
```

- **schema_version**: bumped whenever this shape changes; `rebuild_index.py` (below) and any future tooling should check it rather than assume.
- **event_id**: the clip's filename stem (camera name + timestamp combined) -- a stable, unique key for this event.
- **camera_id**: the configured camera name.
- **start_time** / **end_time**: ISO 8601 with UTC offset -- the recorder's logical event window (pre-buffer through post-buffer cooldown), not necessarily byte-identical to the assembled file's real span (see `duration_seconds`).
- **duration_seconds**: the *actual* assembled clip's duration, read back from the file itself via `ffprobe` -- segment boundaries are keyframe-aligned, so this can run a little longer than `end_time - start_time`.
- **video_path**: absolute path to the clip.
- **resolution**: `[width, height]`, read back from the assembled file -- always exactly what the camera's main stream sent, since recording is pure passthrough.
- **bounding_box**: `[x, y, w, h]`, the union of every motion detection's box during the event, or `null` if none were reported.
- **motion_confidence**: this detector uses background subtraction + contour area, not a neural net, so there's no built-in 0-1 probability -- instead: `mean_score`/`max_score` are the average/peak raw per-frame motion score (contour area) across frames where motion was actually detected, and `motion_frame_ratio` is the fraction of all frames in the clip that had motion detected at all, as a proxy for how consistent the detection was through the clip.
- **motion_time**: total seconds (not a fraction) where motion was detected, summed across the live portion of the clip -- 4 decimal places.
- **detection_size**: the largest single detected contour's bounding-box area as a fraction of the frame (e.g. `0.1` = the biggest detection covered 10% of the image at its peak) -- 4 decimal places.
- **peak_motion_time**: ISO 8601 timestamp of the single highest-scoring frame, or `null` if there was none.
- **motion_timeline**: one entry per whole second of the event (not per-frame -- that could be thousands of entries for a long event), each with that second's peak score and whether any motion was detected in it.
- **sub_fps_measured**: the sub-stream's actual observed frame rate during this event -- a diagnostic, not a recording parameter (recording never uses an fps hint at all; it's pure stream-copy).
- **config_hash** / **mask_hash**: short hashes of the camera's settings/ignore-mask exactly as they were when this event's window opened -- lets you correlate "which config produced this clip" across a fleet, or spot that a clip landed right after a config change, without ever hashing credentials (`config_hash` is computed from `config/cameras/<name>.yaml` only, never `.secrets.yaml`).

### Rebuilding the clip index

`python -m camera_watcher.rebuild_index <clips_dir>` scans every `<clip>.json`
sidecar under a clips directory, cross-checks it against the matching `.mp4`
(logging a warning and skipping anything orphaned either direction -- a clip
with no metadata, or metadata with no clip), and writes a flat,
`start_time`-sorted `index.jsonl` (one metadata record per line) at
`<clips_dir>/index.jsonl` by default (`--index-path` to override). Useful
after copying/reorganizing clips onto a new machine, or before pointing any
later analysis stage at a directory for the first time.

### Missing dependencies

Before doing anything else, `python -m camera_watcher.main` checks that
OpenCV, NumPy, Flask, PyYAML, and waitress are all importable. If any are missing it
prints exactly which packages are missing and how to install them (`pip
install -r requirements.txt`), and exits, rather than failing with a raw
traceback partway through startup. `camera_watcher/retention.py` has no
third-party dependencies at all and can run standalone with just Python 3.

## Credentials -- please read

Camera credentials go in `config/cameras/<name>.secrets.yaml`, one per
camera, kept separate from that camera's own `config/cameras/<name>.yaml`
settings file so the two can't accidentally get mixed up; the fleet web
UI's own auth token lives in `config/fleet.secrets.yaml` the same way,
separate from `config/fleet.yaml`. All four are written entirely by the
app itself (the Add Camera / Settings / Fleet Settings forms, and the
auth-token bootstrap/rotation) -- see "Configuring a camera" and
"Authentication" above; there's no manual file to create or edit for
normal use. They're all gitignored (see "Local config, not checked into
git" below) -- none of them are ever meant to be committed. The web UI's
password field never echoes back a stored password -- it always displays
blank, and leaving it blank on save keeps the existing value.

Before committing, double check `git status` doesn't show anything under
`config/cameras/`, `config/fleet.yaml`, `config/fleet.secrets.yaml`,
`config/classifier.yaml`, or `data/`.

## Local config, not checked into git

Everything this app writes to disk on its own -- `config/fleet.yaml`,
`config/fleet.secrets.yaml`, `config/classifier.yaml`, every camera's
`config/cameras/<name>.yaml` / `<name>.secrets.yaml` / `<name>.mask.json`,
each camera's motion heatmap, and recorded clips (with their metadata
sidecars) under the shared `data_root` -- is gitignored. Only checked-in
*reference templates* live in git, useful for seeing the full shape of a
config file or for scripting/manual setups that bypass the UI on purpose:

| Tracked template (safe to commit) | What actually gets used at runtime |
| --- | --- |
| `config/camera.example.yaml` | `config/cameras/<name>.yaml` -- created by the Add Camera form; edited by each camera's own Settings tab |
| `config/camera.secrets.example.yaml` | `config/cameras/<name>.secrets.yaml` -- same forms, credentials fields |
| `config/classifier.example.yaml` | `config/classifier.yaml` -- created/edited by the fleet UI's Classifier tab |

Nothing under `config/cameras/<name>.mask.json` has a template at all -- a
camera starts with no ignore zones, and the file is created the first time
you draw and save a shape on that camera's Ignore Mask tab.

## Configuration reference

See `config/camera.example.yaml` for the full set of per-camera options
with inline comments (camera connection, motion sensitivity, recording
buffer/chunk/overlap timing); `config/classifier.example.yaml` for the
clip classifier's. Fleet-wide settings (web bind, retention) don't have a
separate template file to read -- `config/fleet.yaml` is created with
sensible defaults on first boot and every field is editable from the
Fleet Settings tab; see "Retention" below for what each of its fields
does.

## Retention

Retention is fleet-wide, not per camera, and runs as a single background
thread **inside the fleet supervisor process itself** -- see
`RetentionScheduler` in `camera_watcher/retention.py` -- sweeping every
camera's clips under the shared `data_root` on a schedule. There's no
separate timer process to deploy or configure.

Everything's editable live from the **Fleet Settings** tab: low/high/review
age windows, the shared size budget, and how often the sweep runs
(`interval_minutes`, default 60) -- saved changes apply on the *next*
sweep, no restart needed. The same tab has "Preview (dry run)" and "Run
now" buttons that call the same sweep on demand and show exactly what it
did (or would do) right there -- worth using once after changing any
setting, before trusting it to the schedule.

The same sweep logic is also still available as a standalone CLI, useful
for one-off/diagnostic runs against a clips directory copied elsewhere
(it has no third-party dependencies and runs with just Python 3):

```bash
python -m camera_watcher.retention --global <data_root>/clips \
    --low-max-age-hours 48 --high-max-age-days 30 --review-max-age-days 30 \
    --max-total-gb 500 --dry-run
```

`--global` sweeps every camera's subdirectory under `<data_root>/clips/`
against **one shared budget** -- the globally oldest, lowest-tier clips are
deleted first once the combined total exceeds `--max-total-gb`, regardless
of which camera they belong to, so one busy camera can't starve a quiet
one's clips out of shared disk. (Omit `--global` and point it at one
camera's own clips directory instead for the single-camera form.) Add
`--dry-run` to see exactly what a real run would do -- every clip that
would be removed (and, via the log lines above it, why: expired out of its
tier's window, or deleted under budget pressure) is computed and printed
with a `[dry-run] would remove:` prefix, without touching the filesystem at
all.

### Verdict-aware tiers

If `clip_classifier` (see below) is running against the same `data_root`,
retention reads its `<stem>.analysis.json` verdict -- and, for `review`
clips, any `<stem>.review.json` human decision -- to sort each clip into
one of three tiers via `classify_tier()`, each with its own age window:

| Tier | Which clips | Default window | Fleet Settings field / CLI flag |
| --- | --- | --- | --- |
| `low` | verdict `low` | 48 hours | Low-tier max age / `--low-max-age-hours` |
| `high` | verdict `high`, verdict `error`, no analysis sidecar yet (not-yet-classified), or verdict `review` with a `review.json` decision of `keep` | 30 days | High-tier max age / `--high-max-age-days` |
| `review` | verdict `review`, never reviewed | 30 days | Review-tier max age / `--review-max-age-days` |

A clip clip_classifier hasn't reached yet (or choked on, `verdict:
"error"`) is deliberately treated as `high`, not deleted early -- nothing
gets treated as disposable just for being unclassified. Any window can be
disabled with `0` (that tier's clips then only expire under the size
budget, if any).

Each window is checked first, unconditionally -- a clip past its own
tier's age is always removed regardless of the size budget. Only then, if
the fleet is still over `--max-total-gb`, does the size-budget phase run:
`low` tier clips are deleted oldest-first, then -- only once every `low`
clip is gone -- the oldest of whatever `high`/`review` clips remain,
**logged as a warning** each time, since that means storage pressure is
what took down a clip otherwise considered worth keeping, not its own
age or a human's decision.

A `review` clip that ages out of its window unreviewed is also logged as a
warning (not just deleted quietly) -- that's a clip a human was meant to
look at and never did.

Either phase, once a clip is actually deleted, removes its whole sidecar
family together: the recorder's `<stem>.json`, clip_classifier's
`<stem>.analysis.json`, and any `<stem>.review.json`.

See `deploy/` for `camera-watcher.service`, the one systemd unit that runs
this automatically as part of the fleet supervisor -- no separate timer.

## Troubleshooting: can't reach the web UI from another device

With the default `web.host: tailscale`, the app binds only its Tailscale
IPv4 address -- reachable from any other device on the same tailnet, at
`http://<tailscale-ip>:8080` (find the IP with `tailscale status` or
`tailscale ip -4` on the host machine itself), nothing further to configure.
If that doesn't load from a second device on the tailnet, work through
these in order:

1. **Confirm the app itself is healthy first, from the host machine:**
   `curl http://$(tailscale ip -4):8080/ -I` there (expect a `302` to
   `/login`, not a connection error). If that fails, the problem is the
   app/config, not networking -- check the terminal running
   `camera_watcher.main` for errors. If it succeeds, the app is fine and the
   rest of this list applies.
2. **Confirm both devices are actually on the same tailnet** and that
   Tailscale is connected on both (`tailscale status` on each).
3. **Check Tailscale ACLs** if your tailnet has custom access rules --
   they can block device-to-device traffic even within the same tailnet.
4. **Check the host machine's local firewall** allows inbound connections
   on the port from the `tailscale0` interface (e.g. `sudo ufw allow in on
   tailscale0 to any port 8080` on Ubuntu). This is the most common blocker
   and won't show up in the app's own logs at all -- the connection just
   times out.
5. **Set `web.host` explicitly** (Fleet Settings tab, or `config/fleet.yaml`
   directly if you can't reach the UI yet) if you deliberately want
   something other than Tailscale (e.g. `127.0.0.1` for local-machine-only
   testing) -- see "Network binding: Tailscale only" above. Remember this
   needs a restart to take effect.

## Clip classifier (tier 1)

`clip_classifier` is a separate, standalone process (its own top-level
package, own `requirements-classifier.txt`, own systemd unit) that watches
a camera_watcher fleet's shared `data_root` for finalized clips and writes
a verdict sidecar (`<stem>.analysis.json`) alongside each one -- it never
modifies the recorder or its own metadata sidecar (`<stem>.json`), only
ever adds files of its own.

```bash
pip install -r requirements-classifier.txt
```

Then save at least a backend choice from the fleet web UI's **Classifier**
tab -- this creates `config/classifier.yaml` for you, with `data_root`
already pointed at the same shared root every camera writes clips into
(see `config/classifier.example.yaml` for the full set of options if
you'd rather create/edit the file by hand instead). Then run it:

```bash
python -m clip_classifier.main --config config/classifier.yaml
```

This is under active development (see the roadmap) -- this section grows
with each piece as it lands. Right now: a startup backfill scan plus a live
`watchdog`-based watch for newly finalized clips (keyed off each clip's own
metadata sidecar appearing -- see `clip_classifier/watcher.py`'s docstring
for why that's a reliable "this clip is done" signal), feeding a bounded
work queue processed serially, oldest first, with a periodic safety-net
re-scan in case a filesystem event gets missed; frame sampling around
motion peaks (`clip_classifier/sampling.py`); and the detector backends
below. `config/classifier.example.yaml` documents every setting.

### Detector backends

`classifier.yaml`'s `backend` setting picks one:

- **`cpu`** (works everywhere): YOLOv8n via ONNX Runtime. The model file is
  never downloaded automatically -- fetch and verify it once, offline from
  the classifier service itself:

  ```bash
  python -m clip_classifier.fetch_model
  ```

  **This repo's own pinned checksum is currently blank** (see the big
  comment in `clip_classifier/fetch_model.py`) -- the environment this was
  built in couldn't reach the download URL to compute and verify one. Before
  relying on the CPU backend: from a network that *can* reach it, run
  `python -m clip_classifier.fetch_model --print-hash-only`, confirm you
  trust the source, and hard-code the printed SHA-256 as `MODEL_SHA256` in
  that file. Until that's filled in, `fetch_model.py` refuses to install
  anything as "the" model -- it fails loud rather than silently skipping
  verification.

- **`hailo`**: a Hailo-8L M.2 accelerator, if you have one. Requires all
  three of: the device node (`/dev/hailo0`), HailoRT's Python bindings
  (`hailo_platform`, from [Hailo's developer
  zone](https://hailo.ai/developer-zone/)) importable in this environment,
  and a YOLOv8-family HEF compiled for Hailo-8L (from the [Hailo Model
  Zoo](https://github.com/hailo-ai/hailo_model_zoo)) at `hailo.hef_path` --
  compile it **without** Hailo's built-in NMS postprocessing, so its raw
  output shape matches the CPU backend's and both share the exact same,
  already-tested decode logic (see `clip_classifier/detectors/hailo.py`'s
  docstring for why, and its validation caveat -- this backend's HailoRT
  integration was written without real Hailo-8L hardware available to test
  it against).

- **`auto`** (the default): uses Hailo if all three of the above are
  present, otherwise falls back to CPU -- loudly logged either way.
  `backend: hailo` explicitly, by contrast, never falls back -- it fails
  loud listing exactly what's missing.

Either way, detections come back as label + confidence + a normalized
`(x, y, w, h)` box, backend-agnostic -- see `clip_classifier/detector.py`.
Target classes for a "high" verdict: `person`, the vehicle classes (car,
truck, bus, motorcycle, bicycle), and the COCO animal classes -- see
`clip_classifier/labels.py`.

### The verdict sidecar (`<stem>.analysis.json`)

Written once per clip, atomically (temp-write-then-rename, same pattern as
the recorder's own metadata sidecar) -- its presence is what marks a clip
"already classified" (see `clip_classifier/watcher.py`). The classifier is
the sole writer of this file; it never touches the recorder's own
`<stem>.json`.

```json
{
  "event_id": "front-door_20260717_143052",
  "schema_version": 1,
  "verdict": "high",
  "labels": [
    {"label": "person", "confidence": 0.91, "box": [0.42, 0.31, 0.18, 0.44], "frame_offset": 8.0}
  ],
  "reason": "person>=0.5",
  "sampled_frame_offsets": [2.0, 8.0, 8.9, 12.0],
  "sampling_fallback": false,
  "backend": "cpu",
  "model": "yolov8n",
  "model_version": "3f1a9c02e8b1",
  "processed_at": "2026-07-17T14:31:05.331200+00:00",
  "processing_seconds": 1.94
}
```

- **verdict**: `high` (person/vehicle/animal, confident), `review` (nothing
  recognized, but something's there a human should look at), `low`
  (everything else), or `error` (couldn't be classified at all -- see
  below). Never left unset: something that can't be processed still gets a
  sidecar, with `verdict: "error"` and `reason` explaining why, so nothing
  goes invisibly unclassified forever.
- **labels**: every detection across every sampled frame, regardless of
  verdict -- a "low" clip's incidental detections (a moth, a passing
  shadow) are still visible here for later review/debugging.
- **reason**: a short, machine-parsable string naming exactly which rule
  fired -- `"<label>>=<threshold>"` for high, `"large_other:<label>"` /
  `"persistent_detection"` / `"persistent_motion_no_detection"` for
  review, `"no_target_or_notable_detections"` for low, or the error text
  itself for `error`. Feeds the human review UI's reason display (see
  below).
- **sampled_frame_offsets**: which second-offsets into the clip actually
  got decoded and fed to the detector (see `clip_classifier/sampling.py`)
  -- only the ones that decoded successfully, not every offset attempted.
- **sampling_fallback**: `true` if this clip's metadata sidecar was schema
  v1 (or schema v2 with no usable motion timeline data), so frames were
  sampled evenly across the clip instead of around motion peaks.
- **backend** / **model** / **model_version**: which detector actually
  produced this verdict and with what model file -- `model_version` is a
  short hash of the model/HEF file, the same pattern as camera_watcher's
  own `config_hash`/`mask_hash`.

### Human review (the Recordings tab)

The classifier only ever writes `<stem>.analysis.json`; a human's
keep/discard decision on a clip lives in a third, separate sidecar,
`<stem>.review.json`:

```json
{"reviewed_at": "2026-07-17T14:35:10.002000+00:00", "decision": "keep", "note": "raccoon, not a person"}
```

`note` is optional. This file is written by `camera_watcher`'s web layer
(`POST /api/recordings/<filename>/review`, in `camera_watcher/web/routes.py`)
-- the classifier never writes it and never reads it back.

Each per-camera web UI's Recordings tab surfaces this directly, since
verdicts are per-camera data:

- Every clip in the list gets a small **verdict badge**
  (`high`/`review`/`low`/`error`/`unclassified` -- the last meaning
  `clip_classifier` hasn't reached this clip yet).
- A **verdict filter** dropdown above the list narrows it down to just one
  verdict at a time.
- Clips verdicted `review` get an extra panel showing the `reason`, the top
  3 detected labels by confidence, and **Keep** / **Discard** buttons.
  Keep writes `<stem>.review.json` with `decision: "keep"` and leaves the
  clip alone. Discard writes the same sidecar with `decision: "discard"`
  and then immediately deletes the clip through the same path the plain
  Delete button uses -- removing the video and its whole sidecar family
  (`.json`, `.analysis.json`, `.review.json`) together.

`GET /api/recordings` includes `verdict`, `reason`, `labels` (top 3), and
`reviewed` (the parsed `review.json`, or `null`) for every clip, reading
both sidecars best-effort -- a missing or corrupt sidecar is treated as
"not present yet" rather than an error.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use synthetic frames and a temp directory -- no real camera needed.
