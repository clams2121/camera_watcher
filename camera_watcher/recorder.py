"""Motion-triggered segment recorder.

State machine, driven by calling :meth:`SegmentRecorder.handle_frame` once
per incoming frame from a single thread:

- IDLE, no motion: nothing is written.
- Motion detected: a new chunk file opens, primed with ``pre_buffer_seconds``
  of frames pulled from the shared pre-roll buffer, then live frames are
  appended.
- Recording continues through a ``post_buffer_seconds`` cooldown after the
  last detected motion, so a brief gap in detection doesn't fragment one real
  event into multiple clips.
- A chunk longer than ``max_chunk_seconds`` is force-split into a new file,
  carrying the last ``overlap_seconds`` of frames into the start of the next
  chunk so nothing is lost across the cut.
- Every chunk is written under a temporary name and atomically renamed to its
  final, timestamped name only after the writer has cleanly closed -- a
  reader never sees a half-written file under its final name.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .frame_buffer import FrameBuffer, TimedFrame

logger = logging.getLogger(__name__)

TEMP_SUFFIX = ".rec.mp4"


@dataclass
class RecorderConfig:
    output_dir: Path
    pre_buffer_seconds: float = 10
    post_buffer_seconds: float = 10
    max_chunk_seconds: float = 180
    overlap_seconds: float = 5
    fourcc: str = "mp4v"
    max_width: int = 1920
    camera_name: str = "camera1"


def _timestamp_name(camera_name: str, ts: float, suffix: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
    return f"{camera_name}_{stamp}{suffix}"


def _scale_frame(frame: np.ndarray, max_width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame
    scale = max_width / w
    return cv2.resize(frame, (max_width, int(h * scale)))


class SegmentRecorder:
    """Not thread-safe on its own -- call ``handle_frame`` from a single thread."""

    def __init__(self, pre_buffer: FrameBuffer, config: RecorderConfig, fps_hint: float = 15.0):
        self._pre_buffer = pre_buffer
        self.config = config
        self._fps_hint = fps_hint

        self._recording = False
        self._writer: Optional[cv2.VideoWriter] = None
        self._temp_path: Optional[Path] = None
        self._final_path: Optional[Path] = None
        self._chunk_start_ts: Optional[float] = None
        self._last_motion_ts: Optional[float] = None
        self._frame_size: Optional[tuple] = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def current_temp_path(self) -> Optional[Path]:
        return self._temp_path

    def set_fps_hint(self, fps: float) -> None:
        if fps and fps > 0:
            self._fps_hint = fps

    def handle_frame(self, timestamp: float, frame: np.ndarray, motion_detected: bool) -> None:
        if motion_detected:
            self._last_motion_ts = timestamp

        if not self._recording:
            if not motion_detected:
                return
            prepend = [tf for tf in self._pre_buffer.snapshot(self.config.pre_buffer_seconds) if tf.timestamp < timestamp]
            self._open_chunk(timestamp, prepend_frames=prepend)
        else:
            post_buffer_expired = (
                self._last_motion_ts is not None
                and timestamp - self._last_motion_ts > self.config.post_buffer_seconds
            )
            if post_buffer_expired:
                self._finish_event()
                return

        self._write_frame_raw(frame)

        if timestamp - self._chunk_start_ts >= self.config.max_chunk_seconds:
            self._roll_chunk(timestamp)

    def _open_chunk(self, boundary_ts: float, prepend_frames: Optional[list] = None) -> None:
        prepend_frames = prepend_frames or []
        name_ts = prepend_frames[0].timestamp if prepend_frames else boundary_ts

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        name = _timestamp_name(self.config.camera_name, name_ts, ".mp4")
        self._final_path = self.config.output_dir / name
        self._temp_path = self.config.output_dir / (name + TEMP_SUFFIX)
        self._writer = None
        self._frame_size = None
        self._chunk_start_ts = boundary_ts
        self._recording = True
        logger.info("Starting recording chunk: %s (%d prepended frames)", name, len(prepend_frames))

        for tf in prepend_frames:
            self._write_frame_raw(tf.frame)

    def _write_frame_raw(self, frame: np.ndarray) -> None:
        frame = _scale_frame(frame, self.config.max_width)
        if self._writer is None:
            h, w = frame.shape[:2]
            self._frame_size = (w, h)
            fourcc = cv2.VideoWriter_fourcc(*self.config.fourcc)
            self._writer = cv2.VideoWriter(str(self._temp_path), fourcc, self._fps_hint, (w, h))
        elif (frame.shape[1], frame.shape[0]) != self._frame_size:
            frame = cv2.resize(frame, self._frame_size)
        self._writer.write(frame)

    def _roll_chunk(self, timestamp: float) -> None:
        overlap = self._pre_buffer.snapshot(self.config.overlap_seconds)
        overlap = [tf for tf in overlap if tf.timestamp <= timestamp]
        self._close_chunk()
        self._open_chunk(timestamp, prepend_frames=overlap)

    def _finish_event(self) -> None:
        self._close_chunk()
        self._recording = False
        self._chunk_start_ts = None
        self._last_motion_ts = None

    def _close_chunk(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self._temp_path and self._temp_path.exists():
            self._temp_path.rename(self._final_path)
            logger.info("Finalized recording: %s", self._final_path.name)
        self._temp_path = None
        self._final_path = None

    def flush_on_shutdown(self) -> None:
        """Cleanly close any in-progress recording, e.g. during process shutdown."""
        if self._recording:
            self._finish_event()
