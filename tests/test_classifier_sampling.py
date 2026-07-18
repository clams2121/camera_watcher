from pathlib import Path

import numpy as np
import pytest

from clip_classifier.sampling import extract_frame, sample_frames, select_offsets
from tests.ffmpeg_helpers import make_segment


def _v2_metadata(**overrides):
    metadata = {
        "schema_version": 2,
        "event_id": "cam1_20260101_120000",
        "start_time": "2026-01-01T12:00:00+00:00",
        "end_time": "2026-01-01T12:00:10+00:00",
        "duration_seconds": 10.0,
        "peak_motion_time": "2026-01-01T12:00:04+00:00",
        "motion_timeline": [
            {"t": 1, "score": 10.0, "motion_detected": True},
            {"t": 2, "score": 50.0, "motion_detected": True},
            {"t": 4, "score": 300.0, "motion_detected": True},  # matches peak_motion_time -- a guaranteed duplicate
            {"t": 5, "score": 290.0, "motion_detected": True},
            {"t": 8, "score": 150.0, "motion_detected": True},
        ],
    }
    metadata.update(overrides)
    return metadata


def test_v2_always_includes_the_peak_motion_offset():
    offsets, used_fallback = select_offsets(_v2_metadata(), max_frames=5, min_spacing_seconds=1.0)
    assert 4.0 in offsets
    assert used_fallback is False


def test_v2_fills_remaining_slots_with_highest_scores_first():
    # Only 5 distinct seconds exist in motion_timeline and max_frames=5, so
    # every one of them (deduplicated against the peak) ends up chosen --
    # what this actually tests is that duplicate-of-peak (t=4, same second
    # as peak_motion_time) is the one dropped for spacing, not some other
    # entry, i.e. the anchor doesn't get double-counted against its budget.
    offsets, _ = select_offsets(_v2_metadata(), max_frames=5, min_spacing_seconds=1.0)
    assert offsets == sorted(offsets)
    assert offsets == [1.0, 2.0, 4.0, 5.0, 8.0]


def test_v2_enforces_minimum_spacing_between_offsets():
    metadata = _v2_metadata(
        peak_motion_time=None,
        motion_timeline=[
            {"t": 5, "score": 100.0, "motion_detected": True},
            {"t": 5.5, "score": 99.0, "motion_detected": True},  # too close to t=5
            {"t": 9, "score": 98.0, "motion_detected": True},
        ],
    )
    offsets, _ = select_offsets(metadata, max_frames=5, min_spacing_seconds=1.0)
    assert 5.5 not in offsets
    assert 5.0 in offsets
    assert 9.0 in offsets


def test_v2_respects_max_frames_cap():
    metadata = _v2_metadata(
        peak_motion_time=None,
        motion_timeline=[{"t": i, "score": float(i)} for i in range(0, 20, 2)],
    )
    offsets, _ = select_offsets(metadata, max_frames=3, min_spacing_seconds=1.0)
    assert len(offsets) == 3


def test_v2_clamps_offsets_to_duration_seconds():
    metadata = _v2_metadata(
        duration_seconds=6.0,
        peak_motion_time="2026-01-01T12:00:04+00:00",
        motion_timeline=[
            {"t": 2, "score": 10.0},
            {"t": 8, "score": 999.0},  # would be picked first by score, but past duration
        ],
    )
    offsets, _ = select_offsets(metadata, max_frames=5, min_spacing_seconds=1.0)
    assert 8.0 not in offsets
    assert all(0 <= o <= 6.0 for o in offsets)


def test_v2_with_no_motion_data_falls_back_to_even_spacing(caplog):
    metadata = _v2_metadata(peak_motion_time=None, motion_timeline=[], duration_seconds=10.0)
    offsets, used_fallback = select_offsets(metadata, max_frames=4, min_spacing_seconds=1.0)
    assert used_fallback is True
    assert len(offsets) == 4
    assert "falling back to even sampling" in caplog.text


def test_v1_metadata_falls_back_to_even_spacing_and_logs_loudly(caplog):
    metadata = {
        "event_id": "cam1_20250101_000000",
        "start_time": "2025-01-01T00:00:00+00:00",
        "end_time": "2025-01-01T00:00:20+00:00",
        # no schema_version, no peak_motion_time, no motion_timeline -- v1 shape
    }
    offsets, used_fallback = select_offsets(metadata, max_frames=4, min_spacing_seconds=1.0)
    assert used_fallback is True
    assert len(offsets) == 4
    assert max(offsets) < 20.0
    assert "schema v1" in caplog.text


def test_even_spacing_covers_the_full_duration_range():
    metadata = {
        "schema_version": 1,
        "event_id": "cam1",
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:10+00:00",
    }
    offsets, _ = select_offsets(metadata, max_frames=5, min_spacing_seconds=1.0)
    assert offsets[0] > 0
    assert offsets[-1] < 10.0
    assert offsets == sorted(offsets)


def test_offsets_are_always_returned_sorted():
    metadata = _v2_metadata(
        peak_motion_time=None,
        motion_timeline=[
            {"t": 9, "score": 500.0},
            {"t": 1, "score": 400.0},
            {"t": 5, "score": 300.0},
        ],
    )
    offsets, _ = select_offsets(metadata, max_frames=5, min_spacing_seconds=1.0)
    assert offsets == sorted(offsets)


# ---------- real ffmpeg frame extraction ----------


def test_extract_frame_decodes_a_real_frame(tmp_path):
    video = tmp_path / "clip.mp4"
    make_segment(video, duration=2.0, size="32x32")

    frame = extract_frame(video, 0.5)

    assert frame is not None
    assert isinstance(frame, np.ndarray)
    assert frame.shape[2] == 3  # BGR
    assert frame.shape[0] > 0 and frame.shape[1] > 0


def test_extract_frame_past_end_of_video_returns_none(tmp_path):
    video = tmp_path / "clip.mp4"
    make_segment(video, duration=0.3, size="32x32")

    frame = extract_frame(video, 100.0)

    assert frame is None


def test_extract_frame_on_nonexistent_file_returns_none(tmp_path):
    frame = extract_frame(tmp_path / "does-not-exist.mp4", 0.0)
    assert frame is None


def test_sample_frames_end_to_end_with_a_real_clip(tmp_path):
    video = tmp_path / "clip.mp4"
    make_segment(video, duration=3.0, size="32x32")

    metadata = _v2_metadata(
        duration_seconds=3.0,
        peak_motion_time="2026-01-01T12:00:01+00:00",
        motion_timeline=[
            {"t": 0, "score": 10.0},
            {"t": 1, "score": 300.0},
            {"t": 2, "score": 50.0},
        ],
    )

    offsets, frames, used_fallback = sample_frames(video, metadata, max_frames=3, min_spacing_seconds=1.0)

    assert used_fallback is False
    assert len(offsets) == len(frames)
    assert len(frames) >= 1
    for frame in frames:
        assert isinstance(frame, np.ndarray)


def test_sample_frames_raises_when_every_offset_fails(tmp_path):
    from clip_classifier.sampling import SamplingError

    video = tmp_path / "clip.mp4"
    make_segment(video, duration=0.2, size="32x32")

    # No duration_seconds -- nothing clamps this offset, so it stays a
    # genuinely unreachable seek target on the real (0.2s) file.
    metadata = _v2_metadata(peak_motion_time=None, motion_timeline=[{"t": 500, "score": 1.0}])
    del metadata["duration_seconds"]

    with pytest.raises(SamplingError):
        sample_frames(video, metadata, max_frames=1, min_spacing_seconds=1.0)
