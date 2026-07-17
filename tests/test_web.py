import json

import numpy as np

from camera_watcher.config import Config
from camera_watcher.pipeline import CameraPipeline
from camera_watcher.web import create_app


def make_client(tmp_path):
    config = Config(tmp_path / "settings.yaml", tmp_path / "secrets.yaml")
    config.update_settings(
        {
            "mask": {"path": str(tmp_path / "mask.json")},
            "recording": {"output_dir": str(tmp_path / "clips")},
        }
    )
    pipeline = CameraPipeline(config)
    app = create_app(config, pipeline)
    app.testing = True
    return app.test_client()


def make_client_with_pipeline(tmp_path):
    config = Config(tmp_path / "settings.yaml", tmp_path / "secrets.yaml")
    config.update_settings(
        {
            "mask": {"path": str(tmp_path / "mask.json")},
            "recording": {"output_dir": str(tmp_path / "clips")},
        }
    )
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
            "credentials": {"username": "admin", "password": "secret"},
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["settings"]["camera"]["host"] == "10.0.0.9"
    assert body["has_credentials"] is True
    assert "secret" not in json.dumps(body)


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
    recordings = resp.get_json()["recordings"]
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
