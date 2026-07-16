"""Motion accumulator: a persisted, never-decaying count of where motion has
been detected, for spotting spots (like a waving flag) that might need a new
ignore zone. Counts only ever go up; the user clears it explicitly by calling
:meth:`MotionAccumulator.reset`.
"""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

# uint32 has effectively unbounded headroom here -- a single pixel would need
# to register motion on nearly every frame for years to approach it -- so the
# saturating add below is a defensive measure, not something expected to matter.
_DTYPE = np.uint32
_MAX_VALUE = np.iinfo(_DTYPE).max


class MotionAccumulator:
    """Accumulates a per-pixel motion count at a fixed resolution, persisted to disk."""

    def __init__(self, path: Path | str, width: int, height: int):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._width = width
        self._height = height
        self._counts = self._load_or_new()
        self._dirty = False

    @property
    def counts(self) -> np.ndarray:
        """A copy of the current per-pixel counts. Mainly useful for tests/inspection."""
        with self._lock:
            return self._counts.copy()

    def _load_or_new(self) -> np.ndarray:
        if self.path.exists():
            try:
                arr = np.load(self.path)
                if arr.shape == (self._height, self._width) and arr.dtype == _DTYPE:
                    return arr
            except (OSError, ValueError):
                pass
        # Also covers a resolution change (e.g. motion.analysis_width edited)
        # since the old array's shape would no longer match -- starts fresh
        # rather than trying to resample old counts onto a new grid.
        return np.zeros((self._height, self._width), dtype=_DTYPE)

    def add(self, mask: np.ndarray) -> None:
        """`mask` is a foreground mask (non-zero where motion was seen); resized
        to the accumulator's resolution first if it doesn't already match."""
        if mask.shape[:2] != (self._height, self._width):
            mask = cv2.resize(mask, (self._width, self._height), interpolation=cv2.INTER_NEAREST)
        hit = mask > 0
        if not np.any(hit):
            return
        with self._lock:
            bumped = self._counts[hit].astype(np.uint64) + 1
            self._counts[hit] = np.minimum(bumped, _MAX_VALUE).astype(_DTYPE)
            self._dirty = True

    def reset(self) -> None:
        with self._lock:
            self._counts = np.zeros((self._height, self._width), dtype=_DTYPE)
            self._dirty = True
            self._save_locked()

    def save(self) -> None:
        """Persist to disk if anything's changed since the last save. Cheap to call often."""
        with self._lock:
            if self._dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(self.path.stem + ".tmp.npy")
        np.save(tmp_path, self._counts)
        tmp_path.replace(self.path)
        self._dirty = False

    def heatmap_png(self, max_alpha: int = 200) -> bytes:
        """Render as an RGBA PNG (red, increasingly opaque with count) sized to
        match this accumulator's resolution. Fully transparent where nothing
        has ever been detected. Opacity is normalized against the current
        peak count, so it stays meaningful as accumulation grows over time."""
        with self._lock:
            counts = self._counts.copy()

        peak = int(counts.max())
        if peak == 0:
            alpha = np.zeros(counts.shape, dtype=np.uint8)
        else:
            # sqrt compression: a handful of early hits shouldn't already look
            # fully saturated next to a long-running true hotspot.
            normalized = np.sqrt(counts.astype(np.float64) / peak)
            alpha = (normalized * max_alpha).astype(np.uint8)

        bgra = np.zeros((counts.shape[0], counts.shape[1], 4), dtype=np.uint8)
        bgra[..., 2] = 255  # R (OpenCV encodes 4-channel images as BGRA)
        bgra[..., 3] = alpha
        ok, buf = cv2.imencode(".png", bgra)
        if not ok:
            raise RuntimeError("failed to encode heatmap PNG")
        return buf.tobytes()
