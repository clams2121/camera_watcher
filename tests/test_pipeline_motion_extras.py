"""Integration tests for the R3 (bounding-box burn-in) and R5 (heatmap
accumulator) pipeline wiring -- run frames through the real motion detector
via the pipeline's processing thread, no real camera needed.
"""
import threading
import time

import numpy as np

from camera_watcher.config import Config
from camera_watcher.pipeline import CameraPipeline


def make_frame(value=0, size=64):
    return np.full((size, size, 3), value, dtype=np.uint8)


def _wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _make_pipeline(tmp_path, **motion_overrides):
    config = Config(tmp_path / "settings.yaml", tmp_path / "secrets.yaml")
    motion_settings = {"analysis_width": 64, "min_area": 10, "var_threshold": 16, "history": 20}
    motion_settings.update(motion_overrides)
    config.update_settings(
        {
            "mask": {"path": str(tmp_path / "mask.json")},
            "recording": {"output_dir": str(tmp_path / "clips")},
            "motion": motion_settings,
        }
    )
    pipeline = CameraPipeline(config)
    pipeline._process_stop.clear()
    pipeline._process_thread = threading.Thread(target=pipeline._process_loop, name="frame-processor", daemon=True)
    pipeline._process_thread.start()
    return pipeline


def _warm_up_and_trigger_motion(pipeline, received, base_ts):
    # Mimics what RtspCapture._run does per frame: append to the shared
    # pre-roll buffer, then hand off to _on_frame -- calling _on_frame alone
    # (as in the async-plumbing tests) never populates frame_buffer.
    background = make_frame(0)
    for i in range(15):
        ts = base_ts + i * 0.1
        frame = background.copy()
        pipeline.frame_buffer.append(frame, ts)
        pipeline._on_frame(ts, frame)
    assert _wait_until(lambda: len(received) == 15)

    moving = make_frame(0)
    moving[20:40, 20:40] = 255
    moving_ts = base_ts + 15 * 0.1
    pipeline.frame_buffer.append(moving, moving_ts)
    pipeline._on_frame(moving_ts, moving)
    assert _wait_until(lambda: len(received) == 16)
    return moving


def test_bounding_box_drawn_on_a_copy_not_the_shared_buffer_frame(tmp_path):
    pipeline = _make_pipeline(tmp_path, draw_bounding_box=True, box_padding_px=2)
    received = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, frame, motion_detected, boxes=()):
        received.append((frame, motion_detected, boxes))
        return original_handle_frame(ts, frame, motion_detected, boxes)

    pipeline.recorder.handle_frame = spy

    try:
        moving = _warm_up_and_trigger_motion(pipeline, received, 9000.0)
        frame_given_to_recorder, motion_detected, boxes = received[-1]
        assert motion_detected
        assert boxes

        # The box must be burned into a copy -- the original array (which the
        # pre-roll frame buffer also holds a reference to) must stay pristine.
        assert not np.array_equal(frame_given_to_recorder, moving)
        buffered = pipeline.frame_buffer.latest().frame
        assert np.array_equal(buffered, moving)
    finally:
        pipeline._process_stop.set()
        pipeline._process_thread.join(timeout=2)


def test_bounding_box_not_drawn_when_disabled(tmp_path):
    pipeline = _make_pipeline(tmp_path, draw_bounding_box=False)
    received = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, frame, motion_detected, boxes=()):
        received.append((frame, motion_detected, boxes))
        return original_handle_frame(ts, frame, motion_detected, boxes)

    pipeline.recorder.handle_frame = spy

    try:
        moving = _warm_up_and_trigger_motion(pipeline, received, 9500.0)
        frame_given_to_recorder, motion_detected, boxes = received[-1]
        assert motion_detected
        assert boxes  # still detected/reported...
        assert frame_given_to_recorder is moving  # ...but passed through unmodified and uncopied
    finally:
        pipeline._process_stop.set()
        pipeline._process_thread.join(timeout=2)


def test_heatmap_accumulates_and_reset_clears_it(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    received = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, frame, motion_detected, boxes=()):
        received.append((frame, motion_detected, boxes))
        return original_handle_frame(ts, frame, motion_detected, boxes)

    pipeline.recorder.handle_frame = spy

    try:
        assert pipeline.heatmap_png() is None  # nothing analyzed yet

        _warm_up_and_trigger_motion(pipeline, received, 9800.0)

        assert _wait_until(lambda: pipeline.accumulator is not None and int(pipeline.accumulator.counts.max()) > 0)
        assert pipeline.heatmap_png() is not None

        pipeline.reset_heatmap()
        assert int(pipeline.accumulator.counts.max()) == 0
    finally:
        pipeline._process_stop.set()
        pipeline._process_thread.join(timeout=2)
