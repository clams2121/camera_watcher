import json
import time
from datetime import datetime

import pytest

from camera_watcher.recorder import RecorderConfig, SegmentRecorder, TEMP_SUFFIX
from camera_watcher.segment_cache import SegmentCache, SegmentCacheConfig
from tests.ffmpeg_helpers import make_cache_segments, segment_name

BASE_TS = 1_800_000_000.0  # fixed epoch-ish anchor; only relative offsets from it matter in these tests


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _make_cache(tmp_path, segment_seconds=1.0):
    return SegmentCache(
        url_factory=lambda: "rtsp://unused/main",
        config=SegmentCacheConfig(cache_dir=tmp_path / "cache", segment_seconds=segment_seconds),
    )


def _make_recorder(tmp_path, cache, **overrides):
    kwargs = dict(
        output_dir=tmp_path / "clips",
        pre_buffer_seconds=overrides.get("pre_buffer_seconds", 2),
        post_buffer_seconds=overrides.get("post_buffer_seconds", 1),
        max_chunk_seconds=overrides.get("max_chunk_seconds", 100),
        overlap_seconds=overrides.get("overlap_seconds", 1),
        camera_name=overrides.get("camera_name", "cam"),
    )
    if "config_hash_provider" in overrides:
        kwargs["config_hash_provider"] = overrides["config_hash_provider"]
    if "mask_hash_provider" in overrides:
        kwargs["mask_hash_provider"] = overrides["mask_hash_provider"]
    config = RecorderConfig(**kwargs)
    recorder = SegmentRecorder(cache, config)
    recorder.start()
    return recorder


def _finalized_clips(clips_dir):
    return sorted(p for p in clips_dir.glob("*.mp4") if not p.name.endswith(TEMP_SUFFIX))


def _wait_for_clips(clips_dir, count):
    assert _wait_until(lambda: len(_finalized_clips(clips_dir)) == count, timeout=8.0)
    return _finalized_clips(clips_dir)


def _read_metadata(clip_path):
    """Metadata is written just after the clip is renamed into place, not
    atomically alongside it -- wait for it rather than racing the assembler."""
    metadata_path = clip_path.with_suffix(".json")
    assert _wait_until(metadata_path.exists, timeout=8.0)
    return json.loads(metadata_path.read_text())


def _wait_for_stable_finalized_clip_count(clips_dir, timeout=8.0, stable_for=0.5):
    """Waits until the finalized (non-temp) clip count in `clips_dir` stops
    changing and no temp file is present -- more robust than a fixed
    expected count when several async assembly jobs (e.g. from chunk
    rollovers) can still be mid-flight one after another."""
    deadline = time.time() + timeout
    last_count = -1
    stable_since = None
    while time.time() < deadline:
        finalized = [p for p in clips_dir.glob("*.mp4") if not p.name.endswith(TEMP_SUFFIX)]
        has_temp = any(p.name.endswith(TEMP_SUFFIX) for p in clips_dir.glob("*.mp4"))
        if len(finalized) == last_count and not has_temp:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_for:
                return finalized
        else:
            stable_since = None
        last_count = len(finalized)
        time.sleep(0.05)
    raise AssertionError("clip count in %s never stabilized" % clips_dir)


def test_records_prebuffer_and_finalizes_a_real_clip(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(tmp_path, cache, pre_buffer_seconds=3, post_buffer_seconds=2, camera_name="testcam")
    try:
        t0 = BASE_TS
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 4, count=14, segment_seconds=1.0)

        motion_ts = t0 + 0.5
        recorder.handle_frame(motion_ts, True)
        assert recorder.is_recording

        for i in range(1, 4):
            recorder.handle_frame(motion_ts + i * 0.1, True)

        final_ts = motion_ts + 0.3 + recorder.config.post_buffer_seconds + 0.5
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        clips = _wait_for_clips(tmp_path / "clips", count=1)
        assert clips[0].name.startswith("testcam_")
        assert not clips[0].name.endswith(TEMP_SUFFIX)
    finally:
        recorder.stop()


