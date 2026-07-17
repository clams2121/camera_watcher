# camera_watcher

Watches an RTSP camera, detects motion, and saves motion clips (including a
pre/post buffer) to disk in size-capped chunks with overlap between them. A
small web UI lets you configure the camera, draw ignore-zones on a snapshot,
and view a live preview.

This segment only handles capture, motion-triggered recording, and
retention. Analyzing saved clips for people/other activity is a later stage
of the larger project.

## How it works

- A capture thread reads the RTSP stream (forced TCP transport by default)
  and keeps a rolling, time-based buffer of the last few seconds of frames.
- Each frame is checked for motion via background subtraction on a
  downscaled copy, after masking out any user-defined ignore zones.
- On motion, a recorder opens a new clip, prepends the buffered pre-roll,
  and keeps writing through a post-motion cooldown so flaky detection
  doesn't fragment one event into many clips.
- Clips are capped at `max_chunk_seconds` (3 minutes by default); a forced
  split carries `overlap_seconds` of frames into the next chunk so nothing
  is lost across the cut.
- Every clip is written under a temporary name and atomically renamed to its
  final, timestamped name (`<camera>_<YYYYMMDD>_<HHMMSS>.mp4`) only once
  fully written.
- A background sweep enforces the configured retention policy (max age
  and/or max total storage), only ever touching finalized clips.
- A never-decaying motion heatmap and a per-clip event log (bounding boxes)
  help spot spots that need a new or larger ignore zone -- see "Tuning
  ignore zones" below.

## Threading model

Every stage runs on its own thread so a slow one can never stall another,
and the web UI stays responsive no matter what the camera is doing:

- **Capture thread** only reads frames off the RTSP socket and appends them
  to the shared pre-roll buffer -- it never touches disk.
- **Processing thread** pulls frames off a bounded queue and does the
  actually-slow work: motion detection and writing video to disk. It's
  decoupled from capture specifically so a slow disk write can never back up
  RTSP reads. If processing falls behind, the queue sheds (drops) the
  oldest-pending frames rather than growing without bound or blocking
  capture; `/api/status` reports `dropped_frames` if this happens.
- **Retention sweep** runs on its own timer thread.
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

cp config/secrets.yaml.example config/secrets.yaml
# edit config/secrets.yaml with your camera's username/password
```

`config/settings.yaml` doesn't need to be created by hand -- the first time
the app runs, it's seeded automatically from the checked-in
`config/settings.example.yaml`. From then on it's yours to edit directly (or
through the web UI, which writes back to the same file); it's gitignored,
so local changes never show up as something to commit. Only
`settings.example.yaml` is tracked in git, documenting every option with its
default value.

Run it:

```bash
python -m camera_watcher.main
```

Then open `http://localhost:8080` for the web UI (Settings / Ignore Mask /
Live Preview / Recordings tabs) -- from a browser **on that same machine**.
To reach it from a different device, see Troubleshooting below.

