"""single_camera_stream: a standalone, single-process, single-camera
recorder.

Deliberately self-contained (no dependency on the camera_watcher or
clip_classifier packages elsewhere in this repo) and deliberately simple,
closer to this project's original single-process design than the current
camera_watcher fleet supervisor: one process, one camera, one config file,
one job -- watch an RTSP stream, record motion-triggered clips to a
directory, and write a small metadata file (motion duration, start/stop
timestamps) alongside each one.

See README.md in this directory for how to configure and run it.
"""
