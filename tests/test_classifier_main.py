import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from clip_classifier.main import _parse_args, main, run
from clip_classifier.watcher import ClipWatcher


def _wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _FakeWatcher:
    """A minimal stand-in for ClipWatcher -- run() only needs .get()."""

    def __init__(self, items):
        self._items = list(items)
        self._lock = threading.Lock()

    def get(self, timeout=None):
        with self._lock:
            if self._items:
                return self._items.pop(0)
        time.sleep(min(timeout or 0.05, 0.05))
        return None


def test_config_arg_is_required(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["clip_classifier"])
    with pytest.raises(SystemExit):
        _parse_args()


def test_config_arg_is_parsed(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["clip_classifier", "--config", "config/classifier.yaml"])
    args = _parse_args()
    assert args.config == "config/classifier.yaml"


def test_run_processes_clips_serially_in_order():
    clips = [Path(f"/data/clips/cam1/clip{i}.mp4") for i in range(5)]
    watcher = _FakeWatcher(clips)
    processed = []
    stop_event = threading.Event()

    def process_fn(clip_path):
        processed.append(clip_path)
        if len(processed) == len(clips):
            stop_event.set()

    thread = threading.Thread(target=run, args=(watcher, process_fn, stop_event))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert processed == clips


def test_run_continues_after_a_clip_raises(caplog):
    clips = [Path("/data/clips/cam1/bad.mp4"), Path("/data/clips/cam1/good.mp4")]
    watcher = _FakeWatcher(clips)
    processed = []
    stop_event = threading.Event()

    def process_fn(clip_path):
        if clip_path.name == "bad.mp4":
            raise RuntimeError("boom")
        processed.append(clip_path)
        stop_event.set()

    thread = threading.Thread(target=run, args=(watcher, process_fn, stop_event))
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert processed == [Path("/data/clips/cam1/good.mp4")]  # the second clip still got processed
    assert "Unhandled error processing" in caplog.text


def test_run_logs_per_clip_processing_time(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="clip_classifier.main")
    clips = [Path("/data/clips/cam1/clip.mp4")]
    watcher = _FakeWatcher(clips)
    stop_event = threading.Event()

    def process_fn(clip_path):
        stop_event.set()

    thread = threading.Thread(target=run, args=(watcher, process_fn, stop_event))
    thread.start()
    thread.join(timeout=5)

    assert "Processed clip.mp4 in" in caplog.text


def test_run_stops_promptly_when_stop_event_is_set_with_nothing_pending():
    watcher = _FakeWatcher([])
    stop_event = threading.Event()

    thread = threading.Thread(target=run, args=(watcher, lambda p: None, stop_event))
    thread.start()
    time.sleep(0.1)
    stop_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()


def test_run_works_with_a_real_clip_watcher(tmp_path):
    """End-to-end sanity check with the real ClipWatcher (not the fake) --
    proves run()'s .get() usage matches ClipWatcher's real interface."""
    clips_root = tmp_path / "clips" / "cam1"
    clips_root.mkdir(parents=True)
    clip = clips_root / "cam1_a.mp4"
    clip.write_bytes(b"fake")
    (clips_root / "cam1_a.json").write_text("{}")

    watcher = ClipWatcher(tmp_path, queue_maxsize=8, rescan_interval_seconds=999)
    watcher.start()
    processed = []
    stop_event = threading.Event()

    def process_fn(clip_path):
        processed.append(clip_path)
        stop_event.set()

    try:
        thread = threading.Thread(target=run, args=(watcher, process_fn, stop_event))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert processed == [clip]
    finally:
        watcher.stop()


# ---------- main() fail-loud paths (never actually enters the blocking run() loop) ----------


def test_main_fails_loud_on_a_missing_config_file(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["clip_classifier", "--config", "/does/not/exist.yaml"])
    with pytest.raises(SystemExit):
        main()
    assert "cp config/classifier.example.yaml" in capsys.readouterr().err


def test_main_fails_loud_when_the_cpu_backend_cannot_be_built(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "classifier.yaml"
    config_path.write_text(yaml.safe_dump({"data_root": str(tmp_path / "data"), "backend": "cpu"}))
    # cpu.model_path defaults to models/yolov8n.onnx, resolved relative to
    # config_path's directory -- deliberately never created here.

    monkeypatch.setattr(sys, "argv", ["clip_classifier", "--config", str(config_path)])
    with pytest.raises(SystemExit):
        main()

    assert "fetch_model" in capsys.readouterr().err
