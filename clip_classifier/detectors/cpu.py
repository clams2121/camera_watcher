"""CPU object detector: YOLOv8n via ONNX Runtime -- the always-available
default backend (see ../backend.py for auto/cpu/hailo selection).

Standard Ultralytics YOLOv8 ONNX export shape: output (1, 4 + num_classes,
num_anchors) -- box coordinates in the letterboxed input's pixel space (no
separate objectness score, unlike YOLOv5) followed by one raw score per
class. Preprocessing/postprocessing here follow that export's own
conventions: letterbox-pad to a square input, take each anchor's best
class score directly as its confidence, no additional objectness multiply.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np

from ..detector import Detection
from ..labels import COCO_CLASSES

logger = logging.getLogger(__name__)

INPUT_SIZE = 640  # YOLOv8n's standard export input resolution (square)

# Not user-configurable (see config.py) -- these are implementation details
# of how raw model output becomes a Detection list, not policy. The actual
# "does this count as high/review/low" thresholds all live in
# classifier.yaml's `thresholds` section and are applied afterwards, in
# verdict.py, against whatever this backend returns.
DEFAULT_CONFIDENCE_FLOOR = 0.25  # standard YOLO inference default
DEFAULT_NMS_IOU_THRESHOLD = 0.45  # standard YOLO NMS default


class ModelLoadError(Exception):
    """Raised when the ONNX model file is missing or fails to load."""


def letterbox(frame: np.ndarray, size: int = INPUT_SIZE) -> Tuple[np.ndarray, float, int, int]:
    """Resizes `frame` to fit within `size`x`size` preserving aspect ratio,
    padding the rest with neutral gray. Returns (padded_bgr, scale, pad_x,
    pad_y) -- what postprocess() needs to map a detected box in the padded
    image back to the original frame's coordinates."""
    h, w = frame.shape[:2]
    scale = min(size / w, size / h)
    new_w, new_h = round(w * scale), round(h * scale)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    pad_x = (size - new_w) // 2
    pad_y = (size - new_h) // 2
    padded = np.full((size, size, 3), 114, dtype=np.uint8)
    padded[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    return padded, scale, pad_x, pad_y


def preprocess(frame: np.ndarray, size: int = INPUT_SIZE) -> Tuple[np.ndarray, float, int, int]:
    """Letterboxes, normalizes to [0, 1], and reshapes to the model's
    expected (1, 3, size, size) NCHW float32 input."""
    padded, scale, pad_x, pad_y = letterbox(frame, size)
    blob = padded[:, :, ::-1].astype(np.float32) / 255.0  # BGR -> RGB, 0-1
    blob = blob.transpose(2, 0, 1)[np.newaxis, ...]  # HWC -> CHW, add batch dim
    return np.ascontiguousarray(blob), scale, pad_x, pad_y


def postprocess(
    raw_output: np.ndarray,
    scale: float,
    pad_x: int,
    pad_y: int,
    orig_width: int,
    orig_height: int,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    nms_iou_threshold: float = DEFAULT_NMS_IOU_THRESHOLD,
) -> List[Detection]:
    """`raw_output` is the model's (1, 4 + num_classes, num_anchors) (or an
    already-squeezed (4 + num_classes, num_anchors)) tensor. Returns
    normalized, frame-relative Detections, deduplicated via NMS."""
    output = raw_output
    if output.ndim == 3:
        output = output[0]
    # output is now (4 + num_classes, num_anchors)
    boxes_xywh = output[:4, :].T  # (num_anchors, 4) -- cx, cy, w, h, letterboxed pixel space
    class_scores = output[4:, :].T  # (num_anchors, num_classes)

    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(class_scores.shape[0]), class_ids]

    keep_mask = confidences >= confidence_floor
    if not np.any(keep_mask):
        return []

    boxes_xywh = boxes_xywh[keep_mask]
    class_ids = class_ids[keep_mask]
    confidences = confidences[keep_mask]

    # cv2.dnn.NMSBoxes wants (x, y, w, h) top-left-origin boxes -- the
    # coordinate space doesn't matter to it beyond relative overlap, so
    # letterboxed pixel space (before undoing padding/scale below) is fine.
    cx, cy, w, h = boxes_xywh[:, 0], boxes_xywh[:, 1], boxes_xywh[:, 2], boxes_xywh[:, 3]
    nms_boxes = np.stack([cx - w / 2, cy - h / 2, w, h], axis=1)

    indices = cv2.dnn.NMSBoxes(nms_boxes.tolist(), confidences.tolist(), confidence_floor, nms_iou_threshold)
    if len(indices) == 0:
        return []
    indices = np.array(indices).reshape(-1)

    detections = []
    for i in indices:
        box_cx, box_cy, box_w, box_h = boxes_xywh[i]
        # Undo the letterbox: subtract padding, then unscale back to the
        # original frame's pixel space.
        x1 = (box_cx - box_w / 2 - pad_x) / scale
        y1 = (box_cy - box_h / 2 - pad_y) / scale
        w_orig = box_w / scale
        h_orig = box_h / scale

        # Clip to the frame -- a box can legitimately extend past the
        # letterboxed canvas's own padding into what would be out-of-bounds
        # original-frame coordinates near an edge.
        x1_clipped = max(0.0, x1)
        y1_clipped = max(0.0, y1)
        x2_clipped = min(float(orig_width), x1 + w_orig)
        y2_clipped = min(float(orig_height), y1 + h_orig)
        if x2_clipped <= x1_clipped or y2_clipped <= y1_clipped:
            continue

        class_id = int(class_ids[i])
        label = COCO_CLASSES[class_id] if 0 <= class_id < len(COCO_CLASSES) else f"class_{class_id}"
        detections.append(
            Detection(
                label=label,
                confidence=float(confidences[i]),
                box_norm=(
                    x1_clipped / orig_width,
                    y1_clipped / orig_height,
                    (x2_clipped - x1_clipped) / orig_width,
                    (y2_clipped - y1_clipped) / orig_height,
                ),
            )
        )
    return detections


def model_file_hash(model_path: Path) -> str:
    digest = hashlib.sha256()
    with model_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


class CpuYolov8Detector:
    """ONNX Runtime YOLOv8n on CPU -- the always-available default backend.

    The model file is never downloaded automatically -- see fetch_model.py.
    A missing file fails loud here with the exact command to fetch it.
    """

    name = "cpu"

    def __init__(self, model_path: Path, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR):
        # Imported here, not at module level, so importing this module --
        # e.g. for tests that only exercise the pure pre/postprocess
        # functions above -- never requires onnxruntime to be importable
        # unless a CpuYolov8Detector is actually constructed.
        import onnxruntime

        if not model_path.is_file():
            raise ModelLoadError(
                f"CPU detector model not found: {model_path}\n"
                f"Fetch it first:\n"
                f"    python -m clip_classifier.fetch_model\n"
            )
        self._session = onnxruntime.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        self._confidence_floor = confidence_floor
        self.model_version = model_file_hash(model_path)

    def detect(self, frame: np.ndarray) -> List[Detection]:
        blob, scale, pad_x, pad_y = preprocess(frame)
        raw_output = self._session.run(None, {self._input_name: blob})[0]
        orig_height, orig_width = frame.shape[:2]
        return postprocess(raw_output, scale, pad_x, pad_y, orig_width, orig_height, self._confidence_floor)