The Recordings tab lists saved clips (newest first); clicking one streams it
in the browser with play/pause and ±10 second seek buttons (native scrubbing
via the video's own controls too). The Ignore Mask tab lists drawn shapes
alongside the canvas -- click one to highlight it, then "Delete selected
shape" to remove just that one.

The header's "Stop Server" button asks you to type `quit` to confirm, then
cleanly shuts the whole process down (capture, recording, retention, the
heatmap accumulator all stop/flush the same way as Ctrl+C does in the
terminal). If you're running this under something that auto-restarts
crashed/exited processes (a systemd unit with `Restart=always`, `docker run
--restart unless-stopped`, etc.), it'll just come back up -- stop it at that
supervisor level too if you actually want it to stay down.

## Tuning ignore zones: drawing past the edge, the heatmap, and event log

Three features work together for diagnosing "this ignore zone isn't working"
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
- **Per-clip motion log.** Every finalized clip appends one line to
  `recording.event_log_path` (default `data/motion_events.jsonl`) with its
  timestamp, filename, and the overall bounding box of what triggered it --
  e.g. `{"timestamp": ..., "camera": "camera1", "clip": "camera1_....mp4",
  "bbox": [x, y, w, h]}`. Set it to `""` to disable. Useful for scripting a
  review of which regions keep triggering recordings over time.

## Bounding boxes on recorded video

`motion.draw_bounding_box` (off by default, toggleable from Settings) burns
a bright green box around detected motion into recorded frames, padded
`motion.box_padding_px` away from the contour so it doesn't obscure the
moving object itself. It's meant as a tuning/testing aid -- turn it on while
dialing in sensitivity and ignore zones, then back off so future recordings
stay unannotated for any later analysis stage. It only affects frames
recorded *after* motion starts within an event; prepended pre-roll frames
(already captured before detection fired) are written as originally
captured.

### Missing dependencies

Before doing anything else, `python -m camera_watcher.main` checks that
OpenCV, NumPy, Flask, and PyYAML are all importable. If any are missing it
prints exactly which packages are missing and how to install them (`pip
install -r requirements.txt`), and exits, rather than failing with a raw
traceback partway through startup. `camera_watcher/retention.py` has no
third-party dependencies at all and can run standalone with just Python 3.

## Credentials -- please read

Camera credentials are **never** stored in `config/settings.yaml` and always
go in `config/secrets.yaml` instead, kept as a separate file so the two
can't accidentally get mixed up. Both files are gitignored (see "Local
config, not checked into git" below) -- neither is ever meant to be
committed. Only `config/secrets.yaml.example` (with blank values) is tracked
in git. The web UI's password field never echoes back the stored password --
it always displays blank, and leaving it blank on save keeps the existing
value.

Before committing, double check `git status` doesn't show
`config/settings.yaml`, `config/secrets.yaml`, `config/mask.json`, or
anything under `data/`.

## Local config, not checked into git

Everything this app writes to disk on its own -- `config/settings.yaml`,
`config/secrets.yaml`, `config/mask.json`, the motion heatmap, event log,
and recorded clips under `data/` -- is gitignored. Only checked-in
*templates* live in git:

| Tracked template (safe to commit) | Local file it produces (gitignored) |
| --- | --- |
| `config/settings.example.yaml` | `config/settings.yaml` -- auto-copied the first time the app runs, so it starts with every documented default already in place |
| `config/mask.example.json` | `config/mask.json` -- *not* auto-copied (its sample polygon is just a format example, not a sensible default for your camera); starts with no ignore zones and is created once you draw and save your first shape |
| `config/secrets.yaml.example` | `config/secrets.yaml` -- copy it yourself and fill in real credentials (see Setup above); there's no safe default to seed it with |

## Configuration reference

See `config/settings.example.yaml` for the full set of options with inline
comments, covering camera connection, motion sensitivity, recording
buffer/chunk/overlap timing, retention limits, and the web server -- your
actual `config/settings.yaml` starts as a copy of it and has the same shape.

## Retention

Retention runs as a periodic sweep inside this process by default
(`retention.enabled`, `retention.max_age_days`, `retention.max_total_gb`).
The sweep logic itself lives in `camera_watcher/retention.py` as a
standalone function/CLI (`python -m camera_watcher.retention <clips_dir>
--max-age-days 14`), so a separate process/module can also invoke it
directly against the same clips directory later.

## Troubleshooting: can't reach the web UI from another device

`web.host` defaults to `0.0.0.0`, so the app already listens on every
network interface on the machine it runs on -- it's not restricted to
`localhost`. If `http://<host-machine-ip>:8080` doesn't load from a second
device, work through these in order:

1. **Confirm the app itself is healthy first, from the host machine:**
   `curl http://localhost:8080/` there. If that fails, the problem is the
   app/config, not networking -- check the terminal running
   `camera_watcher.main` for errors. If it succeeds, the app is fine and the
   rest of this list applies.
2. **Use the host's actual LAN IP**, not `localhost`/`127.0.0.1` (that only
   ever means "this machine" to whatever device you type it into). Find it
   with `ip addr` / `hostname -I` (Linux), `ipconfig` (Windows), or `ifconfig`
   (macOS) -- look for the address on your LAN/Wi-Fi adapter, not a VPN,
   Docker, or loopback interface.
3. **Check `config/settings.yaml`'s `web.host` hasn't been changed** to
   `127.0.0.1` or `localhost` -- that would make it refuse connections from
   anywhere but the host itself. It should be `0.0.0.0`.
4. **Running inside Docker, WSL2, or a VM?** `0.0.0.0` inside the
   container/VM is not automatically reachable from your LAN. Docker needs
   an explicit published port (`docker run -p 8080:8080 ...`); WSL2 needs
   either mirrored networking mode or its own port-forwarding setup.
5. **Check the host machine's firewall** allows inbound connections on the
   port (e.g. `sudo ufw allow 8080/tcp` on Ubuntu, or an inbound rule in
   Windows Defender Firewall / macOS's firewall). This is the most common
   blocker and won't show up in the app's own logs at all -- the connection
   just times out.
6. **Same network, but still nothing?** Some Wi-Fi networks (especially
   guest networks) enable "client/AP isolation," which blocks device-to-device
   traffic even on the same SSID/subnet. Try both devices on a wired
   connection or a non-guest network to rule this out.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use synthetic frames and a temp directory -- no real camera needed.
