"""Detector abstraction: normalizes the CPU (ONNX Runtime YOLOv8n) and
Hailo-8L backends to the same output, so verdict logic (verdict.py) never
needs to know which one produced a given frame's detections -- see
backend.py for how one gets selected (auto/cpu/hailo).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol, Tuple

import numpy as np


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    # (x, y, w, h), each normalized to [0, 1] of the frame -- same
    # (x, y, w, h) convention as camera_watcher's own bounding boxes (see
    # recorder.py), just normalized instead of pixel-valued since the
    # detector's input resolution and the clip's actual resolution differ.
    box_norm: Tuple[float, float, float, float]


class Detector(Protocol):
    name: str  # "cpu" | "hailo"
    model: str  # e.g. "yolov8n"
    model_version: str

    def detect(self, frame: np.ndarray) -> List[Detection]:
        """`frame` is a BGR ndarray (OpenCV convention, matching
        sampling.extract_frame's output). Returns every detection above
        whatever confidence floor the backend applies internally -- callers
        that need a specific threshold (see verdict.py's `high_confidence`)
        filter afterwards, so nothing is silently thrown away here."""
        ...
