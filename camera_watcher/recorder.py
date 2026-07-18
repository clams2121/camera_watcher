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

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Tuple

import cv2
import numpy as np

from .constants import TEMP_SUFFIX
from .frame_buffer import FrameBuffer, TimedFrame

logger = logging.getLogger(__name__)

BoundingBox = Tuple[int, int, int, int]  # (x, y, w, h)


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
    event_log_path: Optional[Path] = None  # JSONL log of each clip's motion bounding box; None disables it


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
        self._chunk_bbox: Optional[Tuple[int, int, int, int]] = None  # (x1, y1, x2, y2), full-frame coords

        # Per-chunk stats accumulated for the companion metadata JSON (see
        # _write_metadata). All reset in _open_chunk.
        self._chunk_content_start_ts: Optional[float] = None  # earliest included frame -- "start_time" w/ pre-buffer
        self._chunk_last_frame_ts: Optional[float] = None  # most recent frame actually written -- "end_time"
        self._chunk_prev_live_ts: Optional[float] = None  # for measuring inter-frame gaps on live frames only
        self._chunk_frame_count = 0
        self._chunk_motion_frame_count = 0
        self._chunk_score_sum = 0.0
        self._chunk_score_count = 0
        self._chunk_score_max = 0.0
        self._chunk_max_detection_fraction = 0.0
        self._chunk_motion_seconds = 0.0

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def current_temp_path(self) -> Optional[Path]:
        return self._temp_path

    def set_fps_hint(self, fps: float) -> None:
        if fps and fps > 0:
            self._fps_hint = fps

    def handle_frame(
        self,
        timestamp: float,
        frame: np.ndarray,
        motion_detected: bool,
        boxes: Sequence[BoundingBox] = (),
        score: float = 0,
        detection_fraction: float = 0.0,
    ) -> None:
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
        self._chunk_last_frame_ts = timestamp
        self._accumulate_bbox(boxes)
        self._accumulate_stats(timestamp, motion_detected, score, detection_fraction)

        if timestamp - self._chunk_start_ts >= self.config.max_chunk_seconds:
            self._roll_chunk(timestamp)

    def _accumulate_bbox(self, boxes: Sequence[BoundingBox]) -> None:
        for x, y, w, h in boxes:
            box = (x, y, x + w, y + h)
            if self._chunk_bbox is None:
                self._chunk_bbox = box
            else:
                x1, y1, x2, y2 = self._chunk_bbox
                bx1, by1, bx2, by2 = box
                self._chunk_bbox = (min(x1, bx1), min(y1, by1), max(x2, bx2), max(y2, by2))

    def _accumulate_stats(self, timestamp: float, motion_detected: bool, score: float, detection_fraction: float) -> None:
        self._chunk_frame_count += 1

        # Only measured between consecutive *live* frames, so pre-buffer
        # frames (prepended in bulk in _open_chunk, before any per-frame
        # motion status is available for them) never contribute -- they're
        # by definition from before motion was confirmed.
        if self._chunk_prev_live_ts is not None and motion_detected:
            self._chunk_motion_seconds += max(timestamp - self._chunk_prev_live_ts, 0.0)
        self._chunk_prev_live_ts = timestamp

        if motion_detected:
            self._chunk_motion_frame_count += 1
            self._chunk_score_sum += score
            self._chunk_score_count += 1
            self._chunk_score_max = max(self._chunk_score_max, score)

        self._chunk_max_detection_fraction = max(self._chunk_max_detection_fraction, detection_fraction)

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
        self._chunk_bbox = None
        self._recording = True

        self._chunk_content_start_ts = name_ts
        self._chunk_last_frame_ts = name_ts
        self._chunk_prev_live_ts = None
        self._chunk_frame_count = 0
        self._chunk_motion_frame_count = 0
        self._chunk_score_sum = 0.0
        self._chunk_score_count = 0
        self._chunk_score_max = 0.0
        self._chunk_max_detection_fraction = 0.0
        self._chunk_motion_seconds = 0.0

        logger.info("Starting recording chunk: %s (%d prepended frames)", name, len(prepend_frames))

        for tf in prepend_frames:
            self._write_frame_raw(tf.frame)
            self._chunk_last_frame_ts = tf.timestamp

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
            final_path = self._final_path
            self._temp_path.rename(final_path)
            logger.info("Finalized recording: %s", final_path.name)
            self._log_event(final_path.name)
            self._write_metadata(final_path)
        self._temp_path = None
        self._final_path = None
        self._chunk_bbox = None

    def _log_event(self, clip_name: str) -> None:
        if self.config.event_log_path is None or self._chunk_bbox is None:
            return
        x1, y1, x2, y2 = self._chunk_bbox
        entry = {
            "timestamp": time.time(),
            "camera": self.config.camera_name,
            "clip": clip_name,
            "bbox": [x1, y1, x2 - x1, y2 - y1],
        }
        try:
            self.config.event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.config.event_log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            logger.exception("Failed to append motion event log entry")

    def _write_metadata(self, video_path: Path) -> None:
        """Writes <video_path stem>.json alongside the clip: a companion
        record with everything needed to review this event later without
        opening the video itself."""
        start_ts = self._chunk_content_start_ts
        end_ts = self._chunk_last_frame_ts if self._chunk_last_frame_ts is not None else start_ts
        if start_ts is None:
            return

        mean_score = self._chunk_score_sum / self._chunk_score_count if self._chunk_score_count else 0.0
        motion_frame_ratio = (
            self._chunk_motion_frame_count / self._chunk_frame_count if self._chunk_frame_count else 0.0
        )

        metadata = {
            "event_id": video_path.stem,
            "camera_id": self.config.camera_name,
            "start_time": datetime.fromtimestamp(start_ts).astimezone().isoformat(),
            "end_time": datetime.fromtimestamp(end_ts).astimezone().isoformat(),
            "video_path": str(video_path.resolve()),
            "motion_confidence": {
                "mean_score": round(mean_score, 4),
                "max_score": round(self._chunk_score_max, 4),
                "motion_frame_ratio": round(motion_frame_ratio, 4),
            },
            "motion_time": round(self._chunk_motion_seconds, 4),
            "detection_size": round(self._chunk_max_detection_fraction, 4),
        }

        metadata_path = video_path.with_suffix(".json")
        try:
            tmp_path = metadata_path.with_name(metadata_path.stem + ".tmp.json")
            with tmp_path.open("w") as f:
                json.dump(metadata, f, indent=2)
            tmp_path.replace(metadata_path)
        except OSError:
            logger.exception("Failed to write metadata for %s", video_path.name)

    def flush_on_shutdown(self) -> None:
        """Cleanly close any in-progress recording, e.g. during process shutdown."""
        if self._recording:
            self._finish_event()
