import pytest

from camera_watcher.assemble import AssemblyError, assemble_clip
from tests.ffmpeg_helpers import make_segment, probe_duration_seconds


def test_assemble_clip_concatenates_real_segments_with_zero_reencoding(tmp_path):
    segments = [tmp_path / "cache" / f"seg{i}.mp4" for i in range(3)]
    for seg in segments:
        make_segment(seg, duration=0.5)

    output = tmp_path / "clips" / "out.mp4"
    assemble_clip(segments, output)

    assert output.exists()
    # concat -c copy: duration should be close to the sum of the sources'
    # (not exact -- concat/keyframe boundaries can shave a few ms) with
    # no re-encoding involved at all.
    assert probe_duration_seconds(output) == pytest.approx(1.5, abs=0.3)


def test_assemble_clip_raises_on_empty_segment_list(tmp_path):
    with pytest.raises(AssemblyError):
        assemble_clip([], tmp_path / "clips" / "out.mp4")


def test_assemble_clip_raises_when_a_segment_is_missing(tmp_path):
    real = tmp_path / "cache" / "real.mp4"
    make_segment(real, duration=0.3)
    missing = tmp_path / "cache" / "missing.mp4"

    with pytest.raises(AssemblyError):
        assemble_clip([real, missing], tmp_path / "clips" / "out.mp4")


def test_assemble_clip_handles_paths_with_special_characters(tmp_path):
    seg_dir = tmp_path / "it's a cache' dir"
    segment = seg_dir / "seg.mp4"
    make_segment(segment, duration=0.3)

    output = tmp_path / "clips" / "out.mp4"
    assemble_clip([segment], output)
    assert output.exists()
