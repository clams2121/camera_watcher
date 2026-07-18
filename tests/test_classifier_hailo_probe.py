import importlib.util
from pathlib import Path

from clip_classifier.hailo_probe import probe_hailo


def test_probe_reports_everything_missing_on_a_plain_host(tmp_path):
    # This sandbox genuinely has no Hailo device/runtime/HEF -- exercises
    # the real (not mocked) probe against reality for the common case.
    probe = probe_hailo(hef_path=tmp_path / "missing.hef", device_path=tmp_path / "missing-device")

    assert probe.device_present is False
    assert probe.runtime_importable is False
    assert probe.hef_present is False
    assert probe.fully_available is False


def test_probe_device_present_when_device_node_exists(tmp_path):
    device = tmp_path / "hailo0"
    device.write_text("")

    probe = probe_hailo(hef_path=tmp_path / "missing.hef", device_path=device)

    assert probe.device_present is True
    assert probe.fully_available is False  # runtime/hef still missing


def test_probe_hef_present_when_file_exists(tmp_path):
    hef = tmp_path / "model.hef"
    hef.write_bytes(b"fake hef")

    probe = probe_hailo(hef_path=hef, device_path=tmp_path / "missing-device")

    assert probe.hef_present is True
    assert probe.fully_available is False


def test_probe_runtime_importable_reflects_find_spec(monkeypatch, tmp_path):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())

    probe = probe_hailo(hef_path=tmp_path / "missing.hef", device_path=tmp_path / "missing-device")

    assert probe.runtime_importable is True
    assert probe.fully_available is False  # device/hef still missing


def test_fully_available_true_only_when_all_three_present(monkeypatch, tmp_path):
    device = tmp_path / "hailo0"
    device.write_text("")
    hef = tmp_path / "model.hef"
    hef.write_bytes(b"fake hef")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())

    probe = probe_hailo(hef_path=hef, device_path=device)

    assert probe.fully_available is True


def test_missing_summary_lists_only_whats_actually_missing(tmp_path):
    hef = tmp_path / "model.hef"
    hef.write_bytes(b"fake hef")  # hef present -- device and runtime are not

    probe = probe_hailo(hef_path=hef, device_path=tmp_path / "missing-device")
    summary = probe.missing_summary()

    assert "device node" in summary
    assert "HailoRT" in summary
    assert "HEF" not in summary  # the one thing that *is* present shouldn't be listed
