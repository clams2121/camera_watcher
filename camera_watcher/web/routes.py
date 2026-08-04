"""HTTP routes for the camera_watcher fleet web UI.

Two halves: fleet-level routes (camera list/add/remove, fleet settings,
retention, the classifier's config file, login/shutdown) and camera-scoped
routes, all under ``/api/cameras/<camera_id>/...`` (settings, mask, live
preview, recordings). All state changes are written straight through to
disk via :class:`~camera_watcher.fleet.FleetConfig` /
:class:`~camera_watcher.config.Config` and applied to the running pipeline
immediately, with no separate "apply" step -- except web bind host/port and
the auth token, which need a process restart, called out explicitly in
their responses.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import cv2
import yaml
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from ..auth import tokens_match
from ..config import ConfigError
from ..constants import TEMP_SUFFIX
from ..fleet import CameraManager, FleetConfig
from ..retention import RetentionScheduler
from ..yaml_store import deep_merge

bp = Blueprint("camera_watcher", __name__)
logger = logging.getLogger(__name__)

_SHUTDOWN_CONFIRM_TEXT = "quit"

# Reachable without a valid session/bearer token -- everything else on this
# blueprint requires one, enforced in _require_auth below.
_PUBLIC_ENDPOINTS = {"camera_watcher.login_page", "camera_watcher.login"}

# Recording filenames are always "<camera_name>_<YYYYMMDD>_<HHMMSS>.mp4"
# (see recorder.py) -- reject anything else outright before it ever touches
# the filesystem, so a crafted filename can't be used to escape output_dir.
_CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.mp4$")
_CLIP_TS_RE = re.compile(r"_(\d{8})_(\d{6})\.mp4$")
_BUCKET_RE = re.compile(r"^\d{8}_\d{4}$")

CLASSIFIER_CONFIG_FILENAME = "classifier.yaml"
_CLASSIFIER_DEFAULTS = {
    "data_root": "",  # "" -> filled in from the fleet's shared data_root below
    "backend": "auto",
    "cpu": {"model_path": "models/yolov8n.onnx"},
    "hailo": {"hef_path": "models/yolov8n.hef"},
    "thresholds": {
        "high_confidence": 0.5,
        "review_large_object_area_frac": 0.05,
        "review_persistent_detection_frac": 0.6,
        "review_persistent_motion_detection_size": 0.05,
        "review_persistent_motion_frame_ratio": 0.6,
    },
    "sampling": {"max_frames": 5, "min_frame_spacing_seconds": 1.0},
    "watch": {"queue_maxsize": 256, "rescan_interval_seconds": 600},
}


def _fleet_config() -> FleetConfig:
    return current_app.config["FLEET_CONFIG"]


def _camera_manager() -> CameraManager:
    return current_app.config["CAMERA_MANAGER"]


def _retention_scheduler() -> RetentionScheduler:
    return current_app.config["RETENTION_SCHEDULER"]


def _json_error(status: int, message: str) -> Response:
    response = jsonify({"ok": False, "error": message})
    response.status_code = status
    return response


def _require_camera(camera_id: str):
    """Returns (config, pipeline) for a running camera. Aborts with a plain
    404 if no such camera exists at all, or a JSON 503 carrying the stored
    error message if the camera exists but failed to load/start -- distinct
    signals so the UI can tell "no such camera" from "this one needs
    attention". Used by routes that need the live pipeline (snapshot,
    stream, mask, heatmap, status) -- see _require_camera_config for routes
    that only need the persisted settings and should keep working even for
    an errored camera."""
    manager = _camera_manager()
    config = manager.get_config(camera_id)
    pipeline = manager.get_pipeline(camera_id)
    if config is not None and pipeline is not None:
        return config, pipeline
    error = manager.get_error(camera_id)
    if error is not None:
        abort(_json_error(503, f"Camera {camera_id!r} failed to start: {error}"))
    abort(404)


def _require_camera_config(camera_id: str):
    """Returns this camera's Config regardless of whether its pipeline is
    currently running -- so a camera that failed to start (bad host,
    missing credentials, whatever) can still have its settings viewed and
    fixed through the UI, and its existing recordings still browsed."""
    try:
        config = _camera_manager().get_config_for_editing(camera_id)
    except ConfigError as e:
        abort(_json_error(503, f"Camera {camera_id!r}'s config can't be loaded: {e}"))
    if config is None:
        abort(404)
    return config


@bp.before_request
def _require_auth():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None

    if session.get("authenticated"):
        return None

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        candidate = auth_header[len("Bearer ") :]
        if tokens_match(candidate, current_app.config["AUTH_TOKEN"]):
            return None

    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return redirect(url_for("camera_watcher.login_page"))


@bp.get("/login")
def login_page():
    return render_template("login.html")


@bp.post("/api/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    candidate = str(body.get("token", ""))
    if not tokens_match(candidate, current_app.config["AUTH_TOKEN"]):
        return jsonify({"ok": False, "error": "invalid token"}), 401
    session["authenticated"] = True
    return jsonify({"ok": True})


@bp.post("/api/logout")
def logout():
    session.pop("authenticated", None)
    return jsonify({"ok": True})


@bp.get("/")
def index():
    return render_template("dashboard.html")


@bp.get("/cameras/<camera_id>")
def camera_page(camera_id: str):
    if not _camera_manager().exists(camera_id):
        abort(404)
    return render_template("camera.html", camera_id=camera_id)


# ---------- Fleet: camera list / add / remove ----------


@bp.get("/api/cameras")
def list_cameras():
    return jsonify({"cameras": _camera_manager().list_cameras()})


@bp.post("/api/cameras")
def create_camera():
    body = request.get_json(force=True, silent=True) or {}
    settings_patch = body.get("settings") or {}
    credentials = body.get("credentials") or {}
    secrets_patch = None
    cred_patch = {k: v for k, v in credentials.items() if k in ("username", "password") and v}
    if cred_patch:
        secrets_patch = {"camera": cred_patch}

    try:
        camera = _camera_manager().add_camera(settings_patch, secrets_patch)
    except ConfigError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "camera": camera}), 201


@bp.delete("/api/cameras/<camera_id>")
def delete_camera(camera_id: str):
    manager = _camera_manager()
    if not manager.exists(camera_id):
        abort(404)
    delete_data = request.args.get("delete_data", "").strip().lower() in ("1", "true", "yes")
    manager.remove_camera(camera_id, delete_data=delete_data)
    return jsonify({"ok": True})


# ---------- Camera-scoped: settings ----------


@bp.get("/api/cameras/<camera_id>/settings")
def get_camera_settings(camera_id: str):
    config = _require_camera_config(camera_id)
    return jsonify(
        {
            "settings": config.settings,
            "has_credentials": config.has_credentials(),
            "redacted_rtsp_url": config.redacted_rtsp_url(),
            "error": _camera_manager().get_error(camera_id),
        }
    )


@bp.post("/api/cameras/<camera_id>/settings")
def post_camera_settings(camera_id: str):
    manager = _camera_manager()
    if not manager.exists(camera_id):
        abort(404)

    body = request.get_json(force=True, silent=True) or {}
    settings_patch = body.get("settings")
    credentials = body.get("credentials") or {}
    secrets_patch = None
    cred_patch = {k: v for k, v in credentials.items() if k in ("username", "password") and v}
    if cred_patch:
        secrets_patch = {"camera": cred_patch}

    try:
        manager.update_camera(camera_id, settings_patch=settings_patch, secrets_patch=secrets_patch)
    except ConfigError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logger.exception("Camera %r failed to apply updated settings", camera_id)
        return jsonify({"ok": False, "error": f"Settings were saved, but applying them failed: {e}"}), 500

    config = manager.get_config_for_editing(camera_id)
    return jsonify(
        {
            "ok": True,
            "settings": config.settings,
            "has_credentials": config.has_credentials(),
            "redacted_rtsp_url": config.redacted_rtsp_url(),
            "error": manager.get_error(camera_id),
        }
    )


# ---------- Camera-scoped: live status / preview / mask ----------


@bp.get("/api/cameras/<camera_id>/status")
def get_camera_status(camera_id: str):
    _, pipeline = _require_camera(camera_id)
    return jsonify(pipeline.status())


@bp.get("/api/cameras/<camera_id>/snapshot")
def get_camera_snapshot(camera_id: str):
    _, pipeline = _require_camera(camera_id)
    latest = pipeline.frame_buffer.latest()
    if latest is None:
        return jsonify({"error": "no frames available yet"}), 503
    ok, buf = cv2.imencode(".jpg", latest.frame)
    if not ok:
        return jsonify({"error": "failed to encode snapshot"}), 500
    return Response(buf.tobytes(), mimetype="image/jpeg")


@bp.get("/api/cameras/<camera_id>/mask")
def get_camera_mask(camera_id: str):
    _, pipeline = _require_camera(camera_id)
    return jsonify({"polygons": pipeline.mask_store.polygons})


@bp.post("/api/cameras/<camera_id>/mask")
def post_camera_mask(camera_id: str):
    _, pipeline = _require_camera(camera_id)
    body = request.get_json(force=True, silent=True) or {}
    polygons = body.get("polygons", [])
    pipeline.mask_store.save(polygons)
    pipeline.reload_mask()
    return jsonify({"ok": True, "polygons": pipeline.mask_store.polygons})


@bp.get("/api/cameras/<camera_id>/stream")
def get_camera_stream(camera_id: str):
    config, pipeline = _require_camera(camera_id)
    fps = max(1, min(15, config.settings["web"].get("preview_fps", 5)))
    interval = 1.0 / fps

    def generate():
        last_ts = None
        while True:
            latest = pipeline.frame_buffer.latest()
            if latest is not None and latest.timestamp != last_ts:
                last_ts = latest.timestamp
                frame = pipeline.frame_for_preview(latest.frame)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                    )
            time.sleep(interval)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@bp.get("/api/cameras/<camera_id>/heatmap.png")
def get_camera_heatmap(camera_id: str):
    _, pipeline = _require_camera(camera_id)
    png = pipeline.heatmap_png()
    if png is None:
        return jsonify({"error": "no motion analyzed yet"}), 503
    return Response(png, mimetype="image/png")


@bp.post("/api/cameras/<camera_id>/heatmap/reset")
def reset_camera_heatmap(camera_id: str):
    _, pipeline = _require_camera(camera_id)
    pipeline.reset_heatmap()
    return jsonify({"ok": True})


# ---------- Camera-scoped: recordings ----------


def _clip_start_datetime(path: Path, stat) -> datetime:
    """Best-effort start time for grouping: parsed from the recorder's
    embedded filename timestamp (second precision, already includes
    pre-buffer compensation -- see recorder.py's _timestamp_name), falling
    back to the file's mtime for anything that doesn't match."""
    m = _CLIP_TS_RE.search(path.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(stat.st_mtime)


def _bucket_key(dt: datetime) -> str:
    """30-minute bucket aligned to :00/:30, as "YYYYMMDD_HHMM"."""
    bucket_minute = 0 if dt.minute < 30 else 30
    return dt.replace(minute=bucket_minute, second=0, microsecond=0).strftime("%Y%m%d_%H%M")


def _bucket_bounds(bucket: str):
    start = datetime.strptime(bucket, "%Y%m%d_%H%M")
    end = start + timedelta(minutes=30)
    return start.isoformat(), end.isoformat()


def _analysis_path(clip_path: Path) -> Path:
    """<stem>.analysis.json -- the clip_classifier verdict sidecar, if this
    clip has been classified yet. Never written by this process, only
    ever read -- see clip_classifier/analysis.py for the sole writer."""
    return clip_path.parent / f"{clip_path.stem}.analysis.json"


def _review_path(clip_path: Path) -> Path:
    """<stem>.review.json -- a human reviewer's keep/discard decision, if
    any. This module is the sole writer of this one (see review_recording
    below)."""
    return clip_path.parent / f"{clip_path.stem}.review.json"


def _read_json_best_effort(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("Failed to read %s -- treating it as absent", path)
        return None


def _unlink_clip_and_metadata(file_path: Path) -> None:
    """Removes a clip and its whole sidecar family -- the recorder's own
    <stem>.json, and, if present, clip_classifier's <stem>.analysis.json
    and this module's own <stem>.review.json. Each is best-effort: a
    missing or unremovable sidecar never stops the others (or the clip
    itself) from being deleted."""
    file_path.unlink()
    for sidecar_path in (
        file_path.with_suffix(".json"),
        _analysis_path(file_path),
        _review_path(file_path),
    ):
        try:
            sidecar_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove sidecar file %s", sidecar_path)


def _recording_entry(p: Path, stat) -> dict:
    analysis = _read_json_best_effort(_analysis_path(p))
    review = _read_json_best_effort(_review_path(p))
    top_labels = []
    if analysis:
        top_labels = sorted(analysis.get("labels") or [], key=lambda label: label.get("confidence", 0), reverse=True)[
            :3
        ]
    return {
        "name": p.name,
        "size_bytes": stat.st_size,
        "modified": stat.st_mtime,
        # None (not e.g. "unclassified") when there's no analysis sidecar
        # yet at all -- clip_classifier hasn't gotten to this clip yet,
        # distinct from a real verdict of "low"/"high"/"review"/"error".
        "verdict": analysis.get("verdict") if analysis else None,
        "reason": analysis.get("reason") if analysis else None,
        "labels": top_labels,
        "reviewed": review,
    }


@bp.get("/api/cameras/<camera_id>/recordings")
def list_recordings(camera_id: str):
    """Recordings grouped into 30-minute buckets aligned to :00/:30, newest
    group first, newest clip first within each group -- lets the UI offer a
    "delete this whole half-hour" action alongside per-clip delete."""
    config = _require_camera_config(camera_id)
    output_dir = Path(config.resolved()["recording"]["output_dir"])
    buckets: dict = {}
    if output_dir.exists():
        for p in output_dir.iterdir():
            if p.is_file() and p.suffix == ".mp4" and not p.name.endswith(TEMP_SUFFIX):
                try:
                    stat = p.stat()
                except OSError:
                    continue
                key = _bucket_key(_clip_start_datetime(p, stat))
                buckets.setdefault(key, []).append(_recording_entry(p, stat))

    groups = []
    for key in sorted(buckets.keys(), reverse=True):
        recordings = sorted(buckets[key], key=lambda r: r["modified"], reverse=True)
        start_iso, end_iso = _bucket_bounds(key)
        groups.append({"bucket": key, "start": start_iso, "end": end_iso, "recordings": recordings})
    return jsonify({"groups": groups})


def _resolve_clip_path(output_dir: Path, filename: str) -> Optional[Path]:
    """Validates `filename` against the recorder's naming pattern and resolves
    it against the clips directory, refusing anything that would escape it.
    Returns None if the name is invalid or doesn't point at a real, finalized clip."""
    if not _CLIP_NAME_RE.match(filename) or filename.endswith(TEMP_SUFFIX):
        return None
    file_path = (output_dir / filename).resolve()
    if output_dir not in file_path.parents or not file_path.is_file():
        return None
    return file_path


@bp.get("/api/cameras/<camera_id>/recordings/<filename>")
def get_recording(camera_id: str, filename: str):
    config = _require_camera_config(camera_id)
    output_dir = Path(config.resolved()["recording"]["output_dir"])
    file_path = _resolve_clip_path(output_dir, filename)
    if file_path is None:
        abort(404)

    # conditional=True (Flask's default) makes this honor Range requests,
    # which <video> needs to seek without downloading the whole file.
    return send_file(file_path, mimetype="video/mp4", conditional=True)


@bp.delete("/api/cameras/<camera_id>/recordings/<filename>")
def delete_recording(camera_id: str, filename: str):
    config = _require_camera_config(camera_id)
    output_dir = Path(config.resolved()["recording"]["output_dir"])
    file_path = _resolve_clip_path(output_dir, filename)
    if file_path is None:
        abort(404)
    try:
        _unlink_clip_and_metadata(file_path)
    except OSError:
        logger.exception("Failed to delete recording %s", filename)
        return jsonify({"ok": False, "error": "failed to delete the file"}), 500
    return jsonify({"ok": True})


@bp.post("/api/cameras/<camera_id>/recordings/<filename>/review")
def review_recording(camera_id: str, filename: str):
    """Records a human reviewer's keep/discard decision for a clip
    clip_classifier flagged as "review" -- writes <stem>.review.json
    either way; "discard" additionally deletes the clip (and its whole
    sidecar family, review.json included) via the same path the plain
    Delete button uses. This route is the sole writer of review.json."""
    config = _require_camera_config(camera_id)
    output_dir = Path(config.resolved()["recording"]["output_dir"])
    file_path = _resolve_clip_path(output_dir, filename)
    if file_path is None:
        abort(404)

    body = request.get_json(force=True, silent=True) or {}
    decision = str(body.get("decision", "")).strip().lower()
    if decision not in ("keep", "discard"):
        return jsonify({"ok": False, "error": 'decision must be "keep" or "discard"'}), 400

    review_payload = {"reviewed_at": datetime.now().astimezone().isoformat(), "decision": decision}
    note = body.get("note")
    if note:
        review_payload["note"] = str(note)

    review_path = _review_path(file_path)
    try:
        tmp_path = review_path.with_name(review_path.name[: -len(".json")] + ".tmp.json")
        tmp_path.write_text(json.dumps(review_payload, indent=2))
        tmp_path.replace(review_path)
    except OSError:
        logger.exception("Failed to write review sidecar for %s", filename)
        return jsonify({"ok": False, "error": "failed to record the review decision"}), 500

    if decision == "discard":
        try:
            _unlink_clip_and_metadata(file_path)
        except OSError:
            logger.exception("Failed to delete discarded recording %s", filename)
            return (
                jsonify(
                    {"ok": False, "error": "recorded the review decision, but failed to delete the clip"}
                ),
                500,
            )
        return jsonify({"ok": True, "decision": decision, "deleted": True})

    return jsonify({"ok": True, "decision": decision, "deleted": False})


@bp.delete("/api/cameras/<camera_id>/recordings/group/<bucket>")
def delete_recording_group(camera_id: str, bucket: str):
    if not _BUCKET_RE.match(bucket):
        abort(404)

    config = _require_camera_config(camera_id)
    output_dir = Path(config.resolved()["recording"]["output_dir"])
    deleted = []
    errors = []
    if output_dir.exists():
        for p in output_dir.iterdir():
            if not (p.is_file() and p.suffix == ".mp4" and not p.name.endswith(TEMP_SUFFIX)):
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            if _bucket_key(_clip_start_datetime(p, stat)) != bucket:
                continue
            try:
                _unlink_clip_and_metadata(p)
                deleted.append(p.name)
            except OSError:
                logger.exception("Failed to delete %s", p.name)
                errors.append(p.name)

    if errors and not deleted:
        return jsonify({"ok": False, "error": f"Failed to delete {len(errors)} file(s)", "deleted": deleted}), 500
    return jsonify({"ok": True, "deleted": deleted, "errors": errors})


# ---------- Fleet: settings, retention, classifier config ----------


@bp.get("/api/fleet/settings")
def get_fleet_settings():
    return jsonify({"settings": _fleet_config().settings})


@bp.post("/api/fleet/settings")
def post_fleet_settings():
    body = request.get_json(force=True, silent=True) or {}
    patch = body.get("settings")
    fc = _fleet_config()
    if patch:
        fc.update_settings(patch)
    return jsonify(
        {
            "ok": True,
            "settings": fc.settings,
            "note": "web.host/web.port changes take effect on the next restart, not immediately.",
        }
    )


@bp.post("/api/fleet/auth-token/rotate")
def rotate_auth_token():
    new_token = _fleet_config().rotate_auth_token()
    return jsonify(
        {
            "ok": True,
            "token": new_token,
            "note": "Saved, but this process keeps using the OLD token until it restarts -- your "
            "session stays valid until then. Restart the service to make the new token active.",
        }
    )


@bp.post("/api/fleet/retention/run")
def run_retention_now():
    body = request.get_json(force=True, silent=True) or {}
    dry_run = bool(body.get("dry_run"))
    removed = _retention_scheduler().run_once(dry_run=dry_run)
    return jsonify({"ok": True, "dry_run": dry_run, "removed": [str(p) for p in removed]})


@bp.get("/api/fleet/retention/status")
def get_retention_status():
    return jsonify({"last_run": _retention_scheduler().last_run})


def _classifier_config_path() -> Path:
    return _fleet_config().config_dir / CLASSIFIER_CONFIG_FILENAME


@bp.get("/api/classifier/settings")
def get_classifier_settings():
    path = _classifier_config_path()
    raw = {}
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            return jsonify({"ok": False, "error": f"classifier.yaml is not valid YAML: {e}"}), 500
    settings = deep_merge(_CLASSIFIER_DEFAULTS, raw)
    if not settings.get("data_root"):
        settings["data_root"] = str(_fleet_config().resolved_data_root())
    return jsonify({"exists": path.is_file(), "settings": settings})


@bp.post("/api/classifier/settings")
def post_classifier_settings():
    body = request.get_json(force=True, silent=True) or {}
    patch = body.get("settings")
    if not isinstance(patch, dict):
        return jsonify({"ok": False, "error": "settings must be an object"}), 400

    path = _classifier_config_path()
    raw = {}
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            return jsonify({"ok": False, "error": f"classifier.yaml is not valid YAML: {e}"}), 500

    merged = deep_merge(deep_merge(_CLASSIFIER_DEFAULTS, raw), patch)
    if not merged.get("data_root"):
        merged["data_root"] = str(_fleet_config().resolved_data_root())

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(yaml.safe_dump(merged, sort_keys=False))
    tmp_path.replace(path)
    return jsonify({"ok": True, "settings": merged})


# ---------- Fleet: shutdown ----------


def _schedule_shutdown(delay: float = 0.5) -> None:
    """Send this process SIGTERM shortly after returning, so the HTTP
    response has time to flush to the client before shutdown begins. main.py
    already handles SIGTERM by cleanly stopping every camera, the retention
    scheduler, then exiting -- reused as-is here. Deployed with
    Restart=always (see deploy/camera-watcher.service), the process comes
    back up automatically -- this is how bind/token changes actually apply."""
    threading.Timer(delay, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


@bp.post("/api/shutdown")
def shutdown():
    body = request.get_json(force=True, silent=True) or {}
    confirm = str(body.get("confirm", "")).strip().lower()
    if confirm != _SHUTDOWN_CONFIRM_TEXT:
        return jsonify({"ok": False, "error": f'confirmation text must be "{_SHUTDOWN_CONFIRM_TEXT}"'}), 400

    logger.warning("Shutdown requested via the web UI -- stopping the fleet supervisor.")
    _schedule_shutdown()
    return jsonify(
        {
            "ok": True,
            "message": "Server is stopping. If deployed under systemd (Restart=always), it will come "
            "back up automatically, picking up any saved config changes.",
        }
    )
