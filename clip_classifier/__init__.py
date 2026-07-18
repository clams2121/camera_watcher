"""Tier-1 clip classifier: watches a camera_watcher fleet's shared data
root for finalized clips, samples frames around motion peaks, runs an
object detector, and writes a verdict sidecar (<stem>.analysis.json)
alongside each clip. Retention (see camera_watcher/retention.py) then
treats clips differently by verdict.

A separate, standalone process from camera_watcher -- it never modifies
the recorder or its metadata sidecar, only ever adds its own.
"""
