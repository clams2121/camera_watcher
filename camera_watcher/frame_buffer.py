"""Thread-safe, time-bounded ring buffer of recent camera frames."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import NamedTuple, Optional

import numpy as np


class TimedFrame(NamedTuple):
    timestamp: float
    frame: np.ndarray


class FrameBuffer:
    """Holds the most recent ``max_seconds`` of frames, trimmed by wall-clock age.

    Time-based rather than count-based so pre-roll length stays correct across
    variable RTSP frame rates -- a stall or a frame-rate change doesn't change
    how many seconds of buffer are available.
    """

    def __init__(self, max_seconds: float):
        self._max_seconds = max_seconds
        self._lock = threading.Lock()
        self._frames: deque[TimedFrame] = deque()
        self._latest: Optional[TimedFrame] = None

    @property
    def max_seconds(self) -> float:
        return self._max_seconds

    def set_max_seconds(self, max_seconds: float) -> None:
        with self._lock:
            self._max_seconds = max_seconds

    def append(self, frame: np.ndarray, timestamp: Optional[float] = None) -> None:
        timestamp = time.time() if timestamp is None else timestamp
        entry = TimedFrame(timestamp, frame)
        with self._lock:
            self._frames.append(entry)
            self._latest = entry
            cutoff = timestamp - self._max_seconds
            while self._frames and self._frames[0].timestamp < cutoff:
                self._frames.popleft()

    def latest(self) -> Optional[TimedFrame]:
        with self._lock:
            return self._latest

    def snapshot(self, seconds: Optional[float] = None) -> list[TimedFrame]:
        """Return a copy of buffered frames, optionally limited to the last ``seconds``."""
        with self._lock:
            if seconds is None:
                return list(self._frames)
            cutoff = time.time() - seconds
            return [tf for tf in self._frames if tf.timestamp >= cutoff]

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
