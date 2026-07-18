"""Motion-triggered clip recorder -- passthrough edition.

Where the old recorder wrote decoded frames straight into a ``cv2.VideoWriter``,
this one never touches frame content for recording at all: :class:`~camera_watcher.segment_cache.SegmentCache`
is continuously stream-copying the camera's main RTSP stream into short
cached segments in the background, and all this class does is decide *when*
a motion event starts and ends and hand the resulting time window off to a
background assembler thread, which stitches the relevant cached segments
into the final clip via the concat demuxer (also ``-c copy`` -- zero
re-encoding, so the recorded clip's codec/resolution/bitrate exactly match
what the camera sent).

State machine, driven by calling :meth:`SegmentRecorder.handle_frame` once
per analyzed frame from a single thread (the frame-processing thread -- no
frame content is passed any more, just its timestamp and the motion
detector's verdict for it):

- IDLE, no motion: nothing happens.
- Motion detected: a new event opens, its window starting
  ``pre_buffer_seconds`` before this timestamp.
- The event's window keeps extending through a ``post_buffer_seconds``
  cooldown after the last detected motion, so a brief gap in detection
  doesn't fragment one real event into multiple clips.
- An event longer than ``max_chunk_seconds`` is force-split into a new
  window, carrying the last ``overlap_seconds`` into the start of the next
  one so nothing is lost across the cut.
- Every closed window is handed to a background assembler thread -- ffmpeg
  concat calls never block the frame-processing hot path -- which stitches
  the relevant cached segments into the final clip under a temporary name
  and atomically renames it only once fully written.
- While a window is open, or its assembly job hasn't finished yet, the
  segment cache is told never to prune anything inside it -- see
  ``SegmentCache.protect_since``.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .assemble import AssemblyError, assemble_clip
from .constants import TEMP_SUFFIX
from .segment_cache import SegmentCache

logger = logging.getLogger(__name__)

BoundingBox = Tuple[int, int, int, int]  # (x, y, w, h)


@dataclass
class RecorderConfig:
    output_dir: Path
    pre_buffer_seconds: float = 10
    post_buffer_seconds: float = 10
    max_chunk_seconds: float = 180
    overlap_seconds: float = 5
    camera_name: str = "camera1"
    event_log_path: Optional[Path] = None  # JSONL log of each clip's motion bounding box; None disables it


def _timestamp_name(camera_name: str, ts: float, suffix: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
    return f"{camera_name}_{stamp}{suffix}"


@dataclass
class _Window:
    """Per-chunk state: the time window to assemble, plus the same motion
    stats the old recorder accumulated per-frame, still fed by the
    frame-processing thread's motion detector output."""

    content_start_ts: float
    chunk_start_ts: float
    bbox: Optional[Tuple[int, int, int, int]] = None
    frame_count: int = 0
    motion_frame_count: int = 0
    score_sum: float = 0.0
    score_count: int = 0
    score_max: float = 0.0
    max_detection_fraction: float = 0.0
    motion_seconds: float = 0.0
    prev_live_ts: Optional[float] = None
    last_frame_ts: Optional[float] = None


