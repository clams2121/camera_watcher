import json

import numpy as np
import yaml

from camera_watcher.config import Config
from camera_watcher.pipeline import CameraPipeline
from camera_watcher.web import create_app

AUTH_TOKEN = "x" * 40


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


def _make_app(tmp_path):
    config = Config(_write_config(tmp_path))
    pipeline = CameraPipeline(config)
    app = create_app(config, pipeline, AUTH_TOKEN)
    app.testing = True
    return app, pipeline


def make_client(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()
    # Flask's test client persists cookies across requests, so logging in
    # once here authenticates every subsequent call these tests make.
    resp = client.post("/api/login", json={"token": AUTH_TOKEN})
    assert resp.status_code == 200
    return client


def make_client_with_pipeline(tmp_path):
    app, pipeline = _make_app(tmp_path)
    client = app.test_client()
    resp = client.post("/api/login", json={"token": AUTH_TOKEN})
    assert resp.status_code == 200
    return client, pipeline


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


# ---------- Verdict / review sidecars ----------


def test_recordings_list_reports_null_verdict_when_unclassified(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "cam_20260101_000000.mp4").write_bytes(b"x")

    resp = client.get("/api/recordings")
    rec = resp.get_json()["groups"][0]["recordings"][0]
    assert rec["verdict"] is None
    assert rec["reason"] is None
    assert rec["labels"] == []
    assert rec["reviewed"] is None


def test_recordings_list_surfaces_verdict_reason_and_top_labels(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "cam_20260101_000000.mp4").write_bytes(b"x")
    analysis = {
        "verdict": "review",
        "reason": "persistent_detection",
        "labels": [
            {"label": "bird", "confidence": 0.3, "frame_offset": 1.0, "box": [0, 0, 0.1, 0.1]},
            {"label": "cat", "confidence": 0.9, "frame_offset": 2.0, "box": [0, 0, 0.1, 0.1]},
            {"label": "dog", "confidence": 0.6, "frame_offset": 3.0, "box": [0, 0, 0.1, 0.1]},
            {"label": "fox", "confidence": 0.5, "frame_offset": 4.0, "box": [0, 0, 0.1, 0.1]},
        ],
    }
    (clips_dir / "cam_20260101_000000.analysis.json").write_text(json.dumps(analysis))

    resp = client.get("/api/recordings")
    rec = resp.get_json()["groups"][0]["recordings"][0]
    assert rec["verdict"] == "review"
    assert rec["reason"] == "persistent_detection"
    # top 3 by confidence, not sidecar order
    assert [label["label"] for label in rec["labels"]] == ["cat", "dog", "fox"]
    assert rec["reviewed"] is None


def test_recordings_list_surfaces_a_prior_review_decision(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "cam_20260101_000000.mp4").write_bytes(b"x")
    review = {"reviewed_at": "2026-01-01T00:05:00+00:00", "decision": "keep"}
    (clips_dir / "cam_20260101_000000.review.json").write_text(json.dumps(review))

    resp = client.get("/api/recordings")
    rec = resp.get_json()["groups"][0]["recordings"][0]
    assert rec["reviewed"] == review


def test_recordings_list_treats_unreadable_sidecars_as_absent(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "cam_20260101_000000.mp4").write_bytes(b"x")
    (clips_dir / "cam_20260101_000000.analysis.json").write_text("{not valid json")

    resp = client.get("/api/recordings")
    rec = resp.get_json()["groups"][0]["recordings"][0]
    assert rec["verdict"] is None


def test_review_endpoint_keep_writes_sidecar_and_does_not_delete_the_clip(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip = clips_dir / "cam_20260101_000000.mp4"
    clip.write_bytes(b"x")

    resp = client.post("/api/recordings/cam_20260101_000000.mp4/review", json={"decision": "keep", "note": "fine"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"ok": True, "decision": "keep", "deleted": False}
    assert clip.exists()

    review_path = clips_dir / "cam_20260101_000000.review.json"
    assert review_path.exists()
    saved = json.loads(review_path.read_text())
    assert saved["decision"] == "keep"
    assert saved["note"] == "fine"
    assert "reviewed_at" in saved
    assert not list(clips_dir.glob("*.tmp.json"))  # atomic write leaves nothing behind


def test_review_endpoint_discard_writes_sidecar_then_deletes_the_whole_family(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip = clips_dir / "cam_20260101_000000.mp4"
    clip.write_bytes(b"x")
    metadata = clips_dir / "cam_20260101_000000.json"
    metadata.write_text("{}")
    analysis = clips_dir / "cam_20260101_000000.analysis.json"
    analysis.write_text(json.dumps({"verdict": "review", "reason": "persistent_detection", "labels": []}))

    resp = client.post("/api/recordings/cam_20260101_000000.mp4/review", json={"decision": "discard"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "decision": "discard", "deleted": True}

    assert not clip.exists()
    assert not metadata.exists()
    assert not analysis.exists()
    # review.json itself is part of the sidecar family removed on discard
    assert not (clips_dir / "cam_20260101_000000.review.json").exists()


def test_review_endpoint_rejects_invalid_or_missing_decision(tmp_path):
    client = make_client(tmp_path)
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clip = clips_dir / "cam_20260101_000000.mp4"
    clip.write_bytes(b"x")

    resp = client.post("/api/recordings/cam_20260101_000000.mp4/review", json={"decision": "maybe"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False

    resp = client.post("/api/recordings/cam_20260101_000000.mp4/review", json={})
    assert resp.status_code == 400
    assert clip.exists()  # never touched


def test_review_endpoint_rejects_unknown_recording(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/api/recordings/does-not-exist.mp4/review", json={"decision": "keep"})
    assert resp.status_code == 404


# ---------- Auth ----------


def test_unauthenticated_api_request_gets_401_json(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()
    resp = client.get("/api/status")
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_unauthenticated_page_request_redirects_to_login(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_login_page_itself_is_reachable_unauthenticated(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()
    resp = client.get("/login")
    assert resp.status_code == 200


def test_login_with_wrong_token_is_rejected(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()
    resp = client.post("/api/login", json={"token": "wrong"})
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False

    # still unauthenticated -- a failed login attempt must not grant a session
    assert client.get("/api/status").status_code == 401


def test_login_with_correct_token_grants_a_session(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()
    resp = client.post("/api/login", json={"token": AUTH_TOKEN})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    assert client.get("/api/status").status_code == 200
    assert client.get("/").status_code == 200


def test_logout_clears_the_session(tmp_path):
    client = make_client(tmp_path)  # logged in
    assert client.get("/api/status").status_code == 200

    resp = client.post("/api/logout")
    assert resp.status_code == 200

    assert client.get("/api/status").status_code == 401


def test_bearer_token_authenticates_without_a_session(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()  # never logged in -- no session cookie at all
    resp = client.get("/api/status", headers={"Authorization": f"Bearer {AUTH_TOKEN}"})
    assert resp.status_code == 200


def test_wrong_bearer_token_is_rejected(tmp_path):
    app, _ = _make_app(tmp_path)
    client = app.test_client()
    resp = client.get("/api/status", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
