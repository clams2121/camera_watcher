"""A small, thread-safe ring buffer of recent (timestamp, frame) pairs.

Serves two purposes at once: the pre-roll source a new recording starts
from (see recorder.py), and the basis for measuring the stream's actual
frame rate (VideoWriter needs a real fps, not a guess, to produce a
correctly-timed file).
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, List, Optional, Tuple

FrameEntry = Tuple[float, "object"]  # (timestamp, frame)


class FrameBuffer:
    def __init__(self, max_seconds: float):
        self.max_seconds = max_seconds
        self._lock = threading.Lock()
        self._frames: Deque[FrameEntry] = deque()

    def append(self, timestamp: float, frame) -> None:
        with self._lock:
            self._frames.append((timestamp, frame))
            cutoff = timestamp - self.max_seconds
            while self._frames and self._frames[0][0] < cutoff:
                self._frames.popleft()

    def latest(self) -> Optional[FrameEntry]:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def since(self, start_ts: float) -> List[FrameEntry]:
        """All buffered frames with timestamp >= start_ts, oldest first."""
        with self._lock:
            return [(ts, f) for ts, f in self._frames if ts >= start_ts]

    def measured_fps(self) -> Optional[float]:
        """Average fps across everything currently buffered, or None if
        there isn't enough history yet to say."""
        with self._lock:
            if len(self._frames) < 2:
                return None
            span = self._frames[-1][0] - self._frames[0][0]
            if span <= 0:
                return None
            return (len(self._frames) - 1) / span

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)
