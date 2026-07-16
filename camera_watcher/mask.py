"""Ignore-mask storage and rasterization.

Regions to ignore are stored as normalized polygons (coordinates in [0, 1],
independent of the camera's actual resolution) in a small JSON file, drawn by
the user over a snapshot in the web UI. At runtime they are rasterized to a
binary mask matching whatever resolution frames are being analyzed at.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

Polygon = list  # list of [x, y] pairs, each in [0, 1]


def _validate_polygons(polygons: list) -> list:
    cleaned = []
    for poly in polygons:
        pts = [[float(x), float(y)] for x, y in poly]
        if len(pts) >= 3:
            cleaned.append(pts)
    return cleaned


class MaskStore:
    """Loads/saves normalized ignore-polygons and rasterizes them on demand."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._polygons: list = []
        self._cache: dict = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
            if not self.path.exists():
                self._polygons = []
                return
            with self.path.open("r") as f:
                data = json.load(f)
            self._polygons = _validate_polygons(data.get("polygons", []))

    @property
    def polygons(self) -> list:
        with self._lock:
            return [list(p) for p in self._polygons]

    def save(self, polygons: list) -> None:
        cleaned = _validate_polygons(polygons)
        with self._lock:
            self._polygons = cleaned
            self._cache.clear()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp_path.open("w") as f:
                json.dump({"polygons": cleaned}, f, indent=2)
            tmp_path.replace(self.path)

    def keep_mask(self, width: int, height: int) -> np.ndarray:
        """A uint8 mask sized (height, width): 255 where motion should count, 0 where ignored."""
        with self._lock:
            cached = self._cache.get((width, height))
            if cached is not None:
                return cached
            mask = np.full((height, width), 255, dtype=np.uint8)
            for poly in self._polygons:
                pts = np.array([[x * width, y * height] for x, y in poly], dtype=np.int32)
                cv2.fillPoly(mask, [pts], 0)
            self._cache[(width, height)] = mask
            return mask
