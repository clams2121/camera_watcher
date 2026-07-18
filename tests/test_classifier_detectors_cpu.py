import numpy as np
import pytest

from clip_classifier.detector import Detection
from clip_classifier.detectors.cpu import (
    CpuYolov8Detector,
    ModelLoadError,
    letterbox,
    model_file_hash,
    postprocess,
    preprocess,
)


def make_frame(h=100, w=200):
    return np.zeros((h, w, 3), dtype=np.uint8)


# ---------- letterbox / preprocess ----------


def test_letterbox_preserves_aspect_ratio_and_pads_symmetric_dimension():
    frame = make_frame(h=100, w=200)  # 2:1 landscape
    padded, scale, pad_x, pad_y = letterbox(frame, size=640)

    assert padded.shape == (640, 640, 3)
    assert scale == pytest.approx(3.2)  # min(640/200, 640/100)
    assert pad_x == 0
    assert pad_y == 160  # (640 - 320) // 2


def test_letterbox_pads_the_other_axis_for_a_portrait_frame():
    frame = make_frame(h=200, w=100)  # 1:2 portrait
    padded, scale, pad_x, pad_y = letterbox(frame, size=640)

    assert scale == pytest.approx(3.2)
    assert pad_x == 160
    assert pad_y == 0


def test_preprocess_output_shape_and_value_range():
    frame = np.random.randint(0, 256, (100, 200, 3), dtype=np.uint8)
    blob, scale, pad_x, pad_y = preprocess(frame, size=640)

    assert blob.shape == (1, 3, 640, 640)
    assert blob.dtype == np.float32
    assert blob.min() >= 0.0 and blob.max() <= 1.0


# ---------- postprocess ----------


def _make_raw_output(anchors):
    """anchors: list of (cx, cy, w, h, class_id, score) -> (1, 84, N) tensor."""
    num_classes = 80
    n = len(anchors)
    output = np.zeros((4 + num_classes, n), dtype=np.float32)
    for i, (cx, cy, w, h, class_id, score) in enumerate(anchors):
        output[0, i] = cx
        output[1, i] = cy
        output[2, i] = w
        output[3, i] = h
        output[4 + class_id, i] = score
    return output[np.newaxis, ...]


def test_postprocess_decodes_a_single_high_confidence_detection():
    # identity transform (scale=1, no padding) so pixel math is exact
    raw = _make_raw_output([(100, 100, 40, 60, 0, 0.9)])  # class 0 = person

    detections = postprocess(raw, scale=1.0, pad_x=0, pad_y=0, orig_width=640, orig_height=640)

    assert len(detections) == 1
    d = detections[0]
    assert d.label == "person"
    assert d.confidence == pytest.approx(0.9)
    # cx=100,cy=100,w=40,h=60 -> x1=80,y1=70,w=40,h=60 -> normalized /640
    assert d.box_norm == pytest.approx((80 / 640, 70 / 640, 40 / 640, 60 / 640))


def test_postprocess_filters_detections_below_the_confidence_floor():
    raw = _make_raw_output([(100, 100, 40, 60, 0, 0.1)])  # below default floor of 0.25

    detections = postprocess(raw, scale=1.0, pad_x=0, pad_y=0, orig_width=640, orig_height=640)

    assert detections == []


def test_postprocess_applies_nms_to_overlapping_same_class_boxes():
    raw = _make_raw_output(
        [
            (100, 100, 40, 60, 0, 0.9),  # person, higher confidence
            (102, 101, 40, 60, 0, 0.6),  # nearly identical box, same class -- should be suppressed
        ]
    )

    detections = postprocess(raw, scale=1.0, pad_x=0, pad_y=0, orig_width=640, orig_height=640)

    assert len(detections) == 1
    assert detections[0].confidence == pytest.approx(0.9)


def test_postprocess_keeps_non_overlapping_detections_of_different_classes():
    raw = _make_raw_output(
        [
            (100, 100, 40, 60, 0, 0.9),  # person, top-left area
            (500, 500, 40, 60, 2, 0.8),  # car, bottom-right area -- no overlap
        ]
    )

    detections = postprocess(raw, scale=1.0, pad_x=0, pad_y=0, orig_width=640, orig_height=640)

    labels = {d.label for d in detections}
    assert labels == {"person", "car"}


