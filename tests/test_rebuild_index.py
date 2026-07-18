import json
import sys

import pytest

from camera_watcher.rebuild_index import main, rebuild_index


def _write_clip(clips_dir, name, start_time=None, extra=None):
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / f"{name}.mp4").write_bytes(b"fake video")
    metadata = {"schema_version": 2, "event_id": name, "start_time": start_time or "2026-01-01T00:00:00-05:00"}
    if extra:
        metadata.update(extra)
    (clips_dir / f"{name}.json").write_text(json.dumps(metadata))


def test_rebuild_index_writes_a_sorted_flat_index(tmp_path):
    clips_dir = tmp_path / "clips"
    _write_clip(clips_dir, "cam_c", start_time="2026-01-01T02:00:00-05:00")
    _write_clip(clips_dir, "cam_a", start_time="2026-01-01T00:00:00-05:00")
    _write_clip(clips_dir, "cam_b", start_time="2026-01-01T01:00:00-05:00")

    index_path = clips_dir / "index.jsonl"
    count = rebuild_index(clips_dir, index_path)

    assert count == 3
    lines = index_path.read_text().strip().splitlines()
    assert [json.loads(line)["event_id"] for line in lines] == ["cam_a", "cam_b", "cam_c"]


def test_rebuild_index_skips_clip_with_no_metadata(tmp_path, capsys):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    (clips_dir / "orphan.mp4").write_bytes(b"fake")
    _write_clip(clips_dir, "cam_a")

    count = rebuild_index(clips_dir, clips_dir / "index.jsonl")

    assert count == 1
    assert "orphan.mp4 has no metadata sidecar" in capsys.readouterr().err


def test_rebuild_index_skips_metadata_with_no_clip(tmp_path, capsys):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    (clips_dir / "ghost.json").write_text(json.dumps({"event_id": "ghost"}))
    _write_clip(clips_dir, "cam_a")

    count = rebuild_index(clips_dir, clips_dir / "index.jsonl")

    assert count == 1
    assert "ghost.json has no matching clip" in capsys.readouterr().err


def test_rebuild_index_skips_unreadable_metadata_without_crashing(tmp_path, capsys):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    (clips_dir / "broken.mp4").write_bytes(b"fake")
    (clips_dir / "broken.json").write_text("{not valid json")
    _write_clip(clips_dir, "cam_a")

    count = rebuild_index(clips_dir, clips_dir / "index.jsonl")

    assert count == 1
    assert "failed to read broken.json" in capsys.readouterr().err


def test_rebuild_index_temp_clips_are_not_flagged_as_orphaned(tmp_path, capsys):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    (clips_dir / "cam_1.mp4.rec.mp4").write_bytes(b"still recording")  # no .json -- expected, not an orphan

    rebuild_index(clips_dir, clips_dir / "index.jsonl")

    assert "cam_1.mp4.rec.mp4" not in capsys.readouterr().err


def test_cli_fails_loud_on_a_non_directory(tmp_path, monkeypatch):
    not_a_dir = tmp_path / "nope"
    monkeypatch.setattr(sys, "argv", ["rebuild_index", str(not_a_dir)])
    with pytest.raises(SystemExit):
        main()


def test_cli_defaults_index_path_next_to_clips(tmp_path, monkeypatch, capsys):
    clips_dir = tmp_path / "clips"
    _write_clip(clips_dir, "cam_a")
    monkeypatch.setattr(sys, "argv", ["rebuild_index", str(clips_dir)])

    main()

    assert (clips_dir / "index.jsonl").exists()
    assert "wrote 1 entries" in capsys.readouterr().out
