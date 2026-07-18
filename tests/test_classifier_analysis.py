import json

import numpy as np
import pytest

from clip_classifier.analysis import analyze_clip, build_process_fn
from clip_classifier.detector import Detection
from clip_classifier.watcher import analysis_path_for
from tests.ffmpeg_helpers import make_segment

THRESHOLDS = {
    "high_confidence": 0.5,
    "review_large_object_area_frac": 0.05,
    "review_persistent_detection_frac": 0.6,
    "review_persistent_motion_detection_size": 0.05,
    "review_persistent_motion_frame_ratio": 0.6,
}


class _FakeDetector:
    name = "cpu"
    model = "yolov8n"
    model_version = "fake-version-123"

    def __init__(self, detections_by_call=None, raise_on_call=None):
        self._detections_by_call = list(detections_by_call or [])
        self._raise_on_call = raise_on_call
        self.calls = 0

    def detect(self, frame):
        if self._raise_on_call is not None and self.calls == self._raise_on_call:
            raise RuntimeError("simulated detector crash")
        result = self._detections_by_call[self.calls] if self.calls < len(self._detections_by_call) else []
        self.calls += 1
        return result


def _write_v2_clip(tmp_path, name="cam1_20260101_120000", duration=3.0, **metadata_overrides):
    clip = tmp_path / f"{name}.mp4"
    make_segment(clip, duration=duration, size="32x32")
    metadata = {
        "schema_version": 2,
        "event_id": name,
        "start_time": "2026-01-01T12:00:00+00:00",
        "end_time": "2026-01-01T12:00:03+00:00",
        "duration_seconds": duration,
        "peak_motion_time": "2026-01-01T12:00:01+00:00",
        "motion_timeline": [
            {"t": 0, "score": 10.0, "motion_detected": True},
            {"t": 1, "score": 300.0, "motion_detected": True},
            {"t": 2, "score": 50.0, "motion_detected": True},
        ],
        "detection_size": 0.0,
        "motion_confidence": {"motion_frame_ratio": 0.0},
    }
    metadata.update(metadata_overrides)
    (tmp_path / f"{name}.json").write_text(json.dumps(metadata))
    return clip


