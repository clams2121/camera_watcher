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
from typing import Optional

import numpy as np

from .capture import RtspCapture
from .config import Config
from .frame_buffer import FrameBuffer
from .mask import MaskStore
from .motion import MotionDetector
from .recorder import TEMP_SUFFIX, RecorderConfig, SegmentRecorder
from .retention import RetentionConfig, enforce_retention

logger = logging.getLogger(__name__)


class CameraPipeline:
    """Owns the capture thread, motion detector, recorder, and retention sweep for one camera."""

    def __init__(self, config: Config):
        self.config = config
        settings = config.settings

        rec = settings["recording"]
        buffer_seconds = max(rec["pre_buffer_seconds"], rec["overlap_seconds"]) + 2
        self.frame_buffer = FrameBuffer(max_seconds=buffer_seconds)

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

        self.recorder = SegmentRecorder(self.frame_buffer, self._recorder_config(settings))

        self.capture = RtspCapture(
            url_factory=self.config.rtsp_url,
            buffer=self.frame_buffer,
            transport=settings["camera"]["transport"],
            on_frame=self._on_frame,
        )

        self._fps_samples: list = []
        self._last_frame_ts: Optional[float] = None
        self._retention_stop = threading.Event()
        self._retention_thread: Optional[threading.Thread] = None

        # Bounded so a stalled disk (or a burst the processing thread can't
        # keep up with) sheds frames instead of growing memory without limit.
        self._frame_queue: "queue.Queue" = queue.Queue(maxsize=128)
        self._process_stop = threading.Event()
        self._process_thread: Optional[threading.Thread] = None
        self._dropped_frames = 0

    def _recorder_config(self, settings: dict) -> RecorderConfig:
        rec = settings["recording"]
        return RecorderConfig(
            output_dir=Path(rec["output_dir"]),
            pre_buffer_seconds=rec["pre_buffer_seconds"],
            post_buffer_seconds=rec["post_buffer_seconds"],
            max_chunk_seconds=rec["max_chunk_seconds"],
            overlap_seconds=rec["overlap_seconds"],
            fourcc=rec["fourcc"],
            max_width=rec["max_width"],
            camera_name=settings["camera"]["name"],
        )

    def _on_frame(self, timestamp: float, frame: np.ndarray) -> None:
        """Called directly on the capture thread for every frame read -- must stay cheap.

        Motion detection and recording (which can block on disk I/O) happen
        on the separate processing thread instead, so a slow write can never
        stall the RTSP read loop.
        """
        if self._last_frame_ts is not None:
            interval = timestamp - self._last_frame_ts
            if 0 < interval < 1:
                self._fps_samples.append(1.0 / interval)
                if len(self._fps_samples) >= 30:
                    self.recorder.set_fps_hint(sum(self._fps_samples) / len(self._fps_samples))
                    self._fps_samples.clear()
        self._last_frame_ts = timestamp

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
            if self._motion_enabled:
                try:
                    motion_detected = self.motion_detector.process(frame).motion_detected
                except Exception:
                    logger.exception("Motion detection failed on a frame")

            try:
                self.recorder.handle_frame(timestamp, frame, motion_detected)
            except Exception:
                logger.exception("Recording failed on a frame")

    def _cleanup_orphaned_temp_files(self) -> None:
        output_dir = Path(self.config.settings["recording"]["output_dir"])
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
        self._process_stop.clear()
        self._process_thread = threading.Thread(target=self._process_loop, name="frame-processor", daemon=True)
        self._process_thread.start()
        self.capture.start()
        self._start_retention_thread()

    def stop(self) -> None:
        self._retention_stop.set()
        if self._retention_thread:
            self._retention_thread.join(timeout=5)
        # Stop reading new frames first, then let the processing thread drain
        # whatever's still queued (it keeps pulling from the queue until it's
        # empty even after _process_stop is set) before finalizing the recorder.
        self.capture.stop()
        self._process_stop.set()
        if self._process_thread:
            self._process_thread.join(timeout=5)
        self.recorder.flush_on_shutdown()

    def apply_settings(self, settings: dict) -> None:
        """Re-apply settings changed via the web UI, without restarting the process."""
        motion_cfg = settings["motion"]
        self._motion_enabled = motion_cfg["enabled"]
        self.motion_detector.configure(
            analysis_width=motion_cfg["analysis_width"],
            min_area=motion_cfg["min_area"],
            var_threshold=motion_cfg["var_threshold"],
            history=motion_cfg["history"],
        )
        self.recorder.config = self._recorder_config(settings)
        rec = settings["recording"]
        self.frame_buffer.set_max_seconds(max(rec["pre_buffer_seconds"], rec["overlap_seconds"]) + 2)
        self.capture.set_transport(settings["camera"]["transport"])
        self.capture.restart()

    def reload_mask(self) -> None:
        self.mask_store.reload()

    def _start_retention_thread(self) -> None:
        def _run():
            while not self._retention_stop.is_set():
                settings = self.config.settings
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
                interval = max(retention_cfg.get("check_interval_seconds", 3600), 60)
                if self._retention_stop.wait(interval):
                    break

        self._retention_thread = threading.Thread(target=_run, name="retention", daemon=True)
        self._retention_thread.start()

    def status(self) -> dict:
        return {
            "connected": self.capture.connected,
            "recording": self.recorder.is_recording,
            "buffered_seconds": self.frame_buffer.max_seconds,
            "buffered_frames": len(self.frame_buffer),
            "pending_frames": self._frame_queue.qsize(),
            "dropped_frames": self._dropped_frames,
        }
