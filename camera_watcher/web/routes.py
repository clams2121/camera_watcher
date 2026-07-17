"""HTTP routes for the camera_watcher web UI.

Deliberately small: a settings form, a mask editor, a live preview, and a
recordings browser -- nothing else. All state changes are written straight
through to disk via :class:`~camera_watcher.config.Config` and applied to
the running pipeline immediately, with no separate "apply" step.
"""
from __future__ import annotations

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
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, send_file

from ..constants import TEMP_SUFFIX
from ..update import install_dependencies, pull_latest, repo_root

bp = Blueprint("camera_watcher", __name__)
logger = logging.getLogger(__name__)

_SHUTDOWN_CONFIRM_TEXT = "quit"
_UPDATE_CONFIRM_TEXT = "update"

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


def _unlink_clip_and_metadata(file_path: Path) -> None:
    """Removes a clip and its companion <clip stem>.json metadata file, if any."""
    file_path.unlink()
    metadata_path = file_path.with_suffix(".json")
    try:
        metadata_path.unlink(missing_ok=True)
    except OSError:
        logger.exception("Failed to remove metadata file %s", metadata_path)


def _config():
    return current_app.config["CAMERA_CONFIG"]


def _pipeline():
    return current_app.config["CAMERA_PIPELINE"]


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

    pipeline.apply_settings(config.settings)
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
                ok, buf = cv2.imencode(".jpg", latest.frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
                    )
            time.sleep(interval)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@bp.get("/api/recordings")
def list_recordings():
    """Recordings grouped into 30-minute buckets aligned to :00/:30, newest
    group first, newest clip first within each group -- lets the UI offer a
    "delete this whole half-hour" action alongside per-clip delete."""
    output_dir = Path(_config().settings["recording"]["output_dir"])
    buckets: dict = {}
    if output_dir.exists():
        for p in output_dir.iterdir():
            if p.is_file() and p.suffix == ".mp4" and not p.name.endswith(TEMP_SUFFIX):
                try:
                    stat = p.stat()
                except OSError:
                    continue
                key = _bucket_key(_clip_start_datetime(p, stat))
                buckets.setdefault(key, []).append(
                    {"name": p.name, "size_bytes": stat.st_size, "modified": stat.st_mtime}
                )

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
    output_dir = Path(_config().settings["recording"]["output_dir"]).resolve()
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


@bp.delete("/api/recordings/group/<bucket>")
def delete_recording_group(bucket: str):
    if not _BUCKET_RE.match(bucket):
        abort(404)

    output_dir = Path(_config().settings["recording"]["output_dir"])
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


def _schedule_restart(delay: float = 0.5) -> None:
    """Send this process SIGUSR1 shortly after returning, for the same
    flush-the-response-first reason as _schedule_shutdown. main.py handles
    SIGUSR1 by cleanly stopping the pipeline and then re-exec'ing itself,
    picking up whatever code is now on disk."""
    threading.Timer(delay, lambda: os.kill(os.getpid(), signal.SIGUSR1)).start()


@bp.post("/api/update")
def update():
    body = request.get_json(force=True, silent=True) or {}
    confirm = str(body.get("confirm", "")).strip().lower()
    if confirm != _UPDATE_CONFIRM_TEXT:
        return jsonify({"ok": False, "error": f'confirmation text must be "{_UPDATE_CONFIRM_TEXT}"'}), 400

    root = repo_root()
    pull_result, updated = pull_latest(root)
    if not pull_result.ok:
        logger.warning("Update: git pull failed: %s", pull_result.message)
        return jsonify({"ok": False, "updated": False, "error": pull_result.message}), 500

    if not updated:
        return jsonify({"ok": True, "updated": False, "message": pull_result.message})

    deps_result = install_dependencies(root)
    if not deps_result.ok:
        logger.warning("Update: dependency install failed: %s", deps_result.message)
        return (
            jsonify(
                {
                    "ok": False,
                    "updated": True,
                    "error": "Pulled new code, but installing dependencies failed -- not restarting: "
                    + deps_result.message,
                }
            ),
            500,
        )

    logger.warning("Update requested via the web UI -- pulled latest code, restarting.")
    _schedule_restart()
    return jsonify({"ok": True, "updated": True, "message": "Updated. Restarting..."})
