import json
import logging
import os
import time

from camera_watcher.recorder import TEMP_SUFFIX
from camera_watcher.retention import RetentionConfig, classify_tier, enforce_global_retention, enforce_retention


def _touch(path, size, mtime_offset):
    path.write_bytes(b"0" * size)
    now = time.time()
    os.utime(path, (now + mtime_offset, now + mtime_offset))


def _analysis(clip, verdict, reason="test"):
    clip.with_name(clip.stem + ".analysis.json").write_text(json.dumps({"verdict": verdict, "reason": reason}))


def _review(clip, decision):
    clip.with_name(clip.stem + ".review.json").write_text(json.dumps({"reviewed_at": "x", "decision": decision}))


# ---------- classify_tier ----------


def test_classify_tier_with_no_analysis_sidecar_is_high(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    assert classify_tier(clip) == "high"


def test_classify_tier_error_verdict_is_high(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    _analysis(clip, "error")
    assert classify_tier(clip) == "high"


def test_classify_tier_low_verdict_is_low(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    _analysis(clip, "low")
    assert classify_tier(clip) == "low"


def test_classify_tier_high_verdict_is_high(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    _analysis(clip, "high")
    assert classify_tier(clip) == "high"


def test_classify_tier_review_verdict_with_no_review_sidecar_is_review(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    _analysis(clip, "review")
    assert classify_tier(clip) == "review"


def test_classify_tier_review_verdict_reviewed_keep_is_promoted_to_high(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    _analysis(clip, "review")
    _review(clip, "keep")
    assert classify_tier(clip) == "high"


def test_classify_tier_review_verdict_reviewed_discard_stays_review(tmp_path):
    # In practice "discard" deletes the clip immediately (see
    # camera_watcher.web.routes), so this is a defensive edge case: a
    # review.json with decision != "keep" doesn't promote the tier.
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    _analysis(clip, "review")
    _review(clip, "discard")
    assert classify_tier(clip) == "review"


def test_classify_tier_treats_a_corrupt_analysis_sidecar_as_unclassified(tmp_path, caplog):
    clip = tmp_path / "cam_a.mp4"
    clip.write_bytes(b"x")
    clip.with_name("cam_a.analysis.json").write_text("{not valid json")
    assert classify_tier(clip) == "high"


# ---------- age-based expiry, per tier ----------


def test_low_tier_clip_expires_after_its_hour_window(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -3 * 3600)  # 3 hours old
    _analysis(clip, "low")

    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, low_max_age_hours=1, high_max_age_days=None, review_max_age_days=None))
    assert removed == [clip]
    assert not clip.exists()


def test_low_tier_clip_survives_within_its_hour_window(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -30 * 60)  # 30 minutes old
    _analysis(clip, "low")

    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, low_max_age_hours=1, high_max_age_days=None, review_max_age_days=None))
    assert removed == []
    assert clip.exists()


def test_high_tier_clip_expires_after_its_day_window(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -40 * 86400)  # 40 days old
    _analysis(clip, "high")

    removed = enforce_retention(RetentionConfig(output_dir=tmp_path, low_max_age_hours=None, high_max_age_days=30, review_max_age_days=None))
    assert removed == [clip]


def test_unclassified_clip_uses_the_high_window_not_a_shorter_one(tmp_path):
    # No analysis sidecar at all -- classify_tier treats it as "high", so a
    # tight low_max_age_hours must NOT apply to it.
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -5 * 3600)  # 5 hours old

    removed = enforce_retention(
        RetentionConfig(output_dir=tmp_path, low_max_age_hours=1, high_max_age_days=30, review_max_age_days=1)
    )
    assert removed == []
    assert clip.exists()


def test_review_tier_clip_expires_after_its_day_window_and_logs_staleness(tmp_path, caplog):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -40 * 86400)  # 40 days old
    _analysis(clip, "review", reason="persistent_detection")

    with caplog.at_level(logging.WARNING):
        removed = enforce_retention(
            RetentionConfig(output_dir=tmp_path, low_max_age_hours=None, high_max_age_days=None, review_max_age_days=30)
        )
    assert removed == [clip]
    assert "never reviewed" in caplog.text


def test_review_tier_clip_reviewed_keep_uses_the_high_window_instead(tmp_path):
    # review_max_age_days is tight (1 day) but this clip was reviewed and
    # kept, so it's promoted to "high" and uses high_max_age_days (30) --
    # 5 days old must survive.
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -5 * 86400)
    _analysis(clip, "review")
    _review(clip, "keep")

    removed = enforce_retention(
        RetentionConfig(output_dir=tmp_path, low_max_age_hours=None, high_max_age_days=30, review_max_age_days=1)
    )
    assert removed == []
    assert clip.exists()


