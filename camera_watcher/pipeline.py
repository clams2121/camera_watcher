"""Wires capture, motion detection, recording, and retention into one running service.

Threading model: each stage runs on its own thread so a slow one never stalls
another --

- ``RtspCapture`` only reads frames off the socket and appends them to the
  shared pre-roll buffer -- nothing here should ever block on disk I/O.
- The frame-processing thread (``_process_loop``) pulls frames off a queue
  and does the actually-slow work: motion detection and writing video to
  disk. It's decoupled from capture via ``_frame_queue`` so a slow disk
  write never backs up the RTSP read loop.
- The retention sweep runs on its own timer thread.
- The Flask web UI runs on the main thread (via a threaded WSGI server), so
  it keeps answering requests regardless of what the other threads are
  doing -- it never touches the camera directly, only the shared,
  thread-safe ``frame_buffer`` and ``Config``.
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .accumulator import MotionAccumulator
from .capture import RtspCapture
from .config import Config
from .frame_buffer import FrameBuffer
from .mask import MaskStore
from .motion import MotionDetector
from .recorder import TEMP_SUFFIX, BoundingBox, RecorderConfig, SegmentRecorder
from .retention import RetentionConfig, enforce_retention
from .segment_cache import SegmentCache, SegmentCacheConfig

logger = logging.getLogger(__name__)

# BGR -- OpenCV's channel order -- for a high-contrast, easy-to-spot box.
_BOX_COLOR = (0, 255, 0)  # bright green

# The sub-stream frame buffer only feeds live preview/snapshot now --
# recording pre-roll comes entirely from the passthrough segment cache (see
# segment_cache.py) -- so it just needs enough headroom to smooth over
# normal frame-interval jitter, not any particular pre/post-buffer length.
_PREVIEW_BUFFER_SECONDS = 5.0


class CameraPipeline:
    """Owns the capture thread, motion detector, recorder, segment cache, and
    retention sweep for one camera."""

    def __init__(self, config: Config):
        self.config = config
        settings = config.resolved()

        self.frame_buffer = FrameBuffer(max_seconds=_PREVIEW_BUFFER_SECONDS)

        self.mask_store = MaskStore(Path(settings["mask"]["path"]))

        motion_cfg = settings["motion"]
        self.motion_detector = MotionDetector(
            self.mask_store,
            analysis_width=motion_cfg["analysis_width"],
            min_area=motion_cfg["min_area"],
            var_threshold=motion_cfg["var_threshold"],
            history=motion_cfg["history"],
        )
        self._motion_enabled = motion_cfg["enabled"]
        self._draw_bounding_box = motion_cfg["draw_bounding_box"]
        self._box_padding_px = motion_cfg["box_padding_px"]
        self._heatmap_path = Path(motion_cfg["heatmap_path"])
        self.accumulator: Optional[MotionAccumulator] = None

        # Live-preview-only bounding boxes from the most recently analyzed
        # frame -- never baked into recorded video (passthrough recording
        # never touches frame content at all). Guarded by _boxes_lock since
        # the frame-processing thread writes it and the Flask stream route
        # reads it from a different thread.
        self._boxes_lock = threading.Lock()
        self._latest_boxes: List[BoundingBox] = []

        self.segment_cache = SegmentCache(
            url_factory=lambda: self.config.rtsp_url("main"),
            config=self._segment_cache_config(settings),
        )
        self.recorder = SegmentRecorder(self.segment_cache, self._recorder_config(settings))

        self.capture = RtspCapture(
            url_factory=lambda: self.config.rtsp_url("sub"),
            buffer=self.frame_buffer,
            transport=settings["camera"]["transport"],
            on_frame=self._on_frame,
        )

        self._retention_stop = threading.Event()
        self._retention_thread: Optional[threading.Thread] = None

        # Bounded so a stalled disk (or a burst the processing thread can't
        # keep up with) sheds frames instead of growing memory without limit.
        self._frame_queue: "queue.Queue" = queue.Queue(maxsize=128)
        self._process_stop = threading.Event()
        self._process_thread: Optional[threading.Thread] = None
        self._dropped_frames = 0

        self._prune_stop = threading.Event()
        self._prune_thread: Optional[threading.Thread] = None

    def _recorder_config(self, settings: dict) -> RecorderConfig:
        rec = settings["recording"]
        event_log_path = rec.get("event_log_path") or None
        return RecorderConfig(
            output_dir=Path(rec["output_dir"]),
            pre_buffer_seconds=rec["pre_buffer_seconds"],
            post_buffer_seconds=rec["post_buffer_seconds"],
            max_chunk_seconds=rec["max_chunk_seconds"],
            overlap_seconds=rec["overlap_seconds"],
            camera_name=settings["camera"]["name"],
            event_log_path=Path(event_log_path) if event_log_path else None,
        )

    def _segment_cache_config(self, settings: dict) -> SegmentCacheConfig:
        rec = settings["recording"]
        return SegmentCacheConfig(
            cache_dir=Path(rec["cache_dir"]),
            segment_seconds=rec["segment_seconds"],
            transport=settings["camera"]["transport"],
        )

    def _cache_keep_seconds(self) -> float:
        rec_cfg = self.recorder.config
        # Keep enough lookback to open a fresh event's pre-buffer at any
        # moment, plus real margin for the assembler's wait-for-segment-
        # rollover step and general scheduling jitter.
        return rec_cfg.pre_buffer_seconds + self.segment_cache.config.segment_seconds * 5 + 10

    def _on_frame(self, timestamp: float, frame: np.ndarray) -> None:
        """Called directly on the capture thread for every frame read -- must stay cheap.

        Motion detection and recording bookkeeping happen on the separate
        processing thread instead, so a slow disk (writing metadata, say)
        can never stall the RTSP read loop.
        """
        try:
            self._frame_queue.put_nowait((timestamp, frame))
        except queue.Full:
            self._dropped_frames += 1
            if self._dropped_frames == 1 or self._dropped_frames % 50 == 0:
                logger.warning(
                    "Frame processing is falling behind; dropped %d frame(s) so far",
                    self._dropped_frames,
                )

    def _process_loop(self) -> None:
        while True:
            try:
                timestamp, frame = self._frame_queue.get(timeout=0.2)
            except queue.Empty:
                if self._process_stop.is_set():
                    return
                continue

            motion_detected = False
            boxes: List[BoundingBox] = []
            score = 0
            detection_fraction = 0.0
            if self._motion_enabled:
                try:
                    result = self.motion_detector.process(frame)
                    motion_detected = result.motion_detected
                    score = result.score
                    if result.raw_foreground is not None:
                        self._accumulate_heatmap(result.raw_foreground, result.analysis_size)
                    if result.boxes:
                        boxes = self._scale_boxes(result.boxes, result.analysis_size, frame.shape)
                        detection_fraction = self._max_detection_fraction(result.boxes, result.analysis_size)
                except Exception:
                    logger.exception("Motion detection failed on a frame")

            # Boxes are preview-only now -- passthrough recording never
            # decodes frame content, so there's nothing to burn them into.
            # get_stream() (web/routes.py) reads this to draw them at serve
            # time instead.
            with self._boxes_lock:
                self._latest_boxes = boxes if (motion_detected and boxes and self._draw_bounding_box) else []

            try:
                self.recorder.handle_frame(
                    timestamp,
                    motion_detected,
                    boxes,
                    score=score,
                    detection_fraction=detection_fraction,
                )
            except Exception:
                logger.exception("Recording failed on a frame")

    def _max_detection_fraction(self, boxes: List[BoundingBox], analysis_size: Tuple[int, int]) -> float:
        """Max fraction of the analyzed frame's area any single detected
        contour covered -- computed from the raw (unpadded) boxes, at
        analysis resolution. Area *ratio* is scale-invariant since motion.py
        preserves aspect ratio when downscaling, so this equals the fraction
        at full frame resolution too, without needing to convert."""
        analysis_w, analysis_h = analysis_size
        frame_area = analysis_w * analysis_h
        if frame_area <= 0 or not boxes:
            return 0.0
        return max((w * h) / frame_area for _, _, w, h in boxes)

    def _accumulate_heatmap(self, raw_foreground: np.ndarray, analysis_size: Tuple[int, int]) -> None:
        width, height = analysis_size
        if width <= 0 or height <= 0:
            return
        if self.accumulator is None:
            self.accumulator = MotionAccumulator(self._heatmap_path, width, height)
        try:
            self.accumulator.add(raw_foreground)
        except Exception:
            logger.exception("Failed to update the motion heatmap accumulator")

    def _scale_boxes(
        self, boxes: List[BoundingBox], analysis_size: Tuple[int, int], frame_shape: tuple
    ) -> List[BoundingBox]:
        analysis_w, analysis_h = analysis_size
        if analysis_w <= 0 or analysis_h <= 0:
            return []
        frame_h, frame_w = frame_shape[:2]
        scale_x, scale_y = frame_w / analysis_w, frame_h / analysis_h
        pad = self._box_padding_px
        scaled = []
        for x, y, w, h in boxes:
            x1 = max(0, int(x * scale_x) - pad)
            y1 = max(0, int(y * scale_y) - pad)
            x2 = min(frame_w, int((x + w) * scale_x) + pad)
            y2 = min(frame_h, int((y + h) * scale_y) + pad)
            scaled.append((x1, y1, x2 - x1, y2 - y1))
        return scaled

    def _draw_boxes(self, frame: np.ndarray, boxes: List[BoundingBox]) -> np.ndarray:
        # Draw on a copy: `frame` is the same array sitting in the shared
        # frame buffer, and mutating it in place would bake this box into
        # every future preview frame served from that buffer entry.
        annotated = frame.copy()
        for x, y, w, h in boxes:
            cv2.rectangle(annotated, (x, y), (x + w, y + h), _BOX_COLOR, 2)
        return annotated

    def frame_for_preview(self, frame: np.ndarray) -> np.ndarray:
        """Returns `frame` with the most recently detected motion boxes
        burned in, if bounding-box display is enabled -- used only by the
        live MJPEG preview (web/routes.py's get_stream). Never applied to
        recorded clips: passthrough recording never decodes frame content in
        the first place, so there's nothing here to affect it."""
        with self._boxes_lock:
            boxes = list(self._latest_boxes)
        if not boxes:
            return frame
        return self._draw_boxes(frame, boxes)

    def _cleanup_orphaned_temp_files(self) -> None:
        output_dir = Path(self.config.resolved()["recording"]["output_dir"])
        if not output_dir.exists():
            return
        for p in output_dir.iterdir():
            if p.is_file() and p.name.endswith(TEMP_SUFFIX):
                logger.warning("Removing incomplete recording from a previous run: %s", p.name)
                try:
                    p.unlink()
                except OSError:
                    logger.exception("Failed to remove orphaned temp file %s", p)

    def start(self) -> None:
        self._cleanup_orphaned_temp_files()
        self.segment_cache.start()
        self.recorder.start()
        self._process_stop.clear()
        self._process_thread = threading.Thread(target=self._process_loop, name="frame-processor", daemon=True)
        self._process_thread.start()
        self.capture.start()
        self._start_retention_thread()
        self._start_prune_thread()

    def stop(self) -> None:
        self._retention_stop.set()
        if self._retention_thread:
            self._retention_thread.join(timeout=5)
        self._prune_stop.set()
        if self._prune_thread:
            self._prune_thread.join(timeout=5)
        # Stop reading new frames first, then let the processing thread drain
        # whatever's still queued (it keeps pulling from the queue until it's
        # empty even after _process_stop is set) before finalizing the recorder.
        self.capture.stop()
        self._process_stop.set()
        if self._process_thread:
            self._process_thread.join(timeout=5)
        # recorder.stop() finishes any in-progress clip synchronously, so the
        # segment cache backing it must still be running when it's called.
        self.recorder.stop()
        self.segment_cache.stop()
        if self.accumulator is not None:
            self.accumulator.save()

    def apply_settings(self, settings: dict) -> None:
        """Re-apply settings changed via the web UI, without restarting the process."""
        motion_cfg = settings["motion"]
        self._motion_enabled = motion_cfg["enabled"]
        self._draw_bounding_box = motion_cfg["draw_bounding_box"]
        self._box_padding_px = motion_cfg["box_padding_px"]
        self.motion_detector.configure(
            analysis_width=motion_cfg["analysis_width"],
            min_area=motion_cfg["min_area"],
            var_threshold=motion_cfg["var_threshold"],
            history=motion_cfg["history"],
        )

        new_heatmap_path = Path(motion_cfg["heatmap_path"])
        if new_heatmap_path != self._heatmap_path:
            self._heatmap_path = new_heatmap_path
            self.accumulator = None  # recreated lazily on the next frame, at the (possibly new) path
        if self.accumulator is not None:
            self.accumulator.save()
            self.accumulator = None  # analysis resolution may have changed too; recreate lazily

        self.recorder.config = self._recorder_config(settings)
        self.segment_cache.config = self._segment_cache_config(settings)
        self.segment_cache.restart()
        self.capture.set_transport(settings["camera"]["transport"])
        self.capture.restart()

    def reload_mask(self) -> None:
        self.mask_store.reload()

    def heatmap_png(self) -> Optional[bytes]:
        """PNG bytes for the current motion heatmap overlay, or None before any frame's been analyzed."""
        if self.accumulator is None:
            return None
        return self.accumulator.heatmap_png()

    def reset_heatmap(self) -> None:
        if self.accumulator is not None:
            self.accumulator.reset()

    def _start_retention_thread(self) -> None:
        def _run():
            while not self._retention_stop.is_set():
                settings = self.config.resolved()
                retention_cfg = settings["retention"]
                if retention_cfg["enabled"]:
                    try:
                        enforce_retention(
                            RetentionConfig(
                                output_dir=Path(settings["recording"]["output_dir"]),
                                max_age_days=retention_cfg["max_age_days"],
                                max_total_gb=retention_cfg["max_total_gb"],
                            )
                        )
                    except Exception:
                        logger.exception("Retention sweep failed")
                # Piggyback the heatmap accumulator's periodic persistence on
                # this same timer rather than running a whole extra thread
                # for it -- it's also saved on clean shutdown and on reset.
                if self.accumulator is not None:
                    try:
                        self.accumulator.save()
                    except Exception:
                        logger.exception("Failed to persist the motion heatmap accumulator")
                interval = max(retention_cfg.get("check_interval_seconds", 3600), 60)
                if self._retention_stop.wait(interval):
                    break

        self._retention_thread = threading.Thread(target=_run, name="retention", daemon=True)
        self._retention_thread.start()

    def _start_prune_thread(self) -> None:
        # Separate, short-interval timer from the retention sweep above --
        # the passthrough segment cache is a small rolling buffer that needs
        # pruning every few seconds, not something swept once an hour.
        def _run():
            while not self._prune_stop.is_set():
                try:
                    self.segment_cache.prune(self._cache_keep_seconds())
                except Exception:
                    logger.exception("Passthrough segment cache pruning failed")
                if self._prune_stop.wait(max(self.segment_cache.config.segment_seconds, 1.0)):
                    break

        self._prune_thread = threading.Thread(target=_run, name="cache-pruner", daemon=True)
        self._prune_thread.start()

    def status(self) -> dict:
        status = {
            "connected": self.capture.connected,
            "recording": self.recorder.is_recording,
            "buffered_seconds": self.frame_buffer.max_seconds,
            "buffered_frames": len(self.frame_buffer),
            "pending_frames": self._frame_queue.qsize(),
            "dropped_frames": self._dropped_frames,
        }
        status.update(self.segment_cache.status())
        return status
