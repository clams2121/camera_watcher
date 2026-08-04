import json

import numpy as np

from single_camera_stream.frame_buffer import FrameBuffer
from single_camera_stream.recorder import (
    TEMP_SUFFIX,
    RecorderConfig,
    SingleStreamRecorder,
    draw_box,
    scale_box,
)


def _frame(size=16, color=0):
    return np.full((size, size, 3), color, dtype=np.uint8)


def _feed(rec, fb, start_ts, count, interval, motion_detected):
    ts = start_ts
    for _ in range(count):
        frame = _frame()
        fb.append(ts, frame)
        rec.handle_frame(ts, frame, motion_detected)
        ts += interval
    return ts


def _finalized_clips(directory):
    # "*.mp4" also matches the in-progress "<name>.mp4.rec.mp4" temp file,
    # since it ends in ".mp4" too -- exclude it explicitly.
    return [p for p in directory.glob("*.mp4") if not p.name.endswith(TEMP_SUFFIX)]


def test_no_motion_never_starts_a_recording(tmp_path):
    fb = FrameBuffer(max_seconds=15)
    rec = SingleStreamRecorder(fb, RecorderConfig(output_dir=tmp_path, camera_name="cam1"))

    _feed(rec, fb, 1000.0, 20, 0.1, motion_detected=False)

    assert not rec.is_recording
    assert not list(tmp_path.glob("*.mp4"))


def test_motion_starts_a_recording_as_a_temp_file(tmp_path):
    fb = FrameBuffer(max_seconds=15)
    cfg = RecorderConfig(output_dir=tmp_path, camera_name="cam1", pre_buffer_seconds=1, post_buffer_seconds=1, fallback_fps=10)
    rec = SingleStreamRecorder(fb, cfg)

    ts = _feed(rec, fb, 1000.0, 10, 0.1, motion_detected=False)
    frame = _frame(color=255)
    fb.append(ts, frame)
    rec.handle_frame(ts, frame, motion_detected=True)

    assert rec.is_recording
    temp_files = list(tmp_path.glob(f"*{TEMP_SUFFIX}"))
    assert len(temp_files) == 1
    assert not _finalized_clips(tmp_path)  # not finalized yet

    rec.stop()  # clean up so the VideoWriter handle doesn't linger past the test


def test_recording_finalizes_after_the_post_buffer_window(tmp_path):
    fb = FrameBuffer(max_seconds=15)
    cfg = RecorderConfig(
        output_dir=tmp_path, camera_name="cam1", pre_buffer_seconds=1, post_buffer_seconds=1, fallback_fps=10
    )
    rec = SingleStreamRecorder(fb, cfg)

    ts = _feed(rec, fb, 1000.0, 10, 0.1, motion_detected=False)  # pre-roll
    ts = _feed(rec, fb, ts, 5, 0.1, motion_detected=True)  # ~0.5s of motion
    assert rec.is_recording

    _feed(rec, fb, ts, 15, 0.1, motion_detected=False)  # past post_buffer_seconds

    assert not rec.is_recording
    clips = _finalized_clips(tmp_path)
    assert len(clips) == 1
    assert not list(tmp_path.glob(f"*{TEMP_SUFFIX}"))  # temp file was renamed away, not left behind

    metadata = json.loads(clips[0].with_suffix(".json").read_text())
    assert metadata["camera_name"] == "cam1"
    assert metadata["event_id"] == clips[0].stem
    assert "start_time" in metadata
    assert "stop_time" in metadata
    assert 0.3 <= metadata["motion_seconds"] <= 0.6
    assert set(metadata) == {"event_id", "camera_name", "start_time", "stop_time", "motion_seconds"}


