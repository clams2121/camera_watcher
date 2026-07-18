"""HTTP routes for the camera_watcher web UI.

Deliberately small: a settings form, a mask editor, a live preview, and a
recordings browser -- nothing else. All state changes are written straight
through to disk via :class:`~camera_watcher.config.Config` and applied to
the running pipeline immediately, with no separate "apply" step.
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
from ..constants import TEMP_SUFFIX

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


def _config():
    return current_app.config["CAMERA_CONFIG"]


def _pipeline():
    return current_app.config["CAMERA_PIPELINE"]


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
    return render_template("index.html")


@bp.get("/api/settings")
def get_settings():
    config = _config()
    return jsonify(
        {
            "settings": config.settings,
            "has_credentials": config.has_credentials(),
            "redacted_rtsp_url": config.redacted_rtsp_url(),
        }
    )


@bp.post("/api/settings")
def post_settings():
    body = request.get_json(force=True, silent=True) or {}
    config = _config()
    pipeline = _pipeline()

    settings_patch = body.get("settings")
    if settings_patch:
        config.update_settings(settings_patch)

    credentials = body.get("credentials")
    if credentials:
        patch = {}
        if credentials.get("username"):
            patch["username"] = credentials["username"]
        if credentials.get("password"):
            patch["password"] = credentials["password"]
        if patch:
            config.update_secrets({"camera": patch})

    pipeline.apply_settings(config.resolved())
    return jsonify({"ok": True, "settings": config.settings, "has_credentials": config.has_credentials()})


@bp.get("/api/status")
def get_status():
    return jsonify(_pipeline().status())


@bp.get("/api/snapshot")
def get_snapshot():
    latest = _pipeline().frame_buffer.latest()
    if latest is None:
        return jsonify({"error": "no frames available yet"}), 503
    ok, buf = cv2.imencode(".jpg", latest.frame)
    if not ok:
        return jsonify({"error": "failed to encode snapshot"}), 500
    return Response(buf.tobytes(), mimetype="image/jpeg")


@bp.get("/api/mask")
def get_mask():
    return jsonify({"polygons": _pipeline().mask_store.polygons})


@bp.post("/api/mask")
def post_mask():
    body = request.get_json(force=True, silent=True) or {}
    polygons = body.get("polygons", [])
    pipeline = _pipeline()
    pipeline.mask_store.save(polygons)
    pipeline.reload_mask()
    return jsonify({"ok": True, "polygons": pipeline.mask_store.polygons})


@bp.get("/api/stream")
def get_stream():
    pipeline = _pipeline()
    fps = max(1, min(15, _config().settings["web"].get("preview_fps", 5)))
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


@bp.get("/api/recordings")
def list_recordings():
    """Recordings grouped into 30-minute buckets aligned to :00/:30, newest
    group first, newest clip first within each group -- lets the UI offer a
    "delete this whole half-hour" action alongside per-clip delete."""
    output_dir = Path(_config().resolved()["recording"]["output_dir"])
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


def _resolve_clip_path(filename: str) -> Optional[Path]:
    """Validates `filename` against the recorder's naming pattern and resolves
    it against the clips directory, refusing anything that would escape it.
    Returns None if the name is invalid or doesn't point at a real, finalized clip."""
    if not _CLIP_NAME_RE.match(filename) or filename.endswith(TEMP_SUFFIX):
        return None
    output_dir = Path(_config().resolved()["recording"]["output_dir"])
    file_path = (output_dir / filename).resolve()
    if output_dir not in file_path.parents or not file_path.is_file():
        return None
    return file_path


@bp.get("/api/recordings/<filename>")
def get_recording(filename: str):
    file_path = _resolve_clip_path(filename)
    if file_path is None:
        abort(404)

    # conditional=True (Flask's default) makes this honor Range requests,
    # which <video> needs to seek without downloading the whole file.
    return send_file(file_path, mimetype="video/mp4", conditional=True)


@bp.delete("/api/recordings/<filename>")
def delete_recording(filename: str):
    file_path = _resolve_clip_path(filename)
    if file_path is None:
        abort(404)
    try:
        _unlink_clip_and_metadata(file_path)
    except OSError:
        logger.exception("Failed to delete recording %s", filename)
        return jsonify({"ok": False, "error": "failed to delete the file"}), 500
    return jsonify({"ok": True})


@bp.post("/api/recordings/<filename>/review")
def review_recording(filename: str):
    """Records a human reviewer's keep/discard decision for a clip
    clip_classifier flagged as "review" -- writes <stem>.review.json
    either way; "discard" additionally deletes the clip (and its whole
    sidecar family, review.json included) via the same path the plain
    Delete button uses. This route is the sole writer of review.json."""
    file_path = _resolve_clip_path(filename)
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


@bp.delete("/api/recordings/group/<bucket>")
def delete_recording_group(bucket: str):
    if not _BUCKET_RE.match(bucket):
        abort(404)

    output_dir = Path(_config().resolved()["recording"]["output_dir"])
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


@bp.get("/api/heatmap.png")
def get_heatmap():
    png = _pipeline().heatmap_png()
    if png is None:
        return jsonify({"error": "no motion analyzed yet"}), 503
    return Response(png, mimetype="image/png")


@bp.post("/api/heatmap/reset")
def reset_heatmap():
    _pipeline().reset_heatmap()
    return jsonify({"ok": True})


def _schedule_shutdown(delay: float = 0.5) -> None:
    """Send this process SIGTERM shortly after returning, so the HTTP
    response has time to flush to the client before shutdown begins. main.py
    already handles SIGTERM by cleanly stopping the pipeline (capture,
    recorder, retention, accumulator) before exiting -- reused as-is here."""
    threading.Timer(delay, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


@bp.post("/api/shutdown")
def shutdown():
    body = request.get_json(force=True, silent=True) or {}
    confirm = str(body.get("confirm", "")).strip().lower()
    if confirm != _SHUTDOWN_CONFIRM_TEXT:
        return jsonify({"ok": False, "error": f'confirmation text must be "{_SHUTDOWN_CONFIRM_TEXT}"'}), 400

    logger.warning("Shutdown requested via the web UI -- stopping the server.")
    _schedule_shutdown()
    return jsonify({"ok": True, "message": "Server is stopping."})