def test_age_expiry_zero_disables_that_tiers_window(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -1000 * 86400)  # ancient
    _analysis(clip, "low")

    removed = enforce_retention(
        RetentionConfig(output_dir=tmp_path, low_max_age_hours=0, high_max_age_days=None, review_max_age_days=None)
    )
    assert removed == []
    assert clip.exists()


def test_temp_files_are_never_touched(tmp_path):
    temp = tmp_path / ("cam_active.mp4" + TEMP_SUFFIX)
    _touch(temp, 10, -1000 * 86400)

    removed = enforce_retention(RetentionConfig(output_dir=tmp_path))
    assert removed == []
    assert temp.exists()


def test_expiry_removes_the_whole_sidecar_family(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -40 * 86400)
    metadata = tmp_path / "cam_a.json"
    metadata.write_text("{}")
    _analysis(clip, "low")
    review = tmp_path / "cam_a.review.json"
    review.write_text("{}")

    removed = enforce_retention(
        RetentionConfig(output_dir=tmp_path, low_max_age_hours=1, high_max_age_days=None, review_max_age_days=None)
    )
    assert removed == [clip]
    assert not clip.exists()
    assert not metadata.exists()
    assert not (tmp_path / "cam_a.analysis.json").exists()
    assert not review.exists()


# ---------- size-budget phase: tier-priority ordering ----------


def test_budget_pressure_deletes_low_tier_before_high_or_review(tmp_path):
    low = tmp_path / "cam_low.mp4"
    high = tmp_path / "cam_high.mp4"
    review = tmp_path / "cam_review.mp4"
    # low is the newest of the three, high/review are older -- proves tier
    # beats age for what gets picked first.
    _touch(high, 1024 * 1024, -300)
    _touch(review, 1024 * 1024, -200)
    _touch(low, 1024 * 1024, -100)
    _analysis(high, "high")
    _analysis(review, "review")
    _analysis(low, "low")

    max_gb = 2 * 1024 * 1024 / (1024**3)  # forces exactly one removal
    removed = enforce_retention(
        RetentionConfig(output_dir=tmp_path, low_max_age_hours=None, high_max_age_days=None, review_max_age_days=None, max_total_gb=max_gb)
    )
    assert removed == [low]
    assert high.exists() and review.exists()


def test_budget_pressure_falls_back_to_oldest_of_high_and_review_once_low_is_gone(tmp_path):
    high = tmp_path / "cam_high.mp4"  # older
    review = tmp_path / "cam_review.mp4"  # newer
    _touch(high, 1024 * 1024, -300)
    _touch(review, 1024 * 1024, -100)
    _analysis(high, "high")
    _analysis(review, "review")

    max_gb = 1024 * 1024 / (1024**3)  # forces exactly one removal
    removed = enforce_retention(
        RetentionConfig(output_dir=tmp_path, low_max_age_hours=None, high_max_age_days=None, review_max_age_days=None, max_total_gb=max_gb)
    )
    assert removed == [high]  # oldest of the (equal-priority) high/review pool
    assert review.exists()


def test_budget_pressure_logs_when_it_deletes_a_high_value_clip(tmp_path, caplog):
    high = tmp_path / "cam_high.mp4"
    _touch(high, 1024 * 1024, -100)
    _analysis(high, "high")

    with caplog.at_level(logging.WARNING):
        removed = enforce_retention(
            RetentionConfig(output_dir=tmp_path, low_max_age_hours=None, high_max_age_days=None, review_max_age_days=None, max_total_gb=0.0000001)
        )
    assert removed == [high]
    assert "Budget pressure" in caplog.text
    assert "high-tier" in caplog.text


def test_budget_pressure_removes_the_whole_sidecar_family(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 1024 * 1024, -100)
    metadata = tmp_path / "cam_a.json"
    metadata.write_text("{}")
    _analysis(clip, "high")

    max_gb = 1 / (1024**3)  # trivially small -- forces removal
    removed = enforce_retention(
        RetentionConfig(output_dir=tmp_path, low_max_age_hours=None, high_max_age_days=None, review_max_age_days=None, max_total_gb=max_gb)
    )
    assert removed == [clip]
    assert not metadata.exists()
    assert not (tmp_path / "cam_a.analysis.json").exists()


# ---------- global (fleet-wide) retention ----------


def test_global_retention_sweeps_every_camera_subdir_by_age(tmp_path):
    clips_root = tmp_path / "clips"
    cam1_old = clips_root / "cam1" / "cam1_a.mp4"
    cam2_old = clips_root / "cam2" / "cam2_a.mp4"
    cam1_new = clips_root / "cam1" / "cam1_b.mp4"
    for p in (cam1_old, cam2_old, cam1_new):
        p.parent.mkdir(parents=True, exist_ok=True)
    _touch(cam1_old, 10, -40 * 86400)
    _touch(cam2_old, 10, -40 * 86400)
    _touch(cam1_new, 10, 0)

    removed = enforce_global_retention(clips_root, low_max_age_hours=None, high_max_age_days=30, review_max_age_days=None)
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
    removed = enforce_global_retention(
        clips_root, low_max_age_hours=None, high_max_age_days=None, review_max_age_days=None, max_total_gb=max_gb
    )
    assert removed == [cam1]  # globally oldest, regardless of which camera it belongs to
    assert cam2.exists() and cam1_newest.exists()


