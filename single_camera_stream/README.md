# single_camera_stream

A standalone, single-process, single-camera motion recorder -- closer to
this project's original design than the `camera_watcher` fleet supervisor
elsewhere in this repo: one process, one camera, one config file, one job.
No web UI, no fleet, no retention -- just watch an RTSP stream, record
motion-triggered clips to a directory, and write a small metadata file
(how long there was motion, and the clip's start/stop timestamps)
alongside each one.

Self-contained: nothing in here imports from `camera_watcher` or
`clip_classifier`.

## Setup

```bash
pip install -r single_camera_stream/requirements.txt
cp single_camera_stream/config.example.yaml single_camera_stream/config.yaml
# edit config.yaml: at minimum camera.host, and camera.username/password if needed
python -m single_camera_stream.main
```

`config.yaml` is safe to point elsewhere with `--config /path/to/other.yaml`
if you want to run more than one instance on the same host -- each gets
its own directory, its own PID file (see below), and its own
`recording.output_dir`.

## Configuration

Everything lives in one YAML file (`config.example.yaml` documents every
field inline):

- **camera**: `name` (used in clip filenames), `host`, `port`, `path` (the
  RTSP path), `transport` (`tcp`/`udp`), `username`/`password`.
- **motion**: `enabled`, `analysis_width`, `min_area`, `var_threshold`,
  `history` (MOG2 background-subtraction tuning), `draw_bounding_box`,
  `box_padding_px`.
- **recording**: `output_dir`, `pre_buffer_seconds`, `post_buffer_seconds`,
  `max_chunk_seconds`, `overlap_seconds`, `fallback_fps`.

`recording.output_dir` is the only relative path in the file; it resolves
against the directory `config.yaml` itself lives in, never whatever
directory you happen to run the command from.

Credentials live in this same file (unlike `camera_watcher`, which splits
them into a separate secrets file) -- keep `config.yaml` itself out of
git (it already is; see `.gitignore`).

## How it records

Frames are decoded and re-encoded directly via `cv2.VideoWriter` --
simpler than, and unlike, `camera_watcher`'s ffmpeg-passthrough
stream-copy approach, at the cost of a little CPU/quality overhead from
re-encoding. On motion:

1. A new clip opens, seeded with `pre_buffer_seconds` of frames already
   sitting in the in-memory ring buffer (so the moment that triggered
   detection isn't the first frame of the file).
2. Recording continues until `post_buffer_seconds` pass with no further
   motion, or `max_chunk_seconds` is reached (in which case the clip is
   force-split, carrying `overlap_seconds` of frames into the next one so
   nothing is lost across the cut).
3. The clip is written under a temporary name (`<name>.mp4.rec.mp4`) and
   atomically renamed to its final name
   (`<camera.name>_<YYYYMMDD>_<HHMMSS>.mp4`) only once fully written.

## Metadata

Every finalized clip gets a same-named `.json` file next to it, written
atomically, with exactly three pieces of information:

```json
{
  "event_id": "front-door_20260117_140030",
  "camera_name": "front-door",
  "start_time": "2026-01-17T14:00:28.512340-05:00",
  "stop_time": "2026-01-17T14:00:42.881230-05:00",
  "motion_seconds": 8.437
}
```

- **start_time** / **stop_time**: the clip's own span (including the
  pre/post buffer), not just the moment motion was first/last detected.
- **motion_seconds**: total time within the clip where motion was
  actually being detected, summed frame-interval by frame-interval --
  not a fraction, an absolute duration.

## Singleton lock (PID file)

On startup, before doing anything else, the process takes a lock so a
second instance pointed at the same config can't also start and fight over
the same output files:

1. If a PID file already exists next to the config being used
   (`single_camera_stream.pid`) and it names a process that's still
   alive, startup fails loud immediately -- refusing to start.
2. Otherwise (no PID file yet, or a stale one from a crashed/killed
   run) it writes its own PID into that file.
3. It immediately reads the file back to check for a narrow race: if a
   second instance also passed step 1 in the same window and wrote its
   own PID afterward, the file now names *that* process instead. Whichever
   instance the file doesn't name loses and stops.

The PID file is removed on a clean shutdown (Ctrl+C or SIGTERM), and only
if it still names this process -- never a newer instance that's since
taken over.

## Tests

From the repo root:

```bash
pip install -r requirements-dev.txt
pytest tests/ -k single_camera_stream
```

Real synthetic frames and real subprocesses (for the PID-lock tests'
live/dead-PID checks) -- no real camera needed.