def test_postprocess_clips_boxes_to_frame_bounds():
    # box centered near the top-left corner, large enough to extend past (0, 0)
    raw = _make_raw_output([(5, 5, 40, 40, 0, 0.9)])

    detections = postprocess(raw, scale=1.0, pad_x=0, pad_y=0, orig_width=640, orig_height=640)

    assert len(detections) == 1
    x, y, w, h = detections[0].box_norm
    assert x >= 0 and y >= 0
    assert x + w <= 1.0 and y + h <= 1.0


def test_postprocess_undoes_a_non_identity_letterbox_transform():
    # Simulates a 200x100 frame letterboxed into 640x640: scale=3.2, pad_y=160.
    # Place a box in the letterboxed (padded) space and confirm it maps back
    # to the expected location in the original 200x100 frame.
    scale = 3.2
    pad_x, pad_y = 0, 160
    # In original-frame pixels: a 20x10 box at (50, 20) -> letterboxed pixels:
    letterboxed_x1 = 50 * scale + pad_x
    letterboxed_y1 = 20 * scale + pad_y
    letterboxed_w = 20 * scale
    letterboxed_h = 10 * scale
    cx = letterboxed_x1 + letterboxed_w / 2
    cy = letterboxed_y1 + letterboxed_h / 2

    raw = _make_raw_output([(cx, cy, letterboxed_w, letterboxed_h, 0, 0.9)])
    detections = postprocess(raw, scale=scale, pad_x=pad_x, pad_y=pad_y, orig_width=200, orig_height=100)

    assert len(detections) == 1
    x, y, w, h = detections[0].box_norm
    assert x * 200 == pytest.approx(50, abs=0.5)
    assert y * 100 == pytest.approx(20, abs=0.5)
    assert w * 200 == pytest.approx(20, abs=0.5)
    assert h * 100 == pytest.approx(10, abs=0.5)


def test_postprocess_with_no_anchors_at_all_returns_empty():
    raw = _make_raw_output([])
    detections = postprocess(raw, scale=1.0, pad_x=0, pad_y=0, orig_width=640, orig_height=640)
    assert detections == []


def test_postprocess_labels_unknown_class_ids_defensively():
    # class_id 79 is the last real COCO class (toothbrush) -- confirm the
    # array bounds check doesn't off-by-one on the legitimate boundary.
    raw = _make_raw_output([(100, 100, 40, 60, 79, 0.9)])
    detections = postprocess(raw, scale=1.0, pad_x=0, pad_y=0, orig_width=640, orig_height=640)
    assert detections[0].label == "toothbrush"


# ---------- model hashing ----------


def test_model_file_hash_is_stable_and_content_dependent(tmp_path):
    a = tmp_path / "a.onnx"
    b = tmp_path / "b.onnx"
    a.write_bytes(b"model bytes one")
    b.write_bytes(b"model bytes two")

    assert model_file_hash(a) == model_file_hash(a)
    assert model_file_hash(a) != model_file_hash(b)


# ---------- CpuYolov8Detector ----------


def test_cpu_detector_fails_loud_when_model_file_is_missing(tmp_path):
    with pytest.raises(ModelLoadError, match="fetch_model"):
        CpuYolov8Detector(tmp_path / "does-not-exist.onnx")


def test_cpu_detector_detect_orchestrates_preprocess_session_postprocess(tmp_path, monkeypatch):
    import onnxruntime

    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"fake model bytes -- session creation is mocked below")

    raw_output = _make_raw_output([(320, 320, 100, 100, 0, 0.9)])  # centered in a 640x640 letterboxed frame

    class _FakeInput:
        name = "images"

    class _FakeSession:
        def __init__(self, *a, **k):
            self.run_calls = []

        def get_inputs(self):
            return [_FakeInput()]

        def run(self, output_names, input_feed):
            self.run_calls.append(input_feed)
            return [raw_output]

    monkeypatch.setattr(onnxruntime, "InferenceSession", _FakeSession)

    detector = CpuYolov8Detector(model_path)
    assert detector.name == "cpu"
    assert detector.model_version == model_file_hash(model_path)

    frame = np.zeros((640, 640, 3), dtype=np.uint8)
    detections = detector.detect(frame)

    assert len(detections) == 1
    assert detections[0].label == "person"
    assert isinstance(detections[0], Detection)
