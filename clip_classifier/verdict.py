"""Turns per-frame detections (backend-agnostic, see detector.py) plus the
recorder's own event metadata into a verdict + reason + label list -- pure
logic, no I/O, so it's fully unit-testable against synthetic detections.

Rules, in the order they're checked (the first one that matches wins):

1. **high** -- any sampled frame has a person/vehicle/animal detection at
   or above `thresholds.high_confidence`.
2. **review** -- any non-target-class detection with a bounding box
   covering at least `thresholds.review_large_object_area_frac` of the
   frame.
3. **review** -- any detection at all (any class, any size) present in at
   least `thresholds.review_persistent_detection_frac` of sampled frames.
4. **review** -- no detections in any sampled frame at all, but the
   recorder's own metadata shows large, persistent motion
   (`detection_size` and `motion_confidence.motion_frame_ratio` both at or
   above their respective thresholds) -- "something kept triggering this,
   the detector just didn't recognize what."
5. **low** -- none of the above.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .detector import Detection
from .labels import TARGET_LABELS

FrameDetections = List[Tuple[float, List[Detection]]]


@dataclass(frozen=True)
class VerdictResult:
    verdict: str  # high | review | low
    reason: str
    labels: List[Dict] = field(default_factory=list)


def _all_labels(frame_detections: FrameDetections) -> List[Dict]:
    """Every detection across every sampled frame, in the analysis
    sidecar's `labels` shape -- recorded regardless of verdict, so a
    "low" clip's labels are still visible for later review/debugging."""
    labels = []
    for offset, detections in frame_detections:
        for d in detections:
            labels.append(
                {
                    "label": d.label,
                    "confidence": round(d.confidence, 4),
                    "box": list(d.box_norm),
                    "frame_offset": offset,
                }
            )
    return labels


def evaluate_verdict(frame_detections: FrameDetections, recorder_metadata: dict, thresholds: dict) -> VerdictResult:
    """`frame_detections` is one (offset_seconds, [Detection, ...]) entry
    per successfully sampled frame (see sampling.sample_frames)."""
    high_confidence = thresholds["high_confidence"]
    large_area_frac = thresholds["review_large_object_area_frac"]
    persistent_frac = thresholds["review_persistent_detection_frac"]
    motion_size_threshold = thresholds["review_persistent_motion_detection_size"]
    motion_ratio_threshold = thresholds["review_persistent_motion_frame_ratio"]

    labels = _all_labels(frame_detections)

    # 1. high: a target-class detection at or above high_confidence, in any frame.
    for _offset, detections in frame_detections:
        for d in detections:
            if d.label in TARGET_LABELS and d.confidence >= high_confidence:
                return VerdictResult(verdict="high", reason=f"{d.label}>={high_confidence}", labels=labels)

    # 2. review: a large non-target-class detection.
    for _offset, detections in frame_detections:
        for d in detections:
            if d.label not in TARGET_LABELS:
                _, _, w, h = d.box_norm
                if w * h >= large_area_frac:
                    return VerdictResult(verdict="review", reason=f"large_other:{d.label}", labels=labels)

    # 3. review: something detected in most sampled frames, regardless of class/size.
    total_frames = len(frame_detections)
    if total_frames > 0:
        frames_with_any_detection = sum(1 for _offset, detections in frame_detections if detections)
        if frames_with_any_detection / total_frames >= persistent_frac:
            return VerdictResult(verdict="review", reason="persistent_detection", labels=labels)

    # 4. review: nothing detected anywhere, but the recorder saw large, persistent motion.
    if not any(detections for _offset, detections in frame_detections):
        detection_size = recorder_metadata.get("detection_size") or 0.0
        motion_frame_ratio = (recorder_metadata.get("motion_confidence") or {}).get("motion_frame_ratio") or 0.0
        if detection_size >= motion_size_threshold and motion_frame_ratio >= motion_ratio_threshold:
            return VerdictResult(verdict="review", reason="persistent_motion_no_detection", labels=labels)

    return VerdictResult(verdict="low", reason="no_target_or_notable_detections", labels=labels)