def test_analyze_clip_writes_a_high_verdict_sidecar(tmp_path):
    clip = _write_v2_clip(tmp_path)
    detector = _FakeDetector(detections_by_call=[[Detection("person", 0.9, (0.1, 0.1, 0.2, 0.2))]])

    payload = analyze_clip(clip, detector, max_frames=3, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert payload["verdict"] == "high"
    assert payload["reason"] == "person>=0.5"
    assert payload["backend"] == "cpu"
    assert payload["model"] == "yolov8n"
    assert payload["model_version"] == "fake-version-123"
    assert payload["event_id"] == clip.stem
    assert payload["schema_version"] == 1
    assert payload["sampling_fallback"] is False
    assert len(payload["sampled_frame_offsets"]) >= 1
    assert payload["processing_seconds"] >= 0

    analysis_path = analysis_path_for(clip)
    assert analysis_path.exists()
    assert json.loads(analysis_path.read_text()) == payload
    # atomic write leaves nothing behind
    assert not list(tmp_path.glob("*.tmp.json"))


def test_analyze_clip_writes_a_low_verdict_sidecar_when_nothing_detected(tmp_path):
    clip = _write_v2_clip(tmp_path, name="cam1_low")
    detector = _FakeDetector(detections_by_call=[[], [], []])

    payload = analyze_clip(clip, detector, max_frames=3, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert payload["verdict"] == "low"
    assert payload["labels"] == []


def test_analyze_clip_writes_an_error_sidecar_when_metadata_is_missing(tmp_path):
    clip = tmp_path / "cam1_no_metadata.mp4"
    make_segment(clip, duration=0.3, size="32x32")
    detector = _FakeDetector()

    payload = analyze_clip(clip, detector, max_frames=3, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert payload["verdict"] == "error"
    assert "metadata" in payload["reason"]
    assert payload["backend"] is None
    assert analysis_path_for(clip).exists()


def test_analyze_clip_writes_an_error_sidecar_when_metadata_is_invalid_json(tmp_path):
    clip = tmp_path / "cam1_bad_json.mp4"
    make_segment(clip, duration=0.3, size="32x32")
    (tmp_path / "cam1_bad_json.json").write_text("{not valid json")
    detector = _FakeDetector()

    payload = analyze_clip(clip, detector, max_frames=3, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert payload["verdict"] == "error"
    assert "metadata" in payload["reason"]


def test_analyze_clip_writes_an_error_sidecar_when_sampling_fails(tmp_path):
    # A metadata sidecar pointing at a video that doesn't decode at all
    # (zero-byte file) -- sample_frames should fail to extract any frame.
    clip = tmp_path / "cam1_broken.mp4"
    clip.write_bytes(b"")
    metadata = {
        "schema_version": 2,
        "event_id": "cam1_broken",
        "start_time": "2026-01-01T12:00:00+00:00",
        "end_time": "2026-01-01T12:00:03+00:00",
        "duration_seconds": 3.0,
        "peak_motion_time": None,
        "motion_timeline": [],
    }
    (tmp_path / "cam1_broken.json").write_text(json.dumps(metadata))
    detector = _FakeDetector()

    payload = analyze_clip(clip, detector, max_frames=3, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert payload["verdict"] == "error"
    assert "sampling" in payload["reason"]
    assert detector.calls == 0  # never even reached the detector


def test_analyze_clip_writes_an_error_sidecar_when_the_detector_raises(tmp_path):
    clip = _write_v2_clip(tmp_path, name="cam1_detector_crash")
    detector = _FakeDetector(raise_on_call=0)

    payload = analyze_clip(clip, detector, max_frames=3, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert payload["verdict"] == "error"
    assert "detector failed" in payload["reason"]


def test_analyze_clip_marks_v1_fallback_sampling_in_the_sidecar(tmp_path):
    clip = tmp_path / "cam1_v1.mp4"
    make_segment(clip, duration=2.0, size="32x32")
    v1_metadata = {
        "event_id": "cam1_v1",
        "camera_id": "cam1",
        "start_time": "2026-01-01T12:00:00+00:00",
        "end_time": "2026-01-01T12:00:02+00:00",
        # no schema_version -- genuine v1 shape
    }
    (tmp_path / "cam1_v1.json").write_text(json.dumps(v1_metadata))
    detector = _FakeDetector(detections_by_call=[[], []])

    payload = analyze_clip(clip, detector, max_frames=2, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert payload["sampling_fallback"] is True
    assert payload["verdict"] in ("low", "review")  # not "error" -- v1 fallback is not a failure


def test_analysis_sidecar_never_touches_the_recorder_metadata_file(tmp_path):
    clip = _write_v2_clip(tmp_path)
    metadata_path = tmp_path / f"{clip.stem}.json"
    original_metadata = metadata_path.read_text()
    detector = _FakeDetector(detections_by_call=[[]])

    analyze_clip(clip, detector, max_frames=3, min_frame_spacing_seconds=1.0, thresholds=THRESHOLDS)

    assert metadata_path.read_text() == original_metadata


def test_build_process_fn_wires_a_real_backend_and_returns_a_usable_callable(tmp_path, monkeypatch):
    # build_process_fn imports build_detector lazily from .backend -- patch
    # it there, where the lazy import actually resolves it from.
    import clip_classifier.backend as backend_module

    fake_detector = _FakeDetector(detections_by_call=[[]])
    monkeypatch.setattr(backend_module, "build_detector", lambda settings: fake_detector)

    clip = _write_v2_clip(tmp_path, name="cam1_via_process_fn")
    settings = {
        "backend": "cpu",
        "cpu": {"model_path": str(tmp_path / "unused.onnx")},
        "hailo": {"hef_path": str(tmp_path / "unused.hef")},
        "thresholds": THRESHOLDS,
        "sampling": {"max_frames": 2, "min_frame_spacing_seconds": 1.0},
    }

    process_fn = build_process_fn(settings)
    process_fn(clip)

    assert analysis_path_for(clip).exists()