class SegmentRecorder:
    """``handle_frame`` is not thread-safe on its own -- call it from a
    single thread (the frame-processing thread). Assembly happens on its own
    background thread, started/stopped via ``start``/``stop``."""

    def __init__(self, segment_cache: SegmentCache, config: RecorderConfig):
        self._cache = segment_cache
        self.config = config

        self._recording = False
        self._window: Optional[_Window] = None
        self._last_motion_ts: Optional[float] = None

        self._state_lock = threading.Lock()
        self._active_starts: List[float] = []  # open window's start + any not-yet-assembled jobs' starts

        self._job_queue: "queue.Queue" = queue.Queue()
        self._assembler_stop = threading.Event()
        self._assembler_thread: Optional[threading.Thread] = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        self._assembler_stop.clear()
        self._assembler_thread = threading.Thread(target=self._assemble_loop, name="clip-assembler", daemon=True)
        self._assembler_thread.start()

    def stop(self) -> None:
        """Finishes any in-progress event synchronously, then lets the
        assembler thread drain whatever's already queued before stopping."""
        self.flush_on_shutdown()
        self._assembler_stop.set()
        if self._assembler_thread:
            self._assembler_thread.join(timeout=60)

    def handle_frame(
        self,
        timestamp: float,
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
            self._open_window(timestamp)
        else:
            post_buffer_expired = (
                self._last_motion_ts is not None
                and timestamp - self._last_motion_ts > self.config.post_buffer_seconds
            )
            if post_buffer_expired:
                self._finish_event(self._last_motion_ts + self.config.post_buffer_seconds)
                return

        self._accumulate(timestamp, motion_detected, boxes, score, detection_fraction)

        if timestamp - self._window.chunk_start_ts >= self.config.max_chunk_seconds:
            self._roll_chunk(timestamp)

    def _open_window(self, boundary_ts: float) -> None:
        content_start = boundary_ts - self.config.pre_buffer_seconds
        self._window = _Window(content_start_ts=content_start, chunk_start_ts=boundary_ts, last_frame_ts=boundary_ts)
        self._recording = True
        self._register_active_start(content_start)
        logger.info("Motion event started (window opens %.1fs before trigger)", self.config.pre_buffer_seconds)

    def _accumulate(
        self,
        timestamp: float,
        motion_detected: bool,
        boxes: Sequence[BoundingBox],
        score: float,
        detection_fraction: float,
    ) -> None:
        w = self._window
        for x, y, bw, bh in boxes:
            box = (x, y, x + bw, y + bh)
            if w.bbox is None:
                w.bbox = box
            else:
                x1, y1, x2, y2 = w.bbox
                bx1, by1, bx2, by2 = box
                w.bbox = (min(x1, bx1), min(y1, by1), max(x2, bx2), max(y2, by2))

        w.frame_count += 1
        if w.prev_live_ts is not None and motion_detected:
            w.motion_seconds += max(timestamp - w.prev_live_ts, 0.0)
        w.prev_live_ts = timestamp
        w.last_frame_ts = timestamp

        if motion_detected:
            w.motion_frame_count += 1
            w.score_sum += score
            w.score_count += 1
            w.score_max = max(w.score_max, score)

        w.max_detection_fraction = max(w.max_detection_fraction, detection_fraction)

    def _roll_chunk(self, timestamp: float) -> None:
        old_window = self._window
        new_content_start = timestamp - self.config.overlap_seconds
        # Register the new chunk's start *before* handing the old one off to
        # the assembler, so the cache is never briefly unprotected between
        # the two.
        self._register_active_start(new_content_start)
        self._enqueue_assembly(old_window, timestamp)
        self._window = _Window(content_start_ts=new_content_start, chunk_start_ts=timestamp, last_frame_ts=timestamp)

    def _finish_event(self, end_ts: float) -> None:
        window = self._window
        self._enqueue_assembly(window, end_ts)
        self._recording = False
        self._window = None
        self._last_motion_ts = None

    def _register_active_start(self, start_ts: float) -> None:
        with self._state_lock:
            self._active_starts.append(start_ts)
            self._cache.protect_since(min(self._active_starts))

    def _release_active_start(self, start_ts: float) -> None:
        with self._state_lock:
            self._active_starts.remove(start_ts)
            self._cache.protect_since(min(self._active_starts) if self._active_starts else None)

    def _enqueue_assembly(self, window: _Window, end_ts: float) -> None:
        end_ts = max(end_ts, window.last_frame_ts or end_ts)
        self._job_queue.put((window, end_ts))

    def _assemble_loop(self) -> None:
        while True:
            try:
                item = self._job_queue.get(timeout=0.5)
            except queue.Empty:
                if self._assembler_stop.is_set():
                    return
                continue
            window, end_ts = item
            try:
                self._process_job(window, end_ts)
            except Exception:
                logger.exception("Failed to assemble a recorded clip")
            finally:
                self._release_active_start(window.content_start_ts)

    def _process_job(self, window: _Window, end_ts: float) -> None:
        # The cached segment covering `end_ts` may still be actively being
        # written by ffmpeg (it writes each segment file in place until the
        # next segment boundary) -- wait for a newer segment to appear before
        # assembling, without blocking the frame-processing thread (we're on
        # our own thread here).
        segment_seconds = self._cache.config.segment_seconds
        deadline = time.time() + segment_seconds + 2.0
        while time.time() < deadline and not self._assembler_stop.is_set():
            newest = self._cache.newest_segment_start()
            if newest is not None and newest > end_ts:
                break
            time.sleep(0.25)

        segments = self._cache.list_segments(window.content_start_ts, end_ts)
        if not segments:
            logger.error(
                "No cached segments covered the window [%.3f, %.3f) -- dropping this event "
                "(cache_dir may be too small for pre_buffer_seconds, or the passthrough recorder "
                "was disconnected for the whole event)",
                window.content_start_ts,
                end_ts,
            )
            return

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        name = _timestamp_name(self.config.camera_name, window.content_start_ts, ".mp4")
        final_path = self.config.output_dir / name
        temp_path = self.config.output_dir / (name + TEMP_SUFFIX)

        try:
            assemble_clip(segments, temp_path)
        except AssemblyError:
            logger.exception("Clip assembly failed for %s", name)
            temp_path.unlink(missing_ok=True)
            return

        temp_path.rename(final_path)
        logger.info("Finalized recording: %s (%d segment(s))", final_path.name, len(segments))
        self._log_event(final_path.name, window)
        self._write_metadata(final_path, window, end_ts)

    def _log_event(self, clip_name: str, window: _Window) -> None:
        if self.config.event_log_path is None or window.bbox is None:
            return
        x1, y1, x2, y2 = window.bbox
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

    def _write_metadata(self, video_path: Path, window: _Window, end_ts: float) -> None:
        """Writes <video_path stem>.json alongside the clip: a companion
        record with everything needed to review this event later without
        opening the video itself."""
        start_ts = window.content_start_ts
        mean_score = window.score_sum / window.score_count if window.score_count else 0.0
        motion_frame_ratio = window.motion_frame_count / window.frame_count if window.frame_count else 0.0

        metadata = {
            "event_id": video_path.stem,
            "camera_id": self.config.camera_name,
            "start_time": datetime.fromtimestamp(start_ts).astimezone().isoformat(),
            "end_time": datetime.fromtimestamp(end_ts).astimezone().isoformat(),
            "video_path": str(video_path.resolve()),
            "motion_confidence": {
                "mean_score": round(mean_score, 4),
                "max_score": round(window.score_max, 4),
                "motion_frame_ratio": round(motion_frame_ratio, 4),
            },
            "motion_time": round(window.motion_seconds, 4),
            "detection_size": round(window.max_detection_fraction, 4),
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
        """Synchronously finishes any in-progress event so a clean shutdown
        doesn't drop the tail of a recording. Must run before the segment
        cache backing it is stopped."""
        if self._recording and self._window is not None:
            window = self._window
            end_ts = window.last_frame_ts or window.chunk_start_ts
            self._recording = False
            self._window = None
            self._last_motion_ts = None
            try:
                self._process_job(window, end_ts)
            except Exception:
                logger.exception("Failed to assemble the in-progress clip during shutdown")
            finally:
                self._release_active_start(window.content_start_ts)
