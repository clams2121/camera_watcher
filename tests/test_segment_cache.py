import subprocess
import threading
import time

import pytest

from camera_watcher.segment_cache import SegmentCache, SegmentCacheConfig, _redact
from tests.ffmpeg_helpers import segment_name


def _touch(cache_dir, ts):
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / segment_name(ts)).write_bytes(b"fake")


def _make_cache(tmp_path, **overrides):
    config = SegmentCacheConfig(cache_dir=tmp_path / "cache", segment_seconds=overrides.pop("segment_seconds", 2.0), **overrides)
    return SegmentCache(url_factory=lambda: "rtsp://unused/main", config=config)


def test_list_segments_selects_the_overlapping_window(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=2.0)
    base = 1_700_000_000.0
    for i in range(5):  # segments at base, base+2, base+4, base+6, base+8
        _touch(cache.config.cache_dir, base + i * 2)

    # Window [base+3, base+7) overlaps the segments starting at base+2 (covers
    # [base+2, base+4)), base+4 (covers [base+4, base+6)), and base+6 (covers
    # [base+6, base+8)) -- but not the very first (base) or last (base+8).
    names = sorted(p.name for p in cache.list_segments(base + 3, base + 7))
    assert names == [segment_name(base + 2), segment_name(base + 4), segment_name(base + 6)]


def test_list_segments_on_empty_cache_returns_nothing(tmp_path):
    cache = _make_cache(tmp_path)
    assert cache.list_segments(0, 100) == []


def test_newest_segment_start_reflects_the_latest_file(tmp_path):
    cache = _make_cache(tmp_path)
    base = 1_700_000_100.0
    _touch(cache.config.cache_dir, base)
    _touch(cache.config.cache_dir, base + 10)
    assert cache.newest_segment_start() == pytest.approx(base + 10)


def test_prune_deletes_only_segments_older_than_keep_window(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    now = time.time()
    old = cache.config.cache_dir / segment_name(now - 100)
    recent = cache.config.cache_dir / segment_name(now - 1)
    _touch(cache.config.cache_dir, now - 100)
    _touch(cache.config.cache_dir, now - 1)

    cache.prune(keep_seconds=10)

    assert not old.exists()
    assert recent.exists()


def test_prune_never_deletes_a_protected_window(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    now = time.time()
    protected_start = now - 100
    _touch(cache.config.cache_dir, protected_start)
    protected_path = cache.config.cache_dir / segment_name(protected_start)

    cache.protect_since(protected_start)
    cache.prune(keep_seconds=10)  # would otherwise delete anything older than now-10

    assert protected_path.exists()

    cache.protect_since(None)
    cache.prune(keep_seconds=10)
    assert not protected_path.exists()


def test_redacts_credentials_from_ffmpeg_stderr_before_it_would_be_logged():
    line = "Input #0, rtsp, from 'rtsp://admin:hunter2@192.168.1.50:554/main': Connection refused"
    redacted = _redact(line)
    assert "hunter2" not in redacted
    assert "admin" not in redacted
    assert "192.168.1.50:554/main" in redacted  # the non-secret parts stay useful for diagnosis


class _FakeProcess:
    def __init__(self):
        self.stderr = iter([])
        self._returncode = None
        self.terminated = threading.Event()
        self.killed = threading.Event()

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated.set()
        self._returncode = 0

    def kill(self):
        self.killed.set()
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode


def test_supervisor_restarts_ffmpeg_after_it_exits(tmp_path, monkeypatch):
    processes = []

    def fake_popen(cmd, **kwargs):
        proc = _FakeProcess()
        proc._returncode = 0  # exits immediately, as if the camera refused the connection
        processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    cache = _make_cache(tmp_path, segment_seconds=1.0, restart_backoff_seconds=(0.05,), healthy_run_seconds=999)

    cache.start()
    try:
        assert _wait_until(lambda: len(processes) >= 2, timeout=3)
    finally:
        cache.stop()


def test_supervisor_kills_and_restarts_a_stalled_ffmpeg(tmp_path, monkeypatch):
    processes = []

    def fake_popen(cmd, **kwargs):
        proc = _FakeProcess()  # never exits on its own and never writes a segment -- a stall
        processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    cache = _make_cache(
        tmp_path, segment_seconds=0.05, stall_multiplier=2.0, restart_backoff_seconds=(0.05,), healthy_run_seconds=999
    )

    cache.start()
    try:
        assert _wait_until(lambda: processes and processes[0].killed.is_set(), timeout=3)
    finally:
        cache.stop()


def _wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
