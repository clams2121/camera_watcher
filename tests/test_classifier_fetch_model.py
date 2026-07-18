import hashlib
import sys
import urllib.request

import pytest
import yaml

import clip_classifier.fetch_model as fetch_model
from clip_classifier.fetch_model import FetchError, download, fetch_and_verify, main


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a, **k):
        data, self._data = self._data, b""
        return data


def test_download_writes_the_response_body_atomically(monkeypatch, tmp_path):
    payload = b"fake model bytes"
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse(payload))

    dest = tmp_path / "out.onnx"
    download("https://example.invalid/model.onnx", dest)

    assert dest.read_bytes() == payload


def test_download_raises_fetch_error_on_network_failure(monkeypatch, tmp_path):
    def _raise(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    with pytest.raises(FetchError):
        download("https://example.invalid/model.onnx", tmp_path / "out.onnx")


def test_fetch_and_verify_refuses_when_sha256_is_not_pinned(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_model, "MODEL_SHA256", "")
    with pytest.raises(FetchError, match="not pinned"):
        fetch_and_verify(tmp_path / "model.onnx")


def test_fetch_and_verify_installs_a_matching_download(monkeypatch, tmp_path):
    payload = b"the real model bytes"
    correct_hash = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(fetch_model, "MODEL_SHA256", correct_hash)
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse(payload))

    dest = tmp_path / "models" / "yolov8n.onnx"
    fetch_and_verify(dest)

    assert dest.read_bytes() == payload


def test_fetch_and_verify_rejects_a_checksum_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_model, "MODEL_SHA256", "0" * 64)  # deliberately wrong
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse(b"whatever bytes"))

    dest = tmp_path / "model.onnx"
    with pytest.raises(FetchError, match="Checksum mismatch"):
        fetch_and_verify(dest)

    assert not dest.exists()  # never installed on a checksum failure


def test_print_hash_only_mode_downloads_and_prints_without_installing(monkeypatch, tmp_path, capsys):
    payload = b"some model bytes"
    expected_hash = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse(payload))
    monkeypatch.setattr(sys, "argv", ["fetch_model", "--print-hash-only"])

    main()

    out = capsys.readouterr().out.strip()
    assert out == expected_hash


def test_main_fails_loud_on_missing_config(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["fetch_model", "--config", str(tmp_path / "does-not-exist.yaml")])
    with pytest.raises(SystemExit):
        main()


def test_main_fetches_into_the_configured_model_path(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "classifier.yaml"
    config_path.write_text(yaml.safe_dump({"data_root": "data", "cpu": {"model_path": "models/yolov8n.onnx"}}))

    payload = b"pinned and verified bytes"
    correct_hash = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(fetch_model, "MODEL_SHA256", correct_hash)
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse(payload))
    monkeypatch.setattr(sys, "argv", ["fetch_model", "--config", str(config_path)])

    main()

    installed = tmp_path / "models" / "yolov8n.onnx"
    assert installed.read_bytes() == payload
    assert "installed verified model" in capsys.readouterr().out
