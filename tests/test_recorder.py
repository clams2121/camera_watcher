import json
import time
from datetime import datetime

import numpy as np

from camera_watcher.frame_buffer import FrameBuffer
from camera_watcher.recorder import RecorderConfig, SegmentRecorder, TEMP_SUFFIX


def make_frame(value=0):
    return np.full((8, 8, 3), value, dtype=np.uint8)


def test_records_prebuffer_and_finalizes(tmp_path):
    buf = FrameBuffer(max_seconds=5)
    config = RecorderConfig(
        output_dir=tmp_path,
        pre_buffer_seconds=3,
        post_buffer_seconds=2,
        max_chunk_seconds=100,
        overlap_seconds=1,
        camera_name="testcam",
    )
    rec = SegmentRecorder(buf, config, fps_hint=10)

    t0 = 1000.0
    for i in range(5):  # pre-motion frames land in the buffer only
        buf.append(make_frame(1), t0 + i * 0.1)

    motion_ts = t0 + 0.5
    buf.append(make_frame(2), motion_ts)
    rec.handle_frame(motion_ts, make_frame(2), True)
    assert rec.is_recording
    assert rec.current_temp_path is not None
    assert rec.current_temp_path.name.endswith(TEMP_SUFFIX)

    for i in range(1, 4):  # keep seeing motion briefly
        ts = motion_ts + i * 0.1
        buf.append(make_frame(2), ts)
        rec.handle_frame(ts, make_frame(2), True)

    final_ts = motion_ts + config.post_buffer_seconds + 0.5  # let the post-buffer lapse
    buf.append(make_frame(1), final_ts)
    rec.handle_frame(final_ts, make_frame(1), False)

    assert not rec.is_recording
    clips = list(tmp_path.glob("*.mp4"))
    assert len(clips) == 1
    assert not clips[0].name.endswith(TEMP_SUFFIX)
    assert clips[0].name.startswith("testcam_")


def test_forced_chunk_roll_creates_multiple_files(tmp_path):
    buf = FrameBuffer(max_seconds=5)
    config = RecorderConfig(
        output_dir=tmp_path,
        pre_buffer_seconds=1,
        post_buffer_seconds=1,
        max_chunk_seconds=1.0,
        overlap_seconds=0.3,
        camera_name="cam",
    )
    rec = SegmentRecorder(buf, config, fps_hint=20)

    t = 2000.0
    n_frames = 60  # 3 seconds at 20fps, continuous motion -- should force >=2 rolls
    for i in range(n_frames):
        ts = t + i * 0.05
        buf.append(make_frame(5), ts)
        rec.handle_frame(ts, make_frame(5), True)

    final_ts = t + n_frames * 0.05 + config.post_buffer_seconds + 0.5
    buf.append(make_frame(0), final_ts)
    rec.handle_frame(final_ts, make_frame(0), False)

    clips = [p for p in tmp_path.glob("*.mp4") if not p.name.endswith(TEMP_SUFFIX)]
    assert len(clips) >= 2
    assert not list(tmp_path.glob(f"*{TEMP_SUFFIX}"))  # nothing left mid-write


def test_event_log_records_union_bbox_on_finalize(tmp_path):
    buf = FrameBuffer(max_seconds=5)
    log_path = tmp_path / "events.jsonl"
    config = RecorderConfig(
        output_dir=tmp_path,
        pre_buffer_seconds=1,
        post_buffer_seconds=1,
        max_chunk_seconds=100,
        overlap_seconds=0.5,
        camera_name="cam",
        event_log_path=log_path,
    )
    rec = SegmentRecorder(buf, config, fps_hint=10)

    t0 = 5000.0
    buf.append(make_frame(2), t0)
    rec.handle_frame(t0, make_frame(2), True, boxes=[(10, 20, 30, 40)])  # -> (10,20)-(40,60)

    t1 = t0 + 0.1
    buf.append(make_frame(2), t1)
    rec.handle_frame(t1, make_frame(2), True, boxes=[(5, 50, 10, 10)])  # -> (5,50)-(15,60); widens the union

    final_ts = t1 + config.post_buffer_seconds + 0.5
    buf.append(make_frame(1), final_ts)
    rec.handle_frame(final_ts, make_frame(1), False)

    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["camera"] == "cam"
    assert entry["clip"].startswith("cam_")
    assert entry["bbox"] == [5, 20, 35, 40]  # union: x1=min(10,5)=5, y1=min(20,50)=20, x2=max(40,15)=40, y2=max(60,60)=60


