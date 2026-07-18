"""Discovers finalized camera_watcher clips that still need classifying, and
watches for new ones as they land.

A clip is "finalized" the moment its companion metadata sidecar
(``<stem>.json``, written by ``camera_watcher.recorder``) exists -- the
recorder writes that file last, via temp-write-then-atomic-rename, so its
appearance under its final name is itself the completion signal. This
module never needs to touch the clip's own temp-naming convention
(``.rec.mp4``) directly: keying discovery off the metadata sidecar rather
than the video file means a still-recording clip (no sidecar yet) is
naturally invisible here, whether or not its temp video file happens to be
sitting in the same directory.

"Pending" = has a metadata sidecar, but no ``<stem>.analysis.json`` yet (the
verdict sidecar this package writes -- see analysis.py). Re-running this
process never duplicates work: presence of that file is the only thing that
matters, there's no separate "seen" state to get out of sync with reality.
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import List, Optional, Set

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

ANALYSIS_SUFFIX = ".analysis.json"
REVIEW_SUFFIX = ".review.json"
_METADATA_TEMP_SUFFIX = ".tmp.json"  # camera_watcher.recorder._write_metadata's own temp name


def analysis_path_for(clip_path: Path) -> Path:
    return clip_path.parent / f"{clip_path.stem}{ANALYSIS_SUFFIX}"


def review_path_for(clip_path: Path) -> Path:
    return clip_path.parent / f"{clip_path.stem}{REVIEW_SUFFIX}"


def _is_metadata_sidecar(name: str) -> bool:
    """True for a clip's own <stem>.json, false for anything this package
    (or a future reviewer UI) writes alongside it, and false for the
    recorder's own in-flight temp file."""
    return (
        name.endswith(".json")
        and not name.endswith(ANALYSIS_SUFFIX)
        and not name.endswith(REVIEW_SUFFIX)
        and not name.endswith(_METADATA_TEMP_SUFFIX)
    )


def discover_pending_clips(data_root: Path) -> List[Path]:
    """Every finalized-but-unclassified clip under data_root/clips/*/,
    oldest metadata sidecar first."""
    clips_root = data_root / "clips"
    if not clips_root.exists():
        return []

    pending = []
    for camera_dir in sorted(p for p in clips_root.iterdir() if p.is_dir()):
        for json_path in camera_dir.glob("*.json"):
            if not _is_metadata_sidecar(json_path.name):
                continue
            clip_path = json_path.with_suffix(".mp4")
            if not clip_path.is_file():
                logger.warning("Metadata sidecar with no matching clip, skipping: %s", json_path)
                continue
            if analysis_path_for(clip_path).exists():
                continue
            try:
                mtime = json_path.stat().st_mtime
            except OSError:
                continue
            pending.append((mtime, clip_path))

    pending.sort(key=lambda item: item[0])
    return [clip_path for _, clip_path in pending]


class _MetadataSidecarHandler(FileSystemEventHandler):
    """Translates raw filesystem events into "a clip just became pending"
    calls -- the recorder's atomic rename shows up to inotify as a move
    (source and destination are in the same directory), but we also handle
    on_created for robustness against anything that writes the sidecar
    directly instead."""

    def __init__(self, on_pending_clip):
        self._on_pending_clip = on_pending_clip

    def on_created(self, event) -> None:
        self._maybe_report(event.src_path, event.is_directory)

    def on_moved(self, event) -> None:
        self._maybe_report(event.dest_path, event.is_directory)

    def _maybe_report(self, path_str: str, is_directory: bool) -> None:
        if is_directory:
            return
        json_path = Path(path_str)
        if not _is_metadata_sidecar(json_path.name):
            return
        clip_path = json_path.with_suffix(".mp4")
        if not clip_path.is_file():
            return
        if analysis_path_for(clip_path).exists():
            return
        self._on_pending_clip(clip_path)


class ClipWatcher:
    """Owns the backfill scan, the watchdog observer, a periodic safety-net
    rescan, and the bounded work queue clips are handed off through.
    ``get()`` is meant to be called from a single consumer thread; clips are
    fed in from both the initial backfill and the watchdog/rescan threads."""

    def __init__(self, data_root: Path, queue_maxsize: int = 256, rescan_interval_seconds: float = 600):
        self.data_root = data_root
        self._queue: "queue.Queue[Path]" = queue.Queue(maxsize=queue_maxsize)
        self._queued: Set[Path] = set()
        self._lock = threading.Lock()

        self._rescan_interval = rescan_interval_seconds
        self._rescan_stop = threading.Event()
        self._rescan_thread: Optional[threading.Thread] = None
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        for clip in discover_pending_clips(self.data_root):
            self._enqueue(clip)

        watch_dir = self.data_root / "clips"
        watch_dir.mkdir(parents=True, exist_ok=True)
        handler = _MetadataSidecarHandler(self._enqueue)
        self._observer = Observer()
        self._observer.schedule(handler, str(watch_dir), recursive=True)
        self._observer.start()

        self._rescan_stop.clear()
        self._rescan_thread = threading.Thread(target=self._rescan_loop, name="classifier-rescan", daemon=True)
        self._rescan_thread.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
        self._rescan_stop.set()
        if self._rescan_thread is not None:
            self._rescan_thread.join(timeout=5)

    def _rescan_loop(self) -> None:
        while not self._rescan_stop.is_set():
            if self._rescan_stop.wait(self._rescan_interval):
                return
            try:
                for clip in discover_pending_clips(self.data_root):
                    self._enqueue(clip)
            except Exception:
                logger.exception("Safety-net rescan of %s failed", self.data_root)

    def _enqueue(self, clip_path: Path) -> None:
        with self._lock:
            if clip_path in self._queued:
                return
            self._queued.add(clip_path)
        try:
            self._queue.put_nowait(clip_path)
        except queue.Full:
            with self._lock:
                self._queued.discard(clip_path)
            logger.warning(
                "Classifier work queue is full (%d) -- dropping %s for now; "
                "the safety-net rescan will pick it back up",
                self._queue.maxsize,
                clip_path,
            )

    def get(self, timeout: Optional[float] = None) -> Optional[Path]:
        """Blocks up to `timeout` seconds for the next pending clip, or
        returns None on timeout."""
        try:
            clip_path = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._lock:
            self._queued.discard(clip_path)
        return clip_path

    def qsize(self) -> int:
        return self._queue.qsize()
