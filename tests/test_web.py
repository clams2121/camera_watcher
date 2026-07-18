import json

import numpy as np
import yaml

from camera_watcher.config import Config
from camera_watcher.pipeline import CameraPipeline
from camera_watcher.web import create_app


def _write_config(tmp_path):
    path = tmp_path / "camera1.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "camera": {"name": "camera1", "host": "192.168.1.50"},
                "mask": {"path": str(tmp_path / "mask.json")},
                "recording": {"output_dir": str(tmp_path / "clips")},
            }
        )
    )
    return path


def make_client(tmp_path):
    config = Config(_write_config(tmp_path))
    pipeline = CameraPipeline(config)
    app = create_app(config, pipeline)
    app.testing = True
    return app.test_client()


def make_client_with_pipeline(tmp_path):
    config = Config(_write_config(tmp_path))
    pipeline = CameraPipeline(config)
    app = create_app(config, pipeline)
    app.testing = True
    return app.test_client(), pipeline


def test_settings_roundtrip(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.get_json()["has_credentials"] is False

    resp = client.post(
        "/api/settings",
        json={
            "settings": {"camera": {"host": "10.0.0.9"}},
            "credentials": {"username": "admin", "password": "hunter2"},
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["settings"]["camera"]["host"] == "10.0.0.9"
    assert body["has_credentials"] is True
    assert "hunter2" not in json.dumps(body)  # the password itself must never round-trip back


def test_mask_roundtrip(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/mask", json={"polygons": [[[0, 0], [1, 0], [1, 1]]]})
    assert resp.status_code == 200
    assert len(resp.get_json()["polygons"]) == 1

    resp = client.get("/api/mask")
    assert len(resp.get_json()["polygons"]) == 1


def test_snapshot_without_frames_returns_503(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/api/snapshot")
    assert resp.status_code == 503


def test_status_endpoint(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "connected" in body and "recording" in body


def test_recordings_list_excludes_temp_files_and_streams_with_range_support(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "cam_20260101_000000.mp4").write_bytes(b"0123456789" * 10)
    (clips_dir / "cam_20260101_000100.mp4.rec.mp4").write_bytes(b"still recording")

    resp = client.get("/api/recordings")
    assert resp.status_code == 200
    groups = resp.get_json()["groups"]
    assert len(groups) == 1
    recordings = groups[0]["recordings"]
    assert len(recordings) == 1
    assert recordings[0]["name"] == "cam_20260101_000000.mp4"
    assert recordings[0]["size_bytes"] == 100

    resp = client.get("/api/recordings/cam_20260101_000000.mp4")
    assert resp.status_code == 200
    assert resp.data == b"0123456789" * 10

    # <video> relies on Range requests to seek -- must come back as 206 Partial Content.
    resp = client.get("/api/recordings/cam_20260101_000000.mp4", headers={"Range": "bytes=0-9"})
    assert resp.status_code == 206
    assert resp.data == b"0123456789"


def test_recording_rejects_temp_missing_and_non_mp4_names(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "cam_x.mp4.rec.mp4").write_bytes(b"still recording")

    assert client.get("/api/recordings/cam_x.mp4.rec.mp4").status_code == 404
    assert client.get("/api/recordings/does-not-exist.mp4").status_code == 404
    assert client.get("/api/recordings/not-an-mp4.txt").status_code == 404


def test_heatmap_endpoint_before_any_motion_returns_503(tmp_path):
    client = make_client(tmp_path)
    resp = client.get("/api/heatmap.png")
    assert resp.status_code == 503


def test_heatmap_reset_is_safe_even_with_no_accumulator_yet(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/heatmap/reset")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_heatmap_endpoint_serves_png_once_accumulator_exists(tmp_path):
    client, pipeline = make_client_with_pipeline(tmp_path)
    from camera_watcher.accumulator import MotionAccumulator

    pipeline.accumulator = MotionAccumulator(tmp_path / "heatmap.npy", width=8, height=8)
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[2, 2] = 255
    pipeline.accumulator.add(mask)

    resp = client.get("/api/heatmap.png")
    assert resp.status_code == 200
    assert resp.content_type == "image/png"
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    resp = client.post("/api/heatmap/reset")
    assert resp.status_code == 200
    assert int(pipeline.accumulator.counts.max()) == 0


def test_shutdown_rejects_wrong_or_missing_confirmation(tmp_path, monkeypatch):
    from camera_watcher.web import routes

    calls = []
    monkeypatch.setattr(routes, "_schedule_shutdown", lambda *a, **k: calls.append(True))
    client = make_client(tmp_path)

    resp = client.post("/api/shutdown", json={"confirm": "nope"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    resp = client.post("/api/shutdown", json={})
    assert resp.status_code == 400

    assert calls == []  # never actually scheduled a shutdown


def test_shutdown_accepts_case_insensitive_confirmation(tmp_path, monkeypatch):
    from camera_watcher.web import routes

    calls = []
    monkeypatch.setattr(routes, "_schedule_shutdown", lambda *a, **k: calls.append(True))
    client = make_client(tmp_path)

    resp = client.post("/api/shutdown", json={"confirm": "QUIT"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert calls == [True]


def test_delete_recording_removes_the_file_and_its_metadata(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip = clips_dir / "cam_20260101_000000.mp4"
    clip.write_bytes(b"data")
    metadata = clips_dir / "cam_20260101_000000.json"
    metadata.write_text("{}")

    resp = client.delete("/api/recordings/cam_20260101_000000.mp4")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert not clip.exists()
    assert not metadata.exists()


def test_delete_recording_rejects_temp_missing_and_non_mp4_names(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    temp_clip = clips_dir / "cam_x.mp4.rec.mp4"
    temp_clip.write_bytes(b"still recording")

    assert client.delete("/api/recordings/cam_x.mp4.rec.mp4").status_code == 404
    assert temp_clip.exists()  # never touched
    assert client.delete("/api/recordings/does-not-exist.mp4").status_code == 404
    assert client.delete("/api/recordings/not-an-mp4.txt").status_code == 404


def test_recordings_are_grouped_into_half_hour_buckets_aligned_to_00_and_30(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "cam_20260117_135959.mp4",  # -> 13:30-14:00 bucket
        "cam_20260117_140000.mp4",  # -> 14:00-14:30 bucket
        "cam_20260117_142959.mp4",  # -> 14:00-14:30 bucket
        "cam_20260117_143000.mp4",  # -> 14:30-15:00 bucket
    ):
        (clips_dir / name).write_bytes(b"x")

    resp = client.get("/api/recordings")
    assert resp.status_code == 200
    groups = resp.get_json()["groups"]

    assert [g["bucket"] for g in groups] == ["20260117_1430", "20260117_1400", "20260117_1330"]  # newest first

    by_bucket = {g["bucket"]: g for g in groups}
    assert sorted(r["name"] for r in by_bucket["20260117_1400"]["recordings"]) == [
        "cam_20260117_140000.mp4",
        "cam_20260117_142959.mp4",
    ]
    assert [r["name"] for r in by_bucket["20260117_1330"]["recordings"]] == ["cam_20260117_135959.mp4"]
    assert [r["name"] for r in by_bucket["20260117_1430"]["recordings"]] == ["cam_20260117_143000.mp4"]


def test_delete_recording_group_removes_only_that_buckets_clips(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    keep = clips_dir / "cam_20260117_143000.mp4"
    keep.write_bytes(b"x")
    gone1 = clips_dir / "cam_20260117_140000.mp4"
    gone1.write_bytes(b"x")
    gone1_meta = clips_dir / "cam_20260117_140000.json"
    gone1_meta.write_text("{}")
    gone2 = clips_dir / "cam_20260117_142000.mp4"
    gone2.write_bytes(b"x")

    resp = client.delete("/api/recordings/group/20260117_1400")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert sorted(body["deleted"]) == ["cam_20260117_140000.mp4", "cam_20260117_142000.mp4"]

    assert not gone1.exists()
    assert not gone1_meta.exists()  # companion metadata removed too
    assert not gone2.exists()
    assert keep.exists()  # different bucket, untouched


def test_delete_recording_group_rejects_malformed_bucket(tmp_path):
    client = make_client(tmp_path)
    assert client.delete("/api/recordings/group/not-a-bucket").status_code == 404
    assert client.delete("/api/recordings/group/2026011_1400").status_code == 404  # wrong digit count


def test_update_rejects_wrong_confirmation_without_touching_git(tmp_path, monkeypatch):
    from camera_watcher.web import routes

    calls = []
    monkeypatch.setattr(routes, "pull_latest", lambda *a, **k: calls.append("pull"))
    client = make_client(tmp_path)

    resp = client.post("/api/update", json={"confirm": "nope"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert calls == []


def test_update_reports_up_to_date_without_restarting(tmp_path, monkeypatch):
    from camera_watcher.update import CommandResult
    from camera_watcher.web import routes

    monkeypatch.setattr(routes, "pull_latest", lambda root: (CommandResult(True, "Already up to date."), False))
    restart_calls = []
    monkeypatch.setattr(routes, "_schedule_restart", lambda *a, **k: restart_calls.append(True))
    client = make_client(tmp_path)

    resp = client.post("/api/update", json={"confirm": "update"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["updated"] is False
    assert restart_calls == []


def test_update_reports_pull_failure_without_restarting(tmp_path, monkeypatch):
    from camera_watcher.update import CommandResult
    from camera_watcher.web import routes

    monkeypatch.setattr(
        routes, "pull_latest", lambda root: (CommandResult(False, "fatal: not a fast-forward"), False)
    )
    restart_calls = []
    monkeypatch.setattr(routes, "_schedule_restart", lambda *a, **k: restart_calls.append(True))
    client = make_client(tmp_path)

    resp = client.post("/api/update", json={"confirm": "update"})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert "fast-forward" in body["error"]
    assert restart_calls == []


def test_update_reports_dependency_install_failure_without_restarting(tmp_path, monkeypatch):
    from camera_watcher.update import CommandResult
    from camera_watcher.web import routes

    monkeypatch.setattr(routes, "pull_latest", lambda root: (CommandResult(True, "Fast-forwarded."), True))
    monkeypatch.setattr(routes, "install_dependencies", lambda root: CommandResult(False, "pip explosion"))
    restart_calls = []
    monkeypatch.setattr(routes, "_schedule_restart", lambda *a, **k: restart_calls.append(True))
    client = make_client(tmp_path)

    resp = client.post("/api/update", json={"confirm": "UPDATE"})  # case-insensitive
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert body["updated"] is True
    assert "pip explosion" in body["error"]
    assert restart_calls == []


def test_update_succeeds_and_schedules_restart(tmp_path, monkeypatch):
    from camera_watcher.update import CommandResult
    from camera_watcher.web import routes

    monkeypatch.setattr(routes, "pull_latest", lambda root: (CommandResult(True, "Fast-forwarded."), True))
    monkeypatch.setattr(routes, "install_dependencies", lambda root: CommandResult(True, "Dependencies installed."))
    restart_calls = []
    monkeypatch.setattr(routes, "_schedule_restart", lambda *a, **k: restart_calls.append(True))
    client = make_client(tmp_path)

    resp = client.post("/api/update", json={"confirm": "update"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["updated"] is True
    assert restart_calls == [True]
