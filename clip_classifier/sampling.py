"""Picks which frames of a clip to feed the detector, and decodes exactly
those -- never the whole video.

Frame extraction uses ffmpeg (input-side `-ss <offset>` before `-i`,
keyframe seeking, piped out as a single JPEG per frame) rather than
OpenCV's own seeking (`cv2.VideoCapture.set(CAP_PROP_POS_FRAMES/MSEC, ...)`):
OpenCV's frame-accurate seek support is inconsistent across H.264
profiles/containers and often lands on the wrong frame outright, whereas
ffmpeg's input-side seek is fast, reliably supported everywhere ffmpeg
already is (this project depends on it for recording anyway -- see
segment_cache.py/assemble.py), and more than precise enough here:
classification needs "a frame from around this second," not frame-exact
timing.

A note on offset precision: `motion_timeline` offsets (see
camera_watcher/recorder.py) are measured from the recorder's *logical*
event window start (`content_start_ts`), but the assembled clip's actual
t=0 is whichever cached segment boundary that window happened to land on --
which can be up to `segment_seconds` earlier. This module treats them as
the same thing (i.e. assumes the clip's own t=0 is `start_time`), which is
an approximation -- the same one camera_watcher's own README already
documents for `duration_seconds` vs. `end_time - start_time`, and the same
fix (a short camera I-frame interval) tightens it. Offsets are clamped to
`duration_seconds` so this drift can only ever cost a little precision on
*which* frame gets sampled, never a seek past the end of the file.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class SamplingError(Exception):
    """Raised when no frame at all could be decoded from a clip."""


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _evenly_spaced(duration: float, count: int) -> List[float]:
    if duration <= 0 or count <= 0:
        return [0.0]
    if count == 1:
        return [round(duration / 2, 3)]
    step = duration / count
    return [round(step * (i + 0.5), 3) for i in range(count)]


def select_offsets(
    metadata: dict, max_frames: int = 5, min_spacing_seconds: float = 1.0
) -> Tuple[List[float], bool]:
    """Returns (offsets_in_seconds_from_clip_start, used_even_spacing_fallback).

    Schema v2 (``schema_version`` >= 2): always includes the
    ``peak_motion_time`` offset (if any motion was ever detected during the
    event), then fills up to `max_frames` with the highest-scoring distinct
    seconds from ``motion_timeline``, enforcing at least
    `min_spacing_seconds` between any two chosen offsets.

    Schema v1 (no ``schema_version`` field at all -- pre-Prompt-A-Task-3
    clips) has none of that: falls back to sampling evenly across the clip,
    with duration computed as ``end_time - start_time`` -- exact for v1
    clips, which were written frame-by-frame via cv2.VideoWriter with no
    segment-boundary padding to account for.

    The same even-spacing fallback also covers the schema v2 edge case of a
    clip with no motion timeline data at all (e.g. motion detection was
    disabled) -- something must still be sampled rather than nothing.
    """
    schema_version = metadata.get("schema_version", 1)
    start_time = _parse_iso(metadata["start_time"])

    if schema_version < 2:
        logger.warning(
            "%s has a schema v1 metadata sidecar (no motion_timeline/peak_motion_time) -- "
            "falling back to even sampling across the clip",
            metadata.get("event_id", "<unknown clip>"),
        )
        end_time = _parse_iso(metadata["end_time"])
        duration = max((end_time - start_time).total_seconds(), 0.0)
        return _evenly_spaced(duration, max_frames), True

    duration = metadata.get("duration_seconds")
    offsets: List[float] = []

    peak_motion_time = metadata.get("peak_motion_time")
    if peak_motion_time:
        offsets.append((_parse_iso(peak_motion_time) - start_time).total_seconds())

    candidates = sorted(metadata.get("motion_timeline") or [], key=lambda entry: entry["score"], reverse=True)
    for entry in candidates:
        if len(offsets) >= max_frames:
            break
        candidate = float(entry["t"])
        if any(abs(candidate - chosen) < min_spacing_seconds for chosen in offsets):
            continue
        offsets.append(candidate)

    if duration is not None:
        offsets = [o for o in offsets if 0 <= o <= duration]

    if not offsets:
        logger.warning(
            "%s has a schema v2 metadata sidecar but no usable motion_timeline/peak_motion_time data -- "
            "falling back to even sampling across the clip",
            metadata.get("event_id", "<unknown clip>"),
        )
        return _evenly_spaced(duration or 0.0, max_frames), True

    offsets.sort()
    return offsets[:max_frames], False


def extract_frame(video_path: Path, offset_seconds: float, timeout: float = 15.0) -> Optional[np.ndarray]:
    """Decodes a single BGR frame at `offset_seconds` into the clip, or
    None if ffmpeg couldn't produce one (e.g. the offset landed past EOF)."""
    offset_seconds = max(offset_seconds, 0.0)
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{offset_seconds:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0 or not result.stdout:
        logger.warning(
            "Failed to extract a frame at %.2fs from %s: %s",
            offset_seconds,
            video_path.name,
            result.stderr.decode(errors="replace").strip()[-500:],
        )
        return None

    frame = cv2.imdecode(np.frombuffer(result.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        logger.warning(
            "ffmpeg produced output but it wasn't a decodable image at %.2fs from %s", offset_seconds, video_path.name
        )
        return None
    return frame


def sample_frames(
    video_path: Path, metadata: dict, max_frames: int = 5, min_spacing_seconds: float = 1.0
) -> Tuple[List[float], List[np.ndarray], bool]:
    """Selects offsets, decodes each one, and returns only the offsets that
    actually produced a frame -- one failed seek doesn't sink the whole
    clip, only every offset failing does (SamplingError)."""
    offsets, used_fallback = select_offsets(metadata, max_frames, min_spacing_seconds)

    good_offsets: List[float] = []
    frames: List[np.ndarray] = []
    for offset in offsets:
        frame = extract_frame(video_path, offset)
        if frame is not None:
            good_offsets.append(offset)
            frames.append(frame)

    if not frames:
        raise SamplingError(f"Could not decode any frame from {video_path} at offsets {offsets}")

    return good_offsets, frames, used_fallback
