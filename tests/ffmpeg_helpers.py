"""Shared helpers for tests that need real, tiny mp4 files -- assemble.py's
concat demuxer and segment_cache.py's segment listing/pruning both operate
on real files on disk (segment start time comes from the *filename*, not
file content), so generating real ffmpeg output here, rather than mocking it
away, is what actually exercises those code paths.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path


def make_segment(path: Path, duration: float = 0.2, size: str = "64x64") -> None:
    """Writes a real, tiny, valid mp4 to `path` via ffmpeg's lavfi testsrc --
    content is irrelevant to the tests that use this, only that it's a real,
    ffprobe-able video file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=10:duration={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def segment_name(ts: float) -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(ts)) + ".mp4"


def make_cache_segments(cache_dir: Path, start_ts: float, count: int, segment_seconds: float = 1.0) -> None:
    """Populates `cache_dir` with `count` real mp4 segments named per
    SegmentCache's strftime convention, one per whole `segment_seconds` tick
    starting at `start_ts` -- the *names* simulate the timeline a test wants;
    the real (short) content duration keeps test runtime fast."""
    for i in range(count):
        ts = start_ts + i * segment_seconds
        make_segment(cache_dir / segment_name(ts))


def probe_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())