def test_forced_chunk_roll_creates_multiple_clips_with_no_leftover_temp_files(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(
        tmp_path, cache, pre_buffer_seconds=1, post_buffer_seconds=1, max_chunk_seconds=1.0, overlap_seconds=0.3
    )
    try:
        t = BASE_TS + 10_000
        make_cache_segments(cache.config.cache_dir, start_ts=t - 2, count=12, segment_seconds=1.0)

        n_frames = 60  # 3 seconds of continuous motion -- should force >=2 rolls
        for i in range(n_frames):
            recorder.handle_frame(t + i * 0.05, True)

        final_ts = t + n_frames * 0.05 + recorder.config.post_buffer_seconds + 0.5
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        clips = _wait_for_stable_finalized_clip_count(tmp_path / "clips")
        assert len(clips) >= 2
    finally:
        recorder.stop()


def test_metadata_records_union_bbox_and_no_legacy_event_log(tmp_path):
    """Schema v2 folds the bounding box into the companion metadata JSON --
    there's no separate motion-events log any more."""
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(tmp_path, cache, pre_buffer_seconds=1, post_buffer_seconds=1, overlap_seconds=0.5)
    try:
        t0 = BASE_TS + 20_000
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 2, count=8, segment_seconds=1.0)

        recorder.handle_frame(t0, True, boxes=[(10, 20, 30, 40)])  # -> (10,20)-(40,60)
        t1 = t0 + 0.1
        recorder.handle_frame(t1, True, boxes=[(5, 50, 10, 10)])  # -> (5,50)-(15,60); widens the union

        final_ts = t1 + recorder.config.post_buffer_seconds + 0.5
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        clips = _wait_for_clips(tmp_path / "clips", count=1)
        metadata_path = clips[0].with_suffix(".json")
        assert _wait_until(metadata_path.exists)
        metadata = json.loads(metadata_path.read_text())
        assert metadata["bounding_box"] == [5, 20, 35, 40]

        assert list(tmp_path.glob("**/*.jsonl")) == []  # no legacy motion_events.jsonl file anywhere
    finally:
        recorder.stop()


def test_metadata_bounding_box_is_null_when_no_boxes_were_ever_reported(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(tmp_path, cache, pre_buffer_seconds=1, post_buffer_seconds=1)
    try:
        t0 = BASE_TS + 30_000
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 2, count=6, segment_seconds=1.0)

        recorder.handle_frame(t0, True)  # motion detected but no boxes supplied
        final_ts = t0 + recorder.config.post_buffer_seconds + 0.5
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        clips = _wait_for_clips(tmp_path / "clips", count=1)
        metadata = _read_metadata(clips[0])
        assert metadata["bounding_box"] is None
    finally:
        recorder.stop()


def test_writes_companion_metadata_json_on_finalize(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(
        tmp_path, cache, pre_buffer_seconds=1, post_buffer_seconds=1, overlap_seconds=0.5, camera_name="cam1"
    )
    try:
        t0 = BASE_TS + 40_000
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 2, count=10, segment_seconds=1.0)

        recorder.handle_frame(t0, True, boxes=[(0, 0, 10, 10)], score=100, detection_fraction=0.1)
        t1 = t0 + 1.0  # still motion, 1 second later
        recorder.handle_frame(t1, True, boxes=[(0, 0, 20, 20)], score=300, detection_fraction=0.2)
        t2 = t1 + 1.0  # motion stops on this frame
        recorder.handle_frame(t2, False)

        final_ts = t2 + recorder.config.post_buffer_seconds + 0.5
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        clips = _wait_for_clips(tmp_path / "clips", count=1)
        metadata_path = clips[0].with_suffix(".json")
        assert _wait_until(metadata_path.exists)
        metadata = json.loads(metadata_path.read_text())

        assert metadata["schema_version"] == 2
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

        # real, ffprobe'd values from the assembled (real, tiny) clip -- not
        # computed from the recorder's own timestamps
        assert metadata["resolution"] == [64, 64]
        assert metadata["duration_seconds"] > 0

        # 3 frames (t0, t1, t2) spanning 2 real seconds (t0 -> t2) -> ~1fps
        assert metadata["sub_fps_measured"] == pytest.approx(1.0, abs=0.01)

        # the peak score (300) was reported at t1
        assert metadata["peak_motion_time"] == datetime.fromtimestamp(t1).astimezone().isoformat()

        timeline = metadata["motion_timeline"]
        assert [entry["t"] for entry in timeline] == sorted(entry["t"] for entry in timeline)
        assert any(entry["score"] == 300.0 and entry["motion_detected"] for entry in timeline)

        # no fleet-config hashing wired up in this test -- providers default to ""
        assert metadata["config_hash"] == ""
        assert metadata["mask_hash"] == ""
    finally:
        recorder.stop()


