import json

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
