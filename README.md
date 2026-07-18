# camera_watcher

Watches an RTSP camera, detects motion, and saves motion clips (including a
pre/post buffer) to disk in size-capped chunks with overlap between them. A
small web UI lets you configure the camera, draw ignore-zones on a snapshot,
and view a live preview.

This segment only handles capture, motion-triggered recording, and
retention. Analyzing saved clips for people/other activity is a later stage
of the larger project.

## How it works

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

A background sweep enforces the configured retention policy (max age and/or
max total storage) on finalized clips, and a never-decaying motion heatmap
plus a per-clip event log (bounding boxes) help spot zones that need a new
or larger ignore zone -- see "Tuning ignore zones" below.

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

Every stage runs on its own thread so a slow one can never stall another,
and the web UI stays responsive no matter what the camera is doing:

- **Sub-stream capture thread** only reads frames off the RTSP socket and
  appends them to the shared frame buffer (live preview/snapshot source) --
  it never touches disk.
- **Passthrough recorder** (`segment_cache.py`): a supervised ffmpeg
  subprocess stream-copying the main stream into cached segments, with its
  own restart-with-backoff and stall detection (no new segment for too long
  while the process is still alive kills and restarts it).
- **Processing thread** pulls sub-stream frames off a bounded queue and does
  the motion detection work, decoupled from capture so a slow frame never
  backs up RTSP reads. If processing falls behind, the queue sheds (drops)
  the oldest-pending frames rather than growing without bound or blocking
  capture; `/api/status` reports `dropped_frames` if this happens.
- **Clip assembler thread**: motion-triggered ffmpeg concat calls run here,
  never on the frame-processing thread, so assembling a clip can never stall
  live motion detection.
- **Cache pruner** and **retention sweep** each run on their own timer
  thread.
- **Web UI** (Flask) runs on the main thread with a threaded WSGI server, and
  only ever touches the shared, thread-safe frame buffer and config -- never
  the camera connection directly -- so `GET /` and the API always respond
  immediately even while disconnected, reconnecting, or mid-recording.

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

Each camera gets its own config file, name of your choosing (letters,
digits, `_` and `-` only -- it ends up in clip filenames):

```bash
cp config/camera.example.yaml config/front-door.yaml
cp config/camera.secrets.example.yaml config/front-door.secrets.yaml
# edit both: at minimum camera.name/host in front-door.yaml, and
# camera.username/password + web.auth_token in front-door.secrets.yaml
```

`--config` is required and must point at a real file -- nothing is
auto-created for you here. If you point it at a path that doesn't exist,
the app fails immediately with the exact `cp` command above rather than
silently starting some default camera you didn't mean to configure. Startup
also fails loud if `web.auth_token` is missing/too short (see
Authentication below) or if `web.host` is the default `"tailscale"` and
this machine isn't on a Tailscale network.

Run it:

```bash
python -m camera_watcher.main --config config/front-door.yaml
```

**Every relative path inside `front-door.yaml`** (`data_root`,
`secrets_path`, `mask.path`, `recording.output_dir`, `recording.cache_dir`,
`motion.heatmap_path`) resolves against the directory `front-door.yaml`
itself lives in -- **never** against whatever directory you happen to run
the command from. This is what makes it safe to run several camera
processes from systemd, cron, or any working directory. Absolute paths
work too and pass through unchanged.

By default, `secrets_path` derives to `front-door.secrets.yaml` and
`mask.path` to `front-door.mask.json`, both next to the config file;
`recording.output_dir` derives to `<data_root>/clips/front-door`
(`data_root` itself defaults to a `data/` directory next to the config
file). Override any of these explicitly in the YAML if you want them
somewhere else -- e.g. point several cameras' `data_root` at the same
shared path so retention can sweep the whole fleet's clips from one place
(see Retention below).

Each camera needs its own `web.port` -- if two configs on the same host
try to use the same port, the second process fails loud at startup
("Cannot bind ... Address already in use") instead of silently stealing
the port or failing somewhere more confusing later.

