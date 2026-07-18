"""Passthrough recording source: a supervised ffmpeg subprocess stream-copies
the camera's main (full-resolution) RTSP stream into short, disk-resident
segments on a rolling basis. This cache *is* the pre-roll buffer for
recording -- see recorder.py, which slices it via :meth:`SegmentCache.list_segments`
and hands the slice to assemble.py to stitch into a final clip, all without
ever decoding a single frame.

ffmpeg is supervised here (not just spawned once) because an RTSP source can
drop the connection or hang without ffmpeg itself exiting -- both cases need
detecting and recovering from independently:

- process exit -> restart with exponential backoff.
- process alive but no new segment file appears for a while -> treat as
  stalled, kill and restart.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SEGMENT_STRFTIME = "%Y%m%d_%H%M%S"
_SEGMENT_NAME_RE = re.compile(r"^(\d{8}_\d{6})\.mp4$")

# ffmpeg happily echoes the input URL -- credentials and all -- in its own
# stderr diagnostics ("Input #0, rtsp, from 'rtsp://user:pass@host/path':").
# Scrub anything that looks like embedded userinfo before any of this ever
# reaches a log line.
_CREDENTIALS_RE = re.compile(r"://[^/@\s]+@")


def _redact(text: str) -> str:
    return _CREDENTIALS_RE.sub("://<redacted>@", text)


@dataclass
class SegmentCacheConfig:
    cache_dir: Path
    segment_seconds: float = 2.0
    transport: str = "tcp"
    # No new segment file within this many segment_seconds while the process
    # is still alive is treated as a stall (camera stopped sending data but
    # the TCP connection never dropped) and triggers a kill + restart.
    stall_multiplier: float = 4.0
    restart_backoff_seconds: Tuple[float, ...] = (1, 2, 5, 10, 30, 60)
    # A run that survives at least this long counts as "recovered" -- the
    # backoff resets to its first step instead of continuing to climb.
    healthy_run_seconds: float = 60.0


class SegmentCache:
    """Owns one supervised ffmpeg passthrough process for one camera's main stream."""

    def __init__(self, url_factory: Callable[[], str], config: SegmentCacheConfig):
        self._url_factory = url_factory
        self.config = config
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._connected = False
        self._restart_count = 0
        self._protected_since: Optional[float] = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def protect_since(self, ts: Optional[float]) -> None:
        """Tell the pruner to never delete a segment covering timestamp
        ``>= ts`` -- used while a motion event's window is open or its clip
        hasn't finished assembling yet. Pass ``None`` to release."""
        with self._lock:
            self._protected_since = ts

    def start(self) -> None:
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="segment-cache", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._process
        if proc is not None and proc.poll() is None:
            proc.terminate()
        if self._thread:
            self._thread.join(timeout=10)

    def restart(self) -> None:
        """Force a reconnect on the next iteration, e.g. after a settings
        change (new URL, transport, or cache_dir)."""
        with self._lock:
            proc = self._process
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _supervise(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            started_at = time.time()
            try:
                self._run_once()
            except Exception:
                logger.exception("Segment cache supervisor crashed unexpectedly running its ffmpeg subprocess")
            if self._stop.is_set():
                return

            with self._lock:
                self._connected = False
                self._restart_count += 1

            if time.time() - started_at >= self.config.healthy_run_seconds:
                attempt = 0
            backoff = self.config.restart_backoff_seconds[min(attempt, len(self.config.restart_backoff_seconds) - 1)]
            attempt += 1
            logger.warning("Passthrough recorder (main stream) exited; restarting in %.0fs", backoff)
            if self._stop.wait(backoff):
                return

    def _run_once(self) -> None:
        url = self._url_factory()
        pattern = str(self.config.cache_dir / f"{_SEGMENT_STRFTIME}.mp4")
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            self.config.transport,
            "-i",
            url,
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.config.segment_seconds),
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            pattern,
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        with self._lock:
            self._process = proc

        stderr_tail: list = []
        stderr_thread = threading.Thread(target=self._drain_stderr, args=(proc, stderr_tail), daemon=True)
        stderr_thread.start()

        started_at = time.time()
        try:
            while True:
                if self._stop.is_set():
                    proc.terminate()
                    break
                if proc.poll() is not None:
                    break
                newest_age = self._newest_segment_age(since=started_at)
                stall_after = self.config.segment_seconds * self.config.stall_multiplier
                if newest_age > stall_after:
                    logger.warning(
                        "Passthrough recorder produced no new segment for %.0fs (limit %.0fs) -- "
                        "killing and restarting",
                        newest_age,
                        stall_after,
                    )
                    proc.kill()
                    break
                with self._lock:
                    self._connected = True
                time.sleep(0.5)
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            stderr_thread.join(timeout=2)
            with self._lock:
                self._process = None

        if stderr_tail:
            logger.warning("ffmpeg (passthrough, main stream) stderr tail:\n%s", "\n".join(stderr_tail[-20:]))

    def _drain_stderr(self, proc: subprocess.Popen, sink: list) -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                sink.append(_redact(line.rstrip()))
        except ValueError:
            pass  # stream closed out from under us during shutdown

    def _iter_segments(self):
        """Yields (path, parsed_start_ts) for every well-formed segment file, unsorted."""
        try:
            entries = list(self.config.cache_dir.iterdir())
        except OSError:
            return
        for p in entries:
            m = _SEGMENT_NAME_RE.match(p.name)
            if not m:
                continue
            try:
                start_ts = time.mktime(time.strptime(m.group(1), _SEGMENT_STRFTIME))
            except ValueError:
                continue
            yield p, start_ts

    def _sorted_segments(self) -> List[Tuple[float, Path]]:
        return sorted(((ts, p) for p, ts in self._iter_segments()), key=lambda t: t[0])

    def newest_segment_start(self) -> Optional[float]:
        segments = self._sorted_segments()
        return segments[-1][0] if segments else None

    def _newest_segment_age(self, since: float) -> float:
        newest = self.newest_segment_start()
        reference = newest if newest is not None else since
        return time.time() - reference

    def _segment_end(self, segments: List[Tuple[float, Path]], i: int) -> float:
        """This segment's approximate end time: `segment_seconds` after its
        start, capped at the *next* segment's start when that's sooner (a
        segment can be legitimately cut short by a restart). Deliberately
        NOT extended past `segment_seconds` just because the next segment
        started later than that -- a gap after a reconnect is a gap, not
        this segment somehow covering it."""
        seg_start = segments[i][0]
        nominal_end = seg_start + self.config.segment_seconds
        if i + 1 < len(segments):
            return min(nominal_end, segments[i + 1][0])
        return nominal_end

    def list_segments(self, start_ts: float, end_ts: float) -> List[Path]:
        """Segments whose time window overlaps [start_ts, end_ts), oldest first."""
        segments = self._sorted_segments()
        result = []
        for i, (seg_start, path) in enumerate(segments):
            seg_end = self._segment_end(segments, i)
            if seg_end > start_ts and seg_start < end_ts:
                result.append(path)
        return result

    def prune(self, keep_seconds: float) -> None:
        """Deletes segments older than ``keep_seconds``, never touching
        anything at or after the protected timestamp (see protect_since)."""
        with self._lock:
            protected_since = self._protected_since
        cutoff = time.time() - keep_seconds
        segments = self._sorted_segments()
        for i, (seg_start, path) in enumerate(segments):
            seg_end = self._segment_end(segments, i)
            if seg_end > cutoff:
                break  # ascending, monotonic seg_end -- everything from here on is still within the keep window
            if protected_since is not None and seg_end > protected_since:
                continue
            try:
                path.unlink()
            except OSError:
                logger.exception("Failed to prune passthrough segment %s", path)

    def status(self) -> dict:
        with self._lock:
            connected = self._connected
            restart_count = self._restart_count
        newest = self.newest_segment_start()
        return {
            "recorder_connected": connected,
            "recorder_restart_count": restart_count,
            "last_segment_age_seconds": (time.time() - newest) if newest is not None else None,
        }
