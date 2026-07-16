"""Small shared constants with no third-party dependencies.

Kept separate from recorder.py (which needs OpenCV) so that modules like
retention.py that only need the temp-file naming convention -- not video
encoding itself -- don't pull in a heavier dependency just to get a string.
"""
from __future__ import annotations

TEMP_SUFFIX = ".rec.mp4"