Then open `http://<tailscale-ip>:8080` (see "Network binding: Tailscale
only" below for how to find that address, or whatever `web.port` you set)
for the web UI (Settings / Ignore Mask / Live Preview / Recordings tabs) --
from any device on the same tailnet. You'll land on a login page first; see
Authentication below. If it doesn't load, see Troubleshooting below.

To add a second camera, repeat the `cp` steps above with a new name and a
different `web.port`, then run a second `python -m camera_watcher.main
--config config/<name>.yaml` process.

The Recordings tab groups clips into 30-minute buckets aligned to :00/:30
(newest group first); clicking a clip streams it in the browser with
play/pause and ±10 second seek buttons (native scrubbing via the video's own
controls too). Each clip has its own "Delete" button, and each group header
has a "Delete all" button for clearing a whole half-hour at once -- both ask
for confirmation first. Neither waits on retention. The Ignore Mask tab
lists drawn shapes alongside the canvas -- click one to highlight it, then
"Delete selected shape" to remove just that one.

The header's "Stop Server" button asks you to type `quit` to confirm, then
cleanly shuts the whole process down (capture, recording, retention, the
heatmap accumulator all stop/flush the same way as Ctrl+C does in the
terminal). If you're running this under something that auto-restarts
crashed/exited processes (a systemd unit with `Restart=always`, `docker run
--restart unless-stopped`, etc.), it'll just come back up -- stop it at that
supervisor level too if you actually want it to stay down.

## Network binding: Tailscale only

`web.host` defaults to `"tailscale"`, which resolves this host's Tailscale
IPv4 address (`tailscale ip -4`) at startup and binds the web server only
there -- never `0.0.0.0`, never a LAN/public interface. If Tailscale isn't
installed, isn't logged in, or returns something that doesn't look like a
Tailscale address (`100.64.0.0/10`), startup fails loud with a plain-English
message instead of silently binding somewhere broader. Set `web.host` to an
explicit literal address (e.g. `127.0.0.1` for local testing) to bypass
Tailscale entirely.

Served by [waitress](https://docs.pylonsproject.org/projects/waitress/), a
production-grade WSGI server -- not Flask's own development server.

## Authentication

Every camera process requires its own auth token, generated once and stored
in that camera's `<name>.secrets.yaml`:

```bash
python -c "from camera_watcher.auth import generate_token; print(generate_token())"
```

```yaml
# <name>.secrets.yaml
web:
  auth_token: "<paste the generated token here>"
```

There's no way to disable this -- a missing or too-short (< 32 character)
token fails startup loud, the same way a missing `camera.name` does.

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
- **config_hash** / **mask_hash**: short hashes of the camera's settings/ignore-mask exactly as they were when this event's window opened -- lets you correlate "which config produced this clip" across a fleet, or spot that a clip landed right after a config change, without ever hashing credentials (`config_hash` is computed from `config/<name>.yaml` only, never `.secrets.yaml`).

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

Camera credentials **and** the web UI's auth token are **never** stored in
`config/<name>.yaml` and always go in `config/<name>.secrets.yaml` instead
(see `secrets_path` above if you want it named/located differently), kept
as a separate file so the two can't accidentally get mixed up. Both are
gitignored (see "Local config, not checked into git" below) -- neither is
ever meant to be committed. Only the generic
`config/camera.secrets.example.yaml` (with blank values) is tracked in git.
The web UI's password field never echoes back the stored password -- it
always displays blank, and leaving it blank on save keeps the existing
value.

Before committing, double check `git status` doesn't show any
`config/<name>.yaml`, `config/<name>.secrets.yaml`, `config/<name>.mask.json`,
or anything under `data/`.

## Local config, not checked into git

Everything this app writes to disk on its own -- each camera's
`config/<name>.yaml`, `config/<name>.secrets.yaml`, `config/<name>.mask.json`,
its motion heatmap, and recorded clips (with their metadata sidecars) under
`data/` -- is gitignored. Only checked-in *templates* live in git:

| Tracked template (safe to commit) | Local file it produces (gitignored) |
| --- | --- |
| `config/camera.example.yaml` | `config/<name>.yaml` -- copy and edit by hand (`--config` requires it to already exist; nothing is auto-created for you, on purpose -- see Setup above) |
| `config/camera.secrets.example.yaml` | `config/<name>.secrets.yaml` -- copy it yourself and fill in real credentials; there's no safe default to seed it with |
| `config/camera.mask.example.json` | `config/<name>.mask.json` -- *not* auto-copied either (its sample polygon is just a format example, not a sensible default for your camera); starts with no ignore zones and is created once you draw and save your first shape |

## Configuration reference

See `config/camera.example.yaml` for the full set of options with inline
comments, covering camera connection, motion sensitivity, recording
buffer/chunk/overlap timing, retention limits, and the web server -- your
actual `config/<name>.yaml` starts as a copy of it and has the same shape.

## Retention

Retention runs as a periodic sweep inside this process by default
(`retention.enabled`, `retention.max_age_days`, `retention.max_total_gb`).
The sweep logic itself lives in `camera_watcher/retention.py` as a
standalone function/CLI (`python -m camera_watcher.retention <clips_dir>
--max-age-days 14`), so a separate process/module can also invoke it
directly against the same clips directory later.

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
5. **Set `web.host` explicitly** if you deliberately want something other
   than Tailscale (e.g. `127.0.0.1` for local-machine-only testing) --
   see "Network binding: Tailscale only" above.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use synthetic frames and a temp directory -- no real camera needed.
