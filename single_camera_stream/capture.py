"""RTSP capture thread: reads frames continuously and hands each one to a
callback.

Reconnects with exponential backoff on read failure or disconnect. The
RTSP URL (which embeds credentials) is only ever held in-process via a
factory callable -- it is never logged.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

import cv2

logger = logging.getLogger(__name__)


class RtspCapture:
    """Owns a background thread that reads frames from an RTSP URL and
    calls `on_frame(timestamp, frame)` for each one."""

    def __init__(
        self,
        url_factory: Callable[[], str],
        on_frame: Callable[[float, "cv2.typing.MatLike"], None],
        transport: str = "tcp",
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
    ):
        self._url_factory = url_factory
        self._on_frame = on_frame
        self._transport = transport
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="rtsp-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _open_capture(self) -> "cv2.VideoCapture":
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{self._transport}"
        cap = cv2.VideoCapture(self._url_factory(), cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _run(self) -> None:
        delay = self._reconnect_initial_delay
        while not self._stop_event.is_set():
            cap = self._open_capture()
            if not cap.isOpened():
                logger.warning("Could not open RTSP stream, retrying in %.1fs", delay)
                cap.release()
                self._connected = False
                if self._stop_event.wait(delay):
                    break
                delay = min(delay * 2, self._reconnect_max_delay)
                continue

            logger.info("RTSP stream connected")
            self._connected = True
            delay = self._reconnect_initial_delay

            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    logger.warning("RTSP read failed, reconnecting")
                    break
                timestamp = time.time()
                try:
                    self._on_frame(timestamp, frame)
                except Exception:
                    logger.exception("on_frame callback raised")

            self._connected = False
            cap.release()
            if self._stop_event.is_set():
                break
            if self._stop_event.wait(delay):
                break
            delay = min(delay * 2, self._reconnect_max_delay)