def test_metadata_config_and_mask_hash_reflect_what_was_active_at_event_open(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(
        tmp_path,
        cache,
        pre_buffer_seconds=1,
        post_buffer_seconds=1,
        config_hash_provider=lambda: "cfg-abc123",
        mask_hash_provider=lambda: "mask-def456",
    )
    try:
        t0 = BASE_TS + 45_000
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 2, count=6, segment_seconds=1.0)

        recorder.handle_frame(t0, True)
        final_ts = t0 + recorder.config.post_buffer_seconds + 0.5
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        clips = _wait_for_clips(tmp_path / "clips", count=1)
        metadata = _read_metadata(clips[0])
        assert metadata["config_hash"] == "cfg-abc123"
        assert metadata["mask_hash"] == "mask-def456"
    finally:
        recorder.stop()


def test_metadata_start_time_is_pre_buffer_seconds_before_the_triggering_frame(tmp_path):
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(tmp_path, cache, pre_buffer_seconds=3, post_buffer_seconds=1)
    try:
        t0 = BASE_TS + 50_000
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 5, count=10, segment_seconds=1.0)

        recorder.handle_frame(t0, True)
        final_ts = t0 + recorder.config.post_buffer_seconds + 0.5
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        clips = _wait_for_clips(tmp_path / "clips", count=1)
        metadata = _read_metadata(clips[0])
        start_dt = datetime.fromisoformat(metadata["start_time"])
        expected = datetime.fromtimestamp(t0 - 3).astimezone()
        assert abs((start_dt - expected).total_seconds()) < 0.001
    finally:
        recorder.stop()


def test_event_dropped_when_no_cached_segments_are_available(tmp_path):
    """The camera's main-stream passthrough recorder could be disconnected
    for an entire event -- assembly must fail loud (logged) and skip writing
    a clip, not crash the recorder or wedge it for the next event."""
    cache = _make_cache(tmp_path, segment_seconds=0.1)  # cache_dir stays empty -- nothing ever cached
    recorder = _make_recorder(tmp_path, cache, pre_buffer_seconds=1, post_buffer_seconds=0.2)
    try:
        t0 = BASE_TS + 60_000
        recorder.handle_frame(t0, True)
        final_ts = t0 + recorder.config.post_buffer_seconds + 0.3
        recorder.handle_frame(final_ts, False)

        assert _wait_until(lambda: not recorder.is_recording)
        time.sleep(cache.config.segment_seconds + 2.5)  # let the (failed) assembly attempt finish
        assert list((tmp_path / "clips").glob("*.mp4")) == []

        # the recorder must still work for the next event once real segments exist
        t1 = final_ts + 5
        make_cache_segments(cache.config.cache_dir, start_ts=t1 - 2, count=40, segment_seconds=0.1)
        recorder.handle_frame(t1, True)
        recorder.handle_frame(t1 + recorder.config.post_buffer_seconds + 0.3, False)
        assert _wait_until(lambda: not recorder.is_recording)
        assert _wait_until(lambda: len(list((tmp_path / "clips").glob("*.mp4"))) == 1, timeout=8.0)
    finally:
        recorder.stop()


def test_open_event_window_is_never_pruned_from_the_cache(tmp_path):
    # prune()'s cutoff is measured against the real wall clock -- unlike the
    # other tests in this file, the fabricated segment timestamps here must
    # be real-time-relative for that comparison to mean anything.
    cache = _make_cache(tmp_path, segment_seconds=1.0)
    recorder = _make_recorder(tmp_path, cache, pre_buffer_seconds=5, post_buffer_seconds=30)
    try:
        t0 = time.time()
        # Segments spanning well before the pre-buffer window -- these are
        # legitimately stale and should be prunable...
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 100, count=5, segment_seconds=1.0)
        # ...but the event about to open needs [t0-5, ...), which overlaps
        # some of what a naive age-based prune would otherwise sweep away.
        make_cache_segments(cache.config.cache_dir, start_ts=t0 - 6, count=3, segment_seconds=1.0)

        recorder.handle_frame(t0, True)  # opens a window at t0-5; post_buffer is long, so it stays open
        assert recorder.is_recording

        cache.prune(keep_seconds=1)  # would delete nearly everything if the window weren't protected

        protected_path = cache.config.cache_dir / segment_name(t0 - 5)
        assert protected_path.exists()
    finally:
        recorder.stop()
