"""hailo_platform isn't installed in this environment (no Hailo-8L
hardware here at all -- see hailo_probe.py) -- per the task's own
instructions, these tests mock it out entirely rather than skip.
"""
import sys
import types

import numpy as np
import pytest

from clip_classifier.detector import Detection
from clip_classifier.detectors.cpu import model_file_hash


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeVStreamInfo:
    def __init__(self, name):
        self.name = name


def _install_fake_hailo_platform(monkeypatch, raw_output):
    """Builds a minimal fake of the real hailo_platform API surface
    detectors/hailo.py drives, wired to return `raw_output` from infer()."""

    class _FakeHEF:
        def __init__(self, path):
            self.path = path

        def get_input_vstream_infos(self):
            return [_FakeVStreamInfo("input1")]

        def get_output_vstream_infos(self):
            return [_FakeVStreamInfo("output1")]

    class _FakeConfigureParams:
        @staticmethod
        def create_from_hef(hef, interface):
            return {"interface": interface}

    class _FakeHailoStreamInterface:
        PCIe = "PCIe"

    class _FakeNetworkGroup:
        def create_params(self):
            return {}

        def activate(self, params):
            return _NullContext()

    class _FakeVDevice:
        def configure(self, hef, params):
            return [_FakeNetworkGroup()]

    class _FakeInferVStreams:
        def __init__(self, network_group, params):
            self._network_group = network_group
            self._params = params

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def infer(self, input_feed):
            assert "input1" in input_feed
            return {"output1": raw_output}

    fake_module = types.ModuleType("hailo_platform")
    fake_module.HEF = _FakeHEF
    fake_module.ConfigureParams = _FakeConfigureParams
    fake_module.HailoStreamInterface = _FakeHailoStreamInterface
    fake_module.VDevice = _FakeVDevice
    fake_module.InferVStreams = _FakeInferVStreams
    monkeypatch.setitem(sys.modules, "hailo_platform", fake_module)


def _make_raw_output(anchors):
    num_classes = 80
    n = len(anchors)
    output = np.zeros((4 + num_classes, n), dtype=np.float32)
    for i, (cx, cy, w, h, class_id, score) in enumerate(anchors):
        output[0, i], output[1, i], output[2, i], output[3, i] = cx, cy, w, h
        output[4 + class_id, i] = score
    return output[np.newaxis, ...]


def test_hailo_detector_fails_loud_when_hef_file_is_missing(monkeypatch, tmp_path):
    _install_fake_hailo_platform(monkeypatch, raw_output=_make_raw_output([]))
    from clip_classifier.detectors.hailo import HailoModelLoadError, HailoYolov8Detector

    with pytest.raises(HailoModelLoadError, match="not found"):
        HailoYolov8Detector(tmp_path / "does-not-exist.hef")


def test_hailo_detector_wraps_device_init_failures_loudly(monkeypatch, tmp_path):
    hef_path = tmp_path / "model.hef"
    hef_path.write_bytes(b"fake hef")

    fake_module = types.ModuleType("hailo_platform")

    class _BoomHEF:
        def __init__(self, path):
            raise RuntimeError("no PCIe device found")

    fake_module.HEF = _BoomHEF
    fake_module.ConfigureParams = object()
    fake_module.HailoStreamInterface = object()
    fake_module.VDevice = object
    fake_module.InferVStreams = object
    monkeypatch.setitem(sys.modules, "hailo_platform", fake_module)

    from clip_classifier.detectors.hailo import HailoModelLoadError, HailoYolov8Detector

    with pytest.raises(HailoModelLoadError, match="no PCIe device found"):
        HailoYolov8Detector(hef_path)


def test_hailo_detector_detect_orchestrates_inference_and_reuses_cpu_postprocess(monkeypatch, tmp_path):
    hef_path = tmp_path / "model.hef"
    hef_path.write_bytes(b"fake hef bytes")

    raw_output = _make_raw_output([(320, 320, 100, 100, 0, 0.9)])  # centered person in a 640x640 letterboxed frame
    _install_fake_hailo_platform(monkeypatch, raw_output)

    from clip_classifier.detectors.hailo import HailoYolov8Detector

    detector = HailoYolov8Detector(hef_path)
    assert detector.name == "hailo"
    assert detector.model_version == model_file_hash(hef_path)

    frame = np.zeros((640, 640, 3), dtype=np.uint8)
    detections = detector.detect(frame)

    assert len(detections) == 1
    assert isinstance(detections[0], Detection)
    assert detections[0].label == "person"
    assert detections[0].confidence == pytest.approx(0.9)


def test_hailo_detector_returns_no_detections_below_confidence_floor(monkeypatch, tmp_path):
    hef_path = tmp_path / "model.hef"
    hef_path.write_bytes(b"fake hef bytes")

    raw_output = _make_raw_output([(320, 320, 100, 100, 0, 0.05)])  # well below the default floor
    _install_fake_hailo_platform(monkeypatch, raw_output)

    from clip_classifier.detectors.hailo import HailoYolov8Detector

    detector = HailoYolov8Detector(hef_path)
    frame = np.zeros((640, 640, 3), dtype=np.uint8)

    assert detector.detect(frame) == []
