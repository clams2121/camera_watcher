import json
import time
from pathlib import Path

from clip_classifier.watcher import (
    ClipWatcher,
    analysis_path_for,
    discover_pending_clips,
    review_path_for,
)


def _write_clip(clips_root, camera, name, mtime_offset=0.0):
    camera_dir = clips_root / camera
    camera_dir.mkdir(parents=True, exist_ok=True)
    clip = camera_dir / f"{name}.mp4"
    clip.write_bytes(b"fake")
    metadata = camera_dir / f"{name}.json"
    metadata.write_text(json.dumps({"event_id": name}))
    if mtime_offset:
        import os

        now = time.time()
        os.utime(metadata, (now + mtime_offset, now + mtime_offset))
    return clip


def test_analysis_and_review_path_naming():
    clip = Path("/data/clips/cam1/cam1_20260101_000000.mp4")
    assert analysis_path_for(clip) == Path("/data/clips/cam1/cam1_20260101_000000.analysis.json")
    assert review_path_for(clip) == Path("/data/clips/cam1/cam1_20260101_000000.review.json")


def test_discover_finds_finalized_clips_across_camera_dirs(tmp_path):
    clips_root = tmp_path / "clips"
    a = _write_clip(clips_root, "cam1", "cam1_a")
    b = _write_clip(clips_root, "cam2", "cam2_b")

    pending = discover_pending_clips(tmp_path)
    assert set(pending) == {a, b}


def test_discover_skips_already_classified_clips(tmp_path):
    clips_root = tmp_path / "clips"
    clip = _write_clip(clips_root, "cam1", "cam1_a")
    analysis_path_for(clip).write_text("{}")

    assert discover_pending_clips(tmp_path) == []


def test_discover_skips_clips_with_no_metadata_sidecar(tmp_path):
    clips_root = tmp_path / "clips"
    camera_dir = clips_root / "cam1"
    camera_dir.mkdir(parents=True)
    (camera_dir / "orphan.mp4").write_bytes(b"fake")  # no .json -- still recording, or genuinely orphaned

    assert discover_pending_clips(tmp_path) == []


def test_discover_ignores_temp_recording_files_directly(tmp_path):
    """A clip mid-recording has only a .rec.mp4 (see recorder.TEMP_SUFFIX)
    and no metadata sidecar at all yet -- discovery must never surface it,
    exactly because it keys off the sidecar rather than globbing *.mp4."""
    clips_root = tmp_path / "clips"
    camera_dir = clips_root / "cam1"
    camera_dir.mkdir(parents=True)
    (camera_dir / "cam1_active.mp4.rec.mp4").write_bytes(b"still recording")

    assert discover_pending_clips(tmp_path) == []


def test_discover_ignores_analysis_and_review_sidecars_as_if_they_were_metadata(tmp_path):
    """A stray *.analysis.json or *.review.json with no *.mp4 next to it
    (e.g. the clip was since deleted) must never be mistaken for a clip's
    own metadata sidecar."""
    clips_root = tmp_path / "clips"
    camera_dir = clips_root / "cam1"
    camera_dir.mkdir(parents=True)
    (camera_dir / "gone.analysis.json").write_text("{}")
    (camera_dir / "gone.review.json").write_text("{}")

    assert discover_pending_clips(tmp_path) == []


def test_discover_orders_oldest_metadata_first(tmp_path):
    clips_root = tmp_path / "clips"
    newer = _write_clip(clips_root, "cam1", "cam1_newer", mtime_offset=0)
    older = _write_clip(clips_root, "cam1", "cam1_older", mtime_offset=-1000)

    assert discover_pending_clips(tmp_path) == [older, newer]


def test_discover_on_empty_or_missing_data_root(tmp_path):
    assert discover_pending_clips(tmp_path / "does-not-exist") == []
    (tmp_path / "clips").mkdir()
    assert discover_pending_clips(tmp_path) == []


def test_watcher_backfills_existing_pending_clips_on_start(tmp_path):
    clips_root = tmp_path / "clips"
    clip = _write_clip(clips_root, "cam1", "cam1_a")

    watcher = ClipWatcher(tmp_path, queue_maxsize=8, rescan_interval_seconds=999)
    watcher.start()
    try:
        assert watcher.get(timeout=2) == clip
        assert watcher.get(timeout=0.2) is None  # nothing else pending
    finally:
        watcher.stop()


def test_watcher_picks_up_a_new_clip_written_after_start(tmp_path):
    clips_root = tmp_path / "clips"
    watcher = ClipWatcher(tmp_path, queue_maxsize=8, rescan_interval_seconds=999)
    watcher.start()
    try:
        assert watcher.get(timeout=0.2) is None  # nothing yet

        # Mimics the recorder's own write pattern: temp file, then an
        # atomic rename into the final name -- inotify sees this as a move.
        camera_dir = clips_root / "cam1"
        camera_dir.mkdir(parents=True)
        clip = camera_dir / "cam1_new.mp4"
        clip.write_bytes(b"fake")
        metadata = camera_dir / "cam1_new.json"
        tmp_metadata = camera_dir / "cam1_new.tmp.json"
        tmp_metadata.write_text(json.dumps({"event_id": "cam1_new"}))
        tmp_metadata.replace(metadata)

        assert watcher.get(timeout=3) == clip
    finally:
        watcher.stop()


def test_watcher_never_reports_the_recorder_temp_json_itself(tmp_path):
    clips_root = tmp_path / "clips"
    watcher = ClipWatcher(tmp_path, queue_maxsize=8, rescan_interval_seconds=999)
    watcher.start()
    try:
        camera_dir = clips_root / "cam1"
        camera_dir.mkdir(parents=True)
        (camera_dir / "cam1_new.mp4").write_bytes(b"fake")
        # Only the temp file appears -- the recorder hasn't renamed it yet.
        (camera_dir / "cam1_new.tmp.json").write_text(json.dumps({"event_id": "cam1_new"}))

        assert watcher.get(timeout=0.5) is None
    finally:
        watcher.stop()


def test_watcher_deduplicates_a_clip_already_queued(tmp_path):
    clips_root = tmp_path / "clips"
    clip = _write_clip(clips_root, "cam1", "cam1_a")

    watcher = ClipWatcher(tmp_path, queue_maxsize=8, rescan_interval_seconds=0.2)
    watcher.start()
    try:
        assert watcher.get(timeout=2) == clip
        # The rescan loop will re-discover this same still-unclassified clip
        # (nothing writes its analysis sidecar in this test) -- it must not
        # pile up duplicate entries in the queue while one's already queued
        # or in flight. Since it was already dequeued above, the dedup set
        # no longer blocks it, so it's fine for it to reappear once; the
        # real guarantee under test is no *unbounded* duplication.
        time.sleep(0.5)
        assert watcher.qsize() <= 1
    finally:
        watcher.stop()
