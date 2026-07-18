"""Integration tests for the R3 (bounding-box preview overlay), R5 (heatmap
accumulator), and motion-detector-to-recorder wiring -- run frames through
the real motion detector via the pipeline's processing thread, no real
camera or ffmpeg needed (recorder.handle_frame is spied on and the passthrough
segment cache/assembler are never started, so no actual clip assembly runs
here -- that's covered end-to-end with real ffmpeg in test_recorder.py).
"""
import threading
import time

import numpy as np
import yaml

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
    motion_settings = {"analysis_width": 64, "min_area": 10, "var_threshold": 16, "history": 20}
    motion_settings.update(motion_overrides)
    path = tmp_path / "camera1.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "camera": {"name": "camera1", "host": "192.168.1.50"},
                "mask": {"path": str(tmp_path / "mask.json")},
                "recording": {"output_dir": str(tmp_path / "clips")},
                "motion": motion_settings,
            }
        )
    )
    pipeline = CameraPipeline(Config(path))
    pipeline._process_stop.clear()
    pipeline._process_thread = threading.Thread(target=pipeline._process_loop, name="frame-processor", daemon=True)
    pipeline._process_thread.start()
    return pipeline


def _warm_up_and_trigger_motion(pipeline, received, base_ts):
    # Mimics what RtspCapture._run does per frame: append to the shared
    # frame buffer, then hand off to _on_frame -- calling _on_frame alone
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
    """Boxes are preview-only now (passthrough recording never decodes frame
    content) -- frame_for_preview() must draw on a copy, never mutating what
    sits in the shared frame buffer."""
    pipeline = _make_pipeline(tmp_path, draw_bounding_box=True, box_padding_px=2)
    received = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, motion_detected, boxes=(), **kwargs):
        received.append((motion_detected, boxes))
        return original_handle_frame(ts, motion_detected, boxes, **kwargs)

    pipeline.recorder.handle_frame = spy

    try:
        moving = _warm_up_and_trigger_motion(pipeline, received, 9000.0)
        motion_detected, boxes = received[-1]
        assert motion_detected
        assert boxes

        buffered = pipeline.frame_buffer.latest().frame
        assert np.array_equal(buffered, moving)  # untouched by preview drawing

        preview = pipeline.frame_for_preview(buffered)
        assert not np.array_equal(preview, moving)  # the box was actually burned into the returned copy
        assert np.array_equal(buffered, moving)  # ...and still didn't mutate the shared buffer's frame
    finally:
        pipeline._process_stop.set()
        pipeline._process_thread.join(timeout=2)


def test_bounding_box_not_drawn_when_disabled(tmp_path):
    pipeline = _make_pipeline(tmp_path, draw_bounding_box=False)
    received = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, motion_detected, boxes=(), **kwargs):
        received.append((motion_detected, boxes))
        return original_handle_frame(ts, motion_detected, boxes, **kwargs)

    pipeline.recorder.handle_frame = spy

    try:
        _warm_up_and_trigger_motion(pipeline, received, 9500.0)
        motion_detected, boxes = received[-1]
        assert motion_detected
        assert boxes  # still detected/reported to the recorder for metadata purposes...

        latest = pipeline.frame_buffer.latest().frame
        preview = pipeline.frame_for_preview(latest)
        assert preview is latest  # ...but the preview stream draws nothing when disabled
    finally:
        pipeline._process_stop.set()
        pipeline._process_thread.join(timeout=2)


def test_heatmap_accumulates_and_reset_clears_it(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    received = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, motion_detected, boxes=(), **kwargs):
        received.append((motion_detected, boxes))
        return original_handle_frame(ts, motion_detected, boxes, **kwargs)

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


def test_pipeline_passes_real_motion_score_and_detection_size_to_the_recorder(tmp_path):
    """End-to-end wiring check: score/detection_fraction computed by the
    real motion detector must reach recorder.handle_frame non-trivially --
    what the recorder then does with them (the companion metadata JSON) is
    covered directly, with real ffmpeg assembly, in test_recorder.py."""
    pipeline = _make_pipeline(tmp_path, draw_bounding_box=False)
    received = []
    original_handle_frame = pipeline.recorder.handle_frame

    def spy(ts, motion_detected, boxes=(), **kwargs):
        received.append((motion_detected, boxes, kwargs.get("score", 0), kwargs.get("detection_fraction", 0.0)))
        return original_handle_frame(ts, motion_detected, boxes, **kwargs)

    pipeline.recorder.handle_frame = spy

    try:
        base_ts = 9900.0
        _warm_up_and_trigger_motion(pipeline, received, base_ts)

        motion_detected, boxes, score, detection_fraction = received[-1]
        assert motion_detected
        assert boxes
        assert score > 0
        assert detection_fraction > 0
    finally:
        pipeline._process_stop.set()
        pipeline._process_thread.join(timeout=2)
