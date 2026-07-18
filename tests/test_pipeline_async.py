import threading
import time

import numpy as np
import yaml

from camera_watcher.config import Config
from camera_watcher.pipeline import CameraPipeline


def make_frame(value=0):
    return np.full((16, 16, 3), value, dtype=np.uint8)


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _make_pipeline(tmp_path):
    path = tmp_path / "camera1.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "camera": {"name": "camera1", "host": "192.168.1.50"},
                "mask": {"path": str(tmp_path / "mask.json")},
                "recording": {"output_dir": str(tmp_path / "clips")},
                "motion": {"enabled": False},  # isolate queue/thread plumbing from detection logic
            }
        )
    )
    return CameraPipeline(Config(path))


def test_on_frame_never_blocks_and_processing_thread_consumes_it(tmp_path):
    """Simulates what the capture thread does: call _on_frame directly (no real
    RTSP source needed) and confirm a separate thread -- not the caller -- is
    what actually reaches the recorder."""
    pipeline = _make_pipeline(tmp_path)

    processed = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, motion_detected, boxes=(), **kwargs):
        processed.append(threading.current_thread().name)
        return original_handle_frame(ts, motion_detected, boxes, **kwargs)

    pipeline.recorder.handle_frame = spy

    pipeline._process_stop.clear()
    pipeline._process_thread = threading.Thread(target=pipeline._process_loop, name="frame-processor", daemon=True)
    pipeline._process_thread.start()

    try:
        caller_thread = threading.current_thread().name
        for i in range(5):
            pipeline._on_frame(1000.0 + i * 0.1, make_frame(i))

        assert _wait_until(lambda: len(processed) == 5)
        assert all(name != caller_thread for name in processed)
        assert all(name == "frame-processor" for name in processed)
    finally:
        pipeline._process_stop.set()
        pipeline._process_thread.join(timeout=2)


def test_frame_queue_drops_instead_of_blocking_when_processing_falls_behind(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    # No processing thread started -- simulates the processor falling behind.
    capacity = pipeline._frame_queue.maxsize
    for i in range(capacity + 10):
        pipeline._on_frame(2000.0 + i * 0.01, make_frame())  # must not raise or block

    assert pipeline._frame_queue.full()
    assert pipeline._dropped_frames == 10


def test_stop_drains_queued_frames_before_finalizing(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    pipeline._process_stop.clear()
    pipeline._process_thread = threading.Thread(target=pipeline._process_loop, name="frame-processor", daemon=True)
    pipeline._process_thread.start()

    processed = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, motion_detected, boxes=(), **kwargs):
        processed.append(ts)
        return original_handle_frame(ts, motion_detected, boxes, **kwargs)

    pipeline.recorder.handle_frame = spy

    for i in range(20):
        pipeline._on_frame(3000.0 + i * 0.01, make_frame())

    pipeline._process_stop.set()
    pipeline._process_thread.join(timeout=2)

    assert len(processed) == 20  # nothing left unprocessed in the queue


def test_full_lifecycle_starts_and_stops_every_thread_cleanly(tmp_path):
    """Integration smoke test, no real camera needed: RTSP connects to a
    port nothing is listening on and fails/retries in the background, the
    same as against a genuinely unreachable camera. Just proves start()/
    stop() bring up and tear down every thread (capture, frame processing,
    passthrough segment cache, recorder assembler, cache pruner, heatmap
    persistence -- retention is no longer one of them, see retention.py's
    enforce_global_retention) without hanging or raising."""
    path = tmp_path / "camera1.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "camera": {"name": "camera1", "host": "127.0.0.1", "port": 1},
                "mask": {"path": str(tmp_path / "mask.json")},
                "recording": {"output_dir": str(tmp_path / "clips"), "segment_seconds": 1},
            }
        )
    )
    pipeline = CameraPipeline(Config(path))
    pipeline.start()
    try:
        assert _wait_until(
            lambda: pipeline._heatmap_persist_thread is not None and pipeline._heatmap_persist_thread.is_alive()
        )
        assert pipeline._prune_thread is not None and pipeline._prune_thread.is_alive()
        assert not hasattr(pipeline, "_retention_thread")
    finally:
        pipeline.stop()

    assert not pipeline._heatmap_persist_thread.is_alive()
    assert not pipeline._prune_thread.is_alive()
    assert not pipeline._process_thread.is_alive()