def test_event_log_disabled_by_default(tmp_path):
    buf = FrameBuffer(max_seconds=5)
    config = RecorderConfig(output_dir=tmp_path, pre_buffer_seconds=1, post_buffer_seconds=1, camera_name="cam")
    rec = SegmentRecorder(buf, config, fps_hint=10)

    t0 = 6000.0
    buf.append(make_frame(2), t0)
    rec.handle_frame(t0, make_frame(2), True, boxes=[(0, 0, 5, 5)])
    final_ts = t0 + config.post_buffer_seconds + 0.5
    buf.append(make_frame(1), final_ts)
    rec.handle_frame(final_ts, make_frame(1), False)

    assert list(tmp_path.glob("*.jsonl")) == []  # no log file created when event_log_path is unset


def test_writes_companion_metadata_json_on_finalize(tmp_path):
    buf = FrameBuffer(max_seconds=5)
    config = RecorderConfig(
        output_dir=tmp_path,
        pre_buffer_seconds=1,
        post_buffer_seconds=1,
        max_chunk_seconds=100,
        overlap_seconds=0.5,
        camera_name="cam1",
    )
    rec = SegmentRecorder(buf, config, fps_hint=10)

    t0 = 8000.0
    buf.append(make_frame(2), t0)
    rec.handle_frame(t0, make_frame(2), True, boxes=[(0, 0, 10, 10)], score=100, detection_fraction=0.1)

    t1 = t0 + 1.0  # 1 second later, still motion
    buf.append(make_frame(2), t1)
    rec.handle_frame(t1, make_frame(2), True, boxes=[(0, 0, 20, 20)], score=300, detection_fraction=0.2)

    t2 = t1 + 1.0  # 1 more second, motion stops on this frame
    buf.append(make_frame(1), t2)
    rec.handle_frame(t2, make_frame(1), False)

    final_ts = t2 + config.post_buffer_seconds + 0.5  # let the post-buffer lapse
    buf.append(make_frame(1), final_ts)
    rec.handle_frame(final_ts, make_frame(1), False)

    clips = [p for p in tmp_path.glob("*.mp4") if not p.name.endswith(TEMP_SUFFIX)]
    assert len(clips) == 1
    metadata_path = clips[0].with_suffix(".json")
    assert metadata_path.exists()

    metadata = json.loads(metadata_path.read_text())
    assert metadata["event_id"] == clips[0].stem
    assert metadata["camera_id"] == "cam1"
    assert metadata["video_path"] == str(clips[0].resolve())
    # mean/max over only the two motion frames (100, 300); ratio over all three frames (t0, t1, t2)
    assert metadata["motion_confidence"] == {"mean_score": 200.0, "max_score": 300.0, "motion_frame_ratio": 0.6667}
    # only the t0->t1 gap counts (both ends had motion); t1->t2 doesn't, since t2 had no motion
    assert metadata["motion_time"] == 1.0
    assert metadata["detection_size"] == 0.2  # max(0.1, 0.2)

    start_dt = datetime.fromisoformat(metadata["start_time"])
    end_dt = datetime.fromisoformat(metadata["end_time"])
    assert end_dt > start_dt


def test_metadata_start_time_includes_prebuffer_compensation(tmp_path):
    """start_time should reflect the earliest *prepended* pre-buffer frame,
    not the moment motion was actually confirmed.

    Note: FrameBuffer.snapshot(seconds) filters by real wall-clock
    time.time(), not by the timestamp values passed to append() -- so unlike
    other tests in this file, this one must use real-time-based timestamps
    for the pre-buffer prepend to actually kick in (matching how production
    always calls it: with genuine time.time() values from the capture thread)."""
    buf = FrameBuffer(max_seconds=5)
    config = RecorderConfig(
        output_dir=tmp_path, pre_buffer_seconds=3, post_buffer_seconds=1, camera_name="cam"
    )
    rec = SegmentRecorder(buf, config, fps_hint=10)

    now = time.time()
    prebuffer_ts = now - 2.0  # inside the 3s pre-buffer window
    buf.append(make_frame(1), prebuffer_ts)

    motion_ts = now
    buf.append(make_frame(2), motion_ts)
    rec.handle_frame(motion_ts, make_frame(2), True)

    final_ts = motion_ts + config.post_buffer_seconds + 0.5
    buf.append(make_frame(1), final_ts)
    rec.handle_frame(final_ts, make_frame(1), False)

    clips = [p for p in tmp_path.glob("*.mp4") if not p.name.endswith(TEMP_SUFFIX)]
    metadata = json.loads(clips[0].with_suffix(".json").read_text())
    start_dt = datetime.fromisoformat(metadata["start_time"])
    expected = datetime.fromtimestamp(prebuffer_ts).astimezone()
    assert abs((start_dt - expected).total_seconds()) < 0.001