def test_global_retention_on_a_missing_or_empty_clips_root_does_nothing(tmp_path):
    assert enforce_global_retention(tmp_path / "does-not-exist") == []
    empty_root = tmp_path / "clips"
    empty_root.mkdir()
    assert enforce_global_retention(empty_root) == []


def test_global_retention_ignores_stray_files_directly_under_clips_root(tmp_path):
    clips_root = tmp_path / "clips"
    clips_root.mkdir()
    stray = clips_root / "not-a-camera-dir.mp4"
    _touch(stray, 10, -40 * 86400)

    removed = enforce_global_retention(clips_root, low_max_age_hours=None, high_max_age_days=30, review_max_age_days=None)
    assert removed == []
    assert stray.exists()  # only camera *subdirectories* are swept, not loose files


# ---------- --dry-run ----------


def test_dry_run_reports_expired_clips_without_deleting_them(tmp_path):
    clip = tmp_path / "cam_a.mp4"
    _touch(clip, 10, -40 * 86400)
    metadata = tmp_path / "cam_a.json"
    metadata.write_text("{}")
    _analysis(clip, "low")

    removed = enforce_retention(
        RetentionConfig(
            output_dir=tmp_path,
            low_max_age_hours=1,
            high_max_age_days=None,
            review_max_age_days=None,
            dry_run=True,
        )
    )
    assert removed == [clip]
    assert clip.exists()  # dry run -- nothing actually deleted
    assert metadata.exists()
    assert (tmp_path / "cam_a.analysis.json").exists()


def test_dry_run_reports_budget_pressure_deletions_without_deleting_them(tmp_path):
    low = tmp_path / "cam_low.mp4"
    high = tmp_path / "cam_high.mp4"
    _touch(high, 1024 * 1024, -300)
    _touch(low, 1024 * 1024, -100)
    _analysis(high, "high")
    _analysis(low, "low")

    max_gb = 1024 * 1024 / (1024**3)  # forces exactly one removal
    removed = enforce_retention(
        RetentionConfig(
            output_dir=tmp_path,
            low_max_age_hours=None,
            high_max_age_days=None,
            review_max_age_days=None,
            max_total_gb=max_gb,
            dry_run=True,
        )
    )
    assert removed == [low]
    assert low.exists() and high.exists()  # dry run -- nothing actually deleted


def test_dry_run_still_logs_review_staleness_and_budget_pressure_warnings(tmp_path, caplog):
    review = tmp_path / "cam_review.mp4"
    _touch(review, 10, -40 * 86400)
    _analysis(review, "review")

    with caplog.at_level(logging.WARNING):
        removed = enforce_retention(
            RetentionConfig(
                output_dir=tmp_path,
                low_max_age_hours=None,
                high_max_age_days=None,
                review_max_age_days=30,
                dry_run=True,
            )
        )
    assert removed == [review]
    assert review.exists()
    assert "never reviewed" in caplog.text
    assert "Would remove" in caplog.text


def test_dry_run_global_retention_deletes_nothing(tmp_path):
    clips_root = tmp_path / "clips"
    old = clips_root / "cam1" / "cam1_a.mp4"
    old.parent.mkdir(parents=True, exist_ok=True)
    _touch(old, 10, -40 * 86400)

    removed = enforce_global_retention(
        clips_root, low_max_age_hours=None, high_max_age_days=30, review_max_age_days=None, dry_run=True
    )
    assert removed == [old]
    assert old.exists()


def test_global_retention_prioritizes_tier_over_age_for_the_shared_budget(tmp_path, monkeypatch):
    """Proves the fleet-wide budget sweep actually consults classify_tier
    (not just age) -- a newer low-value clip is deleted before an older
    high-value one."""
    clips_root = tmp_path / "clips"
    low_value = clips_root / "cam1" / "newer_but_low_value.mp4"  # newer...
    high_value = clips_root / "cam1" / "older_but_high_value.mp4"  # ...but older
    low_value.parent.mkdir(parents=True, exist_ok=True)
    _touch(low_value, 1024 * 1024, -100)  # newer mtime
    _touch(high_value, 1024 * 1024, -200)  # older mtime
    _analysis(low_value, "low")
    _analysis(high_value, "high")

    max_gb = 1024 * 1024 / (1024**3)  # forces exactly one removal
    removed = enforce_global_retention(
        clips_root, low_max_age_hours=None, high_max_age_days=None, review_max_age_days=None, max_total_gb=max_gb
    )
    assert removed == [low_value]  # deleted first for being low-tier, despite being newer
    assert high_value.exists()
