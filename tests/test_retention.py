import os
import time

from camera_watcher.recorder import TEMP_SUFFIX
from camera_watcher.retention import RetentionConfig, enforce_retention


def _touch(path, size, mtime_offset):
    path.write_bytes(b"0" * size)
    now = time.time()
    os.utime(path, (now + mtime_offset, now + mtime_offset))


def test_age_based_removal(tmp_path):
    old = tmp_path / "cam_20200101_000000.mp4"
    new = tmp_path / "cam_20990101_000000.mp4"
    _touch(old, 10, -100 * 86400)
    _touch(new, 10, 0)

    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, max_age_days=30))
    assert old in removed
    assert new not in removed
    assert not old.exists()
    assert new.exists()


def test_size_based_removal_oldest_first(tmp_path):
    a = tmp_path / "cam_a.mp4"
    b = tmp_path / "cam_b.mp4"
    c = tmp_path / "cam_c.mp4"
    _touch(a, 1024 * 1024, -300)
    _touch(b, 1024 * 1024, -200)
    _touch(c, 1024 * 1024, -100)

    max_gb = 2 * 1024 * 1024 / (1024**3)  # ~2MB cap, forces exactly one removal
    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, max_total_gb=max_gb))
    assert a in removed
    assert len(removed) == 1
    assert b.exists() and c.exists()


def test_temp_files_are_never_touched(tmp_path):
    temp = tmp_path / ("cam_active.mp4" + TEMP_SUFFIX)
    _touch(temp, 10, -1000 * 86400)

    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, max_age_days=1))
    assert removed == []
    assert temp.exists()


def test_age_based_removal_also_removes_companion_metadata(tmp_path):
    old = tmp_path / "cam_20200101_000000.mp4"
    old_metadata = tmp_path / "cam_20200101_000000.json"
    _touch(old, 10, -100 * 86400)
    old_metadata.write_text("{}")

    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, max_age_days=30))
    assert old in removed
    assert not old.exists()
    assert not old_metadata.exists()


def test_size_based_removal_also_removes_companion_metadata(tmp_path):
    a = tmp_path / "cam_a.mp4"
    a_metadata = tmp_path / "cam_a.json"
    b = tmp_path / "cam_b.mp4"
    _touch(a, 1024 * 1024, -300)
    a_metadata.write_text("{}")
    _touch(b, 1024 * 1024, -200)

    max_gb = 1024 * 1024 / (1024**3)  # forces removal of the older clip (a) only
    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, max_total_gb=max_gb))
    assert a in removed
    assert not a.exists()
    assert not a_metadata.exists()
    assert b.exists()
