"""Motion detection: background subtraction on a downscaled frame, with an ignore mask."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .mask import MaskStore


@dataclass
class MotionResult:
    motion_detected: bool
    score: int  # total foreground contour area (px^2, at analysis resolution) after masking


class MotionDetector:
    """Frame-differencing motion detector.

    Runs on a downscaled grayscale copy of each frame to keep CPU cost low --
    full resolution is only needed when actually recording, not when deciding
    whether to.
    """

    def __init__(
        self,
        mask_store: MaskStore,
        analysis_width: int = 320,
        min_area: int = 500,
        var_threshold: float = 25,
        history: int = 300,
    ):
        self._mask_store = mask_store
        self.analysis_width = analysis_width
        self.min_area = min_area
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=False
        )
        # Skip flagging motion for the first few frames while MOG2's
        # background model is still stabilizing, to avoid a false trigger
        # on every startup/reconnect.
        self._warmup_frames_remaining = max(history // 10, 5)

    def configure(
        self,
        *,
        analysis_width: int | None = None,
        min_area: int | None = None,
        var_threshold: float | None = None,
        history: int | None = None,
    ) -> None:
        if analysis_width is not None:
            self.analysis_width = analysis_width
        if min_area is not None:
            self.min_area = min_area
        if var_threshold is not None:
            self._subtractor.setVarThreshold(var_threshold)
        if history is not None:
            self._subtractor.setHistory(history)

    def process(self, frame: np.ndarray) -> MotionResult:
        h, w = frame.shape[:2]
        scale = self.analysis_width / w
        small = cv2.resize(frame, (self.analysis_width, max(1, int(h * scale))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        fg = self._subtractor.apply(gray)

        if self._warmup_frames_remaining > 0:
            self._warmup_frames_remaining -= 1
            return MotionResult(False, 0)

        keep = self._mask_store.keep_mask(gray.shape[1], gray.shape[0])
        fg = cv2.bitwise_and(fg, keep)
        fg = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)[1]
        fg = cv2.dilate(fg, None, iterations=2)

        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        score = sum(cv2.contourArea(c) for c in contours if cv2.contourArea(c) >= self.min_area)
        return MotionResult(score > 0, int(score))
