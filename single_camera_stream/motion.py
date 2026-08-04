"""Motion detection: background subtraction (MOG2) on a downscaled,
grayscale copy of each frame -- the same algorithm/thresholds this
project has used since its first version, reimplemented standalone here
(no ignore-mask support -- that's a fleet-UI feature this simpler tool
doesn't have).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

BoundingBox = Tuple[int, int, int, int]  # (x, y, w, h), in analysis-resolution pixels


@dataclass
class MotionResult:
    motion_detected: bool
    score: int  # total foreground contour area (px^2, at analysis resolution)
    box: Optional[BoundingBox]  # union of all contours that passed min_area, or None
    analysis_size: Tuple[int, int] = (0, 0)  # (width, height) `box` is relative to


class MotionDetector:
    def __init__(
        self,
        analysis_width: int = 320,
        min_area: int = 500,
        var_threshold: float = 25,
        history: int = 300,
    ):
        self.analysis_width = analysis_width
        self.min_area = min_area
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=False
        )
        # Skip flagging motion for the first few frames while MOG2's
        # background model is still stabilizing, to avoid a false trigger
        # on every startup/reconnect.
        self._warmup_frames_remaining = max(history // 10, 5)

    def process(self, frame: np.ndarray) -> MotionResult:
        h, w = frame.shape[:2]
        scale = self.analysis_width / w
        analysis_h = max(1, int(h * scale))
        small = cv2.resize(frame, (self.analysis_width, analysis_h))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        analysis_size = (self.analysis_width, analysis_h)

        fg = self._subtractor.apply(gray)

        if self._warmup_frames_remaining > 0:
            self._warmup_frames_remaining -= 1
            return MotionResult(False, 0, None, analysis_size)

        fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)[1]
        fg = cv2.dilate(fg, None, iterations=2)

        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        score = 0
        min_x = min_y = None
        max_x = max_y = None
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area:
                continue
            score += area
            x, y, bw, bh = cv2.boundingRect(c)
            min_x = x if min_x is None else min(min_x, x)
            min_y = y if min_y is None else min(min_y, y)
            max_x = x + bw if max_x is None else max(max_x, x + bw)
            max_y = y + bh if max_y is None else max(max_y, y + bh)

        box = None
        if min_x is not None:
            box = (min_x, min_y, max_x - min_x, max_y - min_y)

        return MotionResult(box is not None, int(score), box, analysis_size)
