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

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # add -dev.txt too if running tests

cp config/secrets.yaml.example config/secrets.yaml
# edit config/secrets.yaml with your camera's username/password
```

Edit `config/settings.yaml` (or use the web UI once running) for the
camera's host/port/path and the motion/recording/retention parameters.

Run it:

```bash
python -m camera_watcher.main
```

Then open `http://localhost:8080` for the web UI (Settings / Ignore Mask /
Live Preview tabs).

## Credentials -- please read

Camera credentials are **never** stored in `config/settings.yaml` (which is
safe to commit) and always go in `config/secrets.yaml`, which is listed in
`.gitignore` and must never be committed. Only `config/secrets.yaml.example`
(with blank values) is tracked in git. The web UI's password field never
echoes back the stored password -- it always displays blank, and leaving it
blank on save keeps the existing value.

Before committing, double check `git status` doesn't show
`config/secrets.yaml`, `config/mask.json`, or anything under `data/`.

## Configuration reference

See `config/settings.yaml` for the full set of options with inline comments,
covering camera connection, motion sensitivity, recording buffer/chunk/
overlap timing, retention limits, and the web server.

## Retention

Retention runs as a periodic sweep inside this process by default
(`retention.enabled`, `retention.max_age_days`, `retention.max_total_gb`).
The sweep logic itself lives in `camera_watcher/retention.py` as a
standalone function/CLI (`python -m camera_watcher.retention <clips_dir>
--max-age-days 14`), so a separate process/module can also invoke it
directly against the same clips directory later.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests use synthetic frames and a temp directory -- no real camera needed.
