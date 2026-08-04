"""Motion-triggered recording via cv2.VideoWriter, with a pre-motion
buffer, a post-motion cooldown, and a hard per-file length cap.

This is the "first version" recording design: frames are decoded and
re-encoded directly (not stream-copied), unlike the fleet supervisor's
later ffmpeg-passthrough approach elsewhere in this repo -- simpler, at
the cost of a small CPU/quality overhead from re-encoding.

Every finalized clip gets a same-named ``.json`` file next to it, written
atomically (temp-write-then-rename), with exactly the fields asked for:
how long motion was actually detected during the clip, and the clip's own
start/stop timestamps.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .frame_buffer import FrameBuffer, FrameEntry
from .motion import BoundingBox

logger = logging.getLogger(__name__)

TEMP_SUFFIX = ".rec.mp4"

# BGR -- OpenCV's channel order -- for a high-contrast, easy-to-spot box.
_BOX_COLOR = (0, 255, 0)


@dataclass
class RecorderConfig:
    output_dir: Path
    camera_name: str
    pre_buffer_seconds: float = 10.0
    post_buffer_seconds: float = 10.0
    max_chunk_seconds: float = 180.0
    overlap_seconds: float = 5.0
    fallback_fps: float = 15.0
    draw_bounding_box: bool = False
    box_padding_px: int = 12


def scale_box(box: BoundingBox, analysis_size: Tuple[int, int], frame_shape: tuple, padding_px: int) -> BoundingBox:
    """Maps a motion box from analysis-resolution coordinates to full-frame
    pixel coordinates, padded outward by `padding_px` and clamped to the
    frame -- shared by the burn-in below."""
    analysis_w, analysis_h = analysis_size
    frame_h, frame_w = frame_shape[:2]
    scale_x, scale_y = frame_w / analysis_w, frame_h / analysis_h
    x, y, w, h = box
    x1 = max(0, int(x * scale_x) - padding_px)
    y1 = max(0, int(y * scale_y) - padding_px)
    x2 = min(frame_w, int((x + w) * scale_x) + padding_px)
    y2 = min(frame_h, int((y + h) * scale_y) + padding_px)
    return (x1, y1, x2 - x1, y2 - y1)


def draw_box(frame: np.ndarray, box: BoundingBox) -> np.ndarray:
    """Returns a copy of `frame` with `box` burned in -- never mutates the
    original, since it may still be sitting in the shared frame buffer."""
    annotated = frame.copy()
    x, y, w, h = box
    cv2.rectangle(annotated, (x, y), (x + w, y + h), _BOX_COLOR, 2)
    return annotated


class SingleStreamRecorder:
    """Feed every captured frame through handle_frame(); this owns the
    record/idle state machine and all file writing. Not thread-safe against
    concurrent handle_frame() calls (call it from one thread only), but
    is_recording/stop() are safe to call from another."""

    def __init__(self, frame_buffer: FrameBuffer, config: RecorderConfig):
        self.frame_buffer = frame_buffer
        self.config = config
        self._lock = threading.Lock()
        self._writer: Optional[cv2.VideoWriter] = None
        self._temp_path: Optional[Path] = None
        self._final_name: Optional[str] = None
        self._clip_start_ts: Optional[float] = None  # includes pre-buffer/carried-over overlap
        self._chunk_start_ts: Optional[float] = None  # resets on a forced split; drives max_chunk_seconds
        self._last_motion_ts: Optional[float] = None
        self._motion_seconds: float = 0.0
        self._last_frame_ts: Optional[float] = None
        self._frame_size: Optional[Tuple[int, int]] = None
        self._pending_overlap: List[FrameEntry] = []

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._writer is not None

    def handle_frame(self, timestamp: float, frame: np.ndarray, motion_detected: bool, box=None, analysis_size=None) -> None:
        with self._lock:
            self._frame_size = (frame.shape[1], frame.shape[0])
            frame_interval = 0.0
            if self._last_frame_ts is not None:
                frame_interval = max(0.0, timestamp - self._last_frame_ts)
            self._last_frame_ts = timestamp

            if self._writer is None:
                if not motion_detected:
                    return
                self._start_clip(timestamp)

            out_frame = frame
            if self.config.draw_bounding_box and box is not None and analysis_size is not None:
                full_box = scale_box(box, analysis_size, frame.shape, self.config.box_padding_px)
                out_frame = draw_box(frame, full_box)

            if motion_detected:
                self._last_motion_ts = timestamp
                self._motion_seconds += frame_interval

            self._writer.write(out_frame)

            if timestamp - self._chunk_start_ts >= self.config.max_chunk_seconds:
                self._finalize_clip(timestamp, forced_split=True)
                self._start_clip(timestamp, carry_over=True)
                return

            if timestamp - self._last_motion_ts >= self.config.post_buffer_seconds:
                self._finalize_clip(timestamp, forced_split=False)

    def _start_clip(self, timestamp: float, carry_over: bool = False) -> None:
        fps = self.frame_buffer.measured_fps() or self.config.fallback_fps

        if carry_over:
            seed_frames = self._pending_overlap
            self._pending_overlap = []
        else:
            pre_start = timestamp - self.config.pre_buffer_seconds
            seed_frames = self.frame_buffer.since(pre_start)

        dt = datetime.fromtimestamp(timestamp).astimezone()
        self._final_name = f"{self.config.camera_name}_{dt.strftime('%Y%m%d_%H%M%S')}.mp4"
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        self._temp_path = self.config.output_dir / (self._final_name + TEMP_SUFFIX)

        size = self._frame_size or (640, 480)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(str(self._temp_path), fourcc, fps, size)

        self._clip_start_ts = seed_frames[0][0] if seed_frames else timestamp
        self._chunk_start_ts = timestamp
        self._last_motion_ts = timestamp
        self._motion_seconds = 0.0

        for _, seed_frame in seed_frames:
            self._writer.write(seed_frame)

    def _finalize_clip(self, timestamp: float, forced_split: bool) -> None:
        writer, temp_path, final_name = self._writer, self._temp_path, self._final_name
        self._writer = None
        if writer is not None:
            writer.release()
        if temp_path is None or final_name is None:
            return

        final_path = self.config.output_dir / final_name
        try:
            temp_path.replace(final_path)
        except OSError:
            logger.exception("Failed to finalize clip %s", temp_path)
            return

        if forced_split:
            overlap_start = timestamp - self.config.overlap_seconds
            self._pending_overlap = self.frame_buffer.since(overlap_start)
        else:
            self._pending_overlap = []

        self._write_metadata(final_path, timestamp)
        logger.info("Finalized clip %s (motion=%.1fs)", final_path.name, self._motion_seconds)

    def _write_metadata(self, final_path: Path, stop_ts: float) -> None:
        metadata = {
            "event_id": final_path.stem,
            "camera_name": self.config.camera_name,
            "start_time": datetime.fromtimestamp(self._clip_start_ts).astimezone().isoformat(),
            "stop_time": datetime.fromtimestamp(stop_ts).astimezone().isoformat(),
            "motion_seconds": round(self._motion_seconds, 3),
        }
        metadata_path = final_path.with_suffix(".json")
        tmp_path = metadata_path.with_suffix(".tmp.json")
        tmp_path.write_text(json.dumps(metadata, indent=2))
        tmp_path.replace(metadata_path)

    def stop(self) -> None:
        """Finalizes any in-progress clip synchronously -- call this on shutdown."""
        with self._lock:
            if self._writer is not None:
                self._finalize_clip(self._last_frame_ts or time.time(), forced_split=False)
