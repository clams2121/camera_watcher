import json
import os
import time

from camera_watcher.recorder import TEMP_SUFFIX
from camera_watcher.retention import RetentionConfig, classify_tier, enforce_global_retention, enforce_retention


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


def test_global_retention_sweeps_every_camera_subdir_by_age(tmp_path):
    clips_root = tmp_path / "clips"
    cam1_old = clips_root / "cam1" / "cam1_20200101_000000.mp4"
    cam2_old = clips_root / "cam2" / "cam2_20200101_000000.mp4"
    cam1_new = clips_root / "cam1" / "cam1_20990101_000000.mp4"
    for p in (cam1_old, cam2_old, cam1_new):
        p.parent.mkdir(parents=True, exist_ok=True)
    _touch(cam1_old, 10, -100 * 86400)
    _touch(cam2_old, 10, -100 * 86400)
    _touch(cam1_new, 10, 0)

    removed = enforce_global_retention(clips_root, max_age_days=30)
    assert set(removed) == {cam1_old, cam2_old}
    assert cam1_new.exists()


def test_global_retention_enforces_one_shared_size_budget_across_cameras(tmp_path):
    clips_root = tmp_path / "clips"
    cam1 = clips_root / "cam1" / "a.mp4"  # oldest
    cam2 = clips_root / "cam2" / "b.mp4"  # middle
    cam1_newest = clips_root / "cam1" / "c.mp4"  # newest
    for p in (cam1, cam2, cam1_newest):
        p.parent.mkdir(parents=True, exist_ok=True)
    _touch(cam1, 1024 * 1024, -300)
    _touch(cam2, 1024 * 1024, -200)
    _touch(cam1_newest, 1024 * 1024, -100)

    max_gb = 2 * 1024 * 1024 / (1024**3)  # ~2MB combined cap -- forces exactly one removal, fleet-wide
    removed = enforce_global_retention(clips_root, max_total_gb=max_gb)
    assert removed == [cam1]  # globally oldest, regardless of which camera it belongs to
    assert cam2.exists() and cam1_newest.exists()


def test_global_retention_on_a_missing_or_empty_clips_root_does_nothing(tmp_path):
    assert enforce_global_retention(tmp_path / "does-not-exist", max_age_days=1) == []
    empty_root = tmp_path / "clips"
    empty_root.mkdir()
    assert enforce_global_retention(empty_root, max_age_days=1) == []


def test_global_retention_ignores_stray_files_directly_under_clips_root(tmp_path):
    clips_root = tmp_path / "clips"
    clips_root.mkdir()
    stray = clips_root / "not-a-camera-dir.mp4"
    _touch(stray, 10, -100 * 86400)

    removed = enforce_global_retention(clips_root, max_age_days=30)
    assert removed == []
    assert stray.exists()  # only camera *subdirectories* are swept, not loose files


def test_classify_tier_defaults_to_unclassified():
    assert classify_tier({}) == "unclassified"
    assert classify_tier({"schema_version": 2}) == "unclassified"


def test_global_retention_consults_classify_tier_for_deletion_priority(tmp_path, monkeypatch):
    """Even though classify_tier always returns "unclassified" today, the
    sort must actually call it (not just exist) -- this proves the seam
    is wired in, using a monkeypatched tier function to make a *newer*
    clip get deleted before an *older* one."""
    clips_root = tmp_path / "clips"
    low_value = clips_root / "cam1" / "newer_but_low_value.mp4"  # newer...
    high_value = clips_root / "cam1" / "older_but_high_value.mp4"  # ...but older
    low_value.parent.mkdir(parents=True, exist_ok=True)
    _touch(low_value, 1024 * 1024, -100)  # newer mtime
    _touch(high_value, 1024 * 1024, -200)  # older mtime
    (low_value.with_suffix(".json")).write_text(json.dumps({"tier": "low_value"}))
    (high_value.with_suffix(".json")).write_text(json.dumps({"tier": "high_value"}))

    import camera_watcher.retention as retention_module

    monkeypatch.setattr(retention_module, "classify_tier", lambda metadata: metadata.get("tier", "unclassified"))

    max_gb = 1024 * 1024 / (1024**3)  # forces exactly one removal
    removed = enforce_global_retention(clips_root, max_total_gb=max_gb)
    assert removed == [low_value]  # deleted first for being low_value, despite being newer
    assert high_value.exists()
