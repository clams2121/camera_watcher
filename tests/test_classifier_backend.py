import sys
import types

import pytest

import clip_classifier.backend as backend_module
from clip_classifier.backend import BackendError, build_detector
from clip_classifier.hailo_probe import HailoProbeResult


def _settings(tmp_path, **overrides):
    settings = {
        "backend": "cpu",
        "cpu": {"model_path": str(tmp_path / "yolov8n.onnx")},
        "hailo": {"hef_path": str(tmp_path / "yolov8n.hef")},
    }
    settings.update(overrides)
    return settings


def _install_fake_hailo_detector(monkeypatch, tmp_path):
    """Installs a fake clip_classifier.detectors.hailo module so
    backend._build_hailo() never actually needs hailo_platform installed."""
    fake_module = types.ModuleType("clip_classifier.detectors.hailo")

    class _FakeHailoDetector:
        name = "hailo"
        model_version = "fake-hailo-version"

        def __init__(self, hef_path):
            self.hef_path = hef_path

        def detect(self, frame):
            return []

    class _FakeHailoModelLoadError(Exception):
        pass

    fake_module.HailoYolov8Detector = _FakeHailoDetector
    fake_module.HailoModelLoadError = _FakeHailoModelLoadError
    monkeypatch.setitem(sys.modules, "clip_classifier.detectors.hailo", fake_module)
    return _FakeHailoDetector


def test_backend_cpu_never_probes_hailo(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise AssertionError("probe_hailo should never be called for backend: cpu")

    monkeypatch.setattr(backend_module, "probe_hailo", _boom)

    settings = _settings(tmp_path, backend="cpu")
    with pytest.raises(BackendError, match="fetch_model"):
        build_detector(settings)  # model file doesn't exist -- fails loud, but via the CPU path only


def test_backend_hailo_fails_loud_with_no_fallback_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        backend_module,
        "probe_hailo",
        lambda hef_path: HailoProbeResult(device_present=False, runtime_importable=False, hef_present=False),
    )

    settings = _settings(tmp_path, backend="hailo")
    with pytest.raises(BackendError, match="No fallback"):
        build_detector(settings)


def test_backend_hailo_succeeds_when_fully_available(monkeypatch, tmp_path):
    monkeypatch.setattr(
        backend_module,
        "probe_hailo",
        lambda hef_path: HailoProbeResult(device_present=True, runtime_importable=True, hef_present=True),
    )
    fake_cls = _install_fake_hailo_detector(monkeypatch, tmp_path)

    settings = _settings(tmp_path, backend="hailo")
    detector = build_detector(settings)

    assert isinstance(detector, fake_cls)
    assert detector.name == "hailo"


def test_backend_auto_uses_hailo_when_fully_available(monkeypatch, tmp_path):
    monkeypatch.setattr(
        backend_module,
        "probe_hailo",
        lambda hef_path: HailoProbeResult(device_present=True, runtime_importable=True, hef_present=True),
    )
    fake_cls = _install_fake_hailo_detector(monkeypatch, tmp_path)

    settings = _settings(tmp_path, backend="auto")
    detector = build_detector(settings)

    assert isinstance(detector, fake_cls)


def test_backend_auto_falls_back_to_cpu_and_logs_loudly_when_hailo_unavailable(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        backend_module,
        "probe_hailo",
        lambda hef_path: HailoProbeResult(device_present=False, runtime_importable=False, hef_present=True),
    )

    settings = _settings(tmp_path, backend="auto")
    # CPU model also missing here -- proves the *fallback selection itself*
    # happened (we reach the CPU path and get its fail-loud error, not
    # Hailo's), independent of whether the CPU model file exists.
    with pytest.raises(BackendError, match="fetch_model"):
        build_detector(settings)

    assert "falling back to the CPU backend" in caplog.text
    assert "device node" in caplog.text  # names what was actually missing


def test_backend_unknown_string_fails_loud(tmp_path):
    settings = _settings(tmp_path, backend="gpu")
    with pytest.raises(BackendError, match="Unknown backend"):
        build_detector(settings)