def test_max_chunk_seconds_forces_a_split_while_motion_continues(tmp_path):
    fb = FrameBuffer(max_seconds=15)
    cfg = RecorderConfig(
        output_dir=tmp_path,
        camera_name="cam1",
        pre_buffer_seconds=1,
        post_buffer_seconds=100,  # never triggers on its own within this test
        max_chunk_seconds=1,
        overlap_seconds=0.3,
        fallback_fps=10,
    )
    rec = SingleStreamRecorder(fb, cfg)

    ts = _feed(rec, fb, 1000.0, 5, 0.1, motion_detected=False)
    _feed(rec, fb, ts, 15, 0.1, motion_detected=True)  # 1.5s of continuous motion -> one forced split

    assert rec.is_recording  # second chunk still open -- motion never stopped
    assert len(_finalized_clips(tmp_path)) == 1  # first chunk finalized by the split

    rec.stop()
    assert len(_finalized_clips(tmp_path)) == 2
    assert not list(tmp_path.glob(f"*{TEMP_SUFFIX}"))


def test_stop_finalizes_an_in_progress_recording(tmp_path):
    fb = FrameBuffer(max_seconds=15)
    cfg = RecorderConfig(
        output_dir=tmp_path, camera_name="cam1", pre_buffer_seconds=1, post_buffer_seconds=100, fallback_fps=10
    )
    rec = SingleStreamRecorder(fb, cfg)

    ts = _feed(rec, fb, 1000.0, 5, 0.1, motion_detected=False)
    _feed(rec, fb, ts, 5, 0.1, motion_detected=True)
    assert rec.is_recording

    rec.stop()

    assert not rec.is_recording
    assert len(_finalized_clips(tmp_path)) == 1
    assert not list(tmp_path.glob(f"*{TEMP_SUFFIX}"))


def test_stop_with_nothing_recording_is_a_safe_no_op(tmp_path):
    fb = FrameBuffer(max_seconds=15)
    rec = SingleStreamRecorder(fb, RecorderConfig(output_dir=tmp_path, camera_name="cam1"))
    rec.stop()  # must not raise
    assert not _finalized_clips(tmp_path)


def test_draw_bounding_box_option_does_not_break_recording(tmp_path):
    # Pixel-level burn-in behavior is covered directly by the scale_box/
    # draw_box tests below -- this just proves handle_frame wires them in
    # (via RecorderConfig.draw_bounding_box) without raising or corrupting
    # the write path.
    fb = FrameBuffer(max_seconds=15)
    cfg = RecorderConfig(
        output_dir=tmp_path,
        camera_name="cam1",
        pre_buffer_seconds=0,
        post_buffer_seconds=1,
        fallback_fps=10,
        draw_bounding_box=True,
        box_padding_px=2,
    )
    rec = SingleStreamRecorder(fb, cfg)
    frame = _frame()
    fb.append(1000.0, frame)
    rec.handle_frame(1000.0, frame, motion_detected=True, box=(2, 2, 4, 4), analysis_size=(16, 16))

    assert rec.is_recording
    rec.stop()
    assert len(_finalized_clips(tmp_path)) == 1


# ---------- scale_box / draw_box ----------


def test_scale_box_maps_analysis_coordinates_and_pads():
    box = scale_box((10, 10, 10, 10), analysis_size=(64, 64), frame_shape=(128, 128, 3), padding_px=5)
    # 2x scale: (20, 20, 20, 20), then padded 5px on every side
    assert box == (15, 15, 30, 30)


def test_scale_box_clamps_to_the_frame_bounds():
    x, y, w, h = scale_box((0, 0, 5, 5), analysis_size=(64, 64), frame_shape=(64, 64, 3), padding_px=1000)
    assert x == 0 and y == 0
    assert x + w <= 64 and y + h <= 64


def test_draw_box_returns_a_modified_copy_without_mutating_the_original():
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    original = frame.copy()

    annotated = draw_box(frame, (5, 5, 10, 10))

    assert np.array_equal(frame, original)  # original untouched
    assert not np.array_equal(annotated, frame)  # the copy actually changed
