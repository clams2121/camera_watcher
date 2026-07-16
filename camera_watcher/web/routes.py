"""HTTP routes for the camera_watcher web UI.

Deliberately small: a settings form, a mask editor, a live preview, and a
recordings browser -- nothing else. All state changes are written straight
through to disk via :class:`~camera_watcher.config.Config` and applied to
the running pipeline immediately, with no separate "apply" step.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import cv2
from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, send_file

from ..constants import TEMP_SUFFIX

bp = Blueprint("camera_watcher", __name__)
logger = logging.getLogger(__name__)

# Recording filenames are always "<camera_name>_<YYYYMMDD>_<HHMMSS>.mp4"
# (see recorder.py) -- reject anything else outright before it ever touches
# the filesystem, so a crafted filename can't be used to escape output_dir.
_CLIP_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.mp4$")


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
    output_dir = Path(_config().settings["recording"]["output_dir"])
    recordings = []
    if output_dir.exists():
        for p in output_dir.iterdir():
            if p.is_file() and p.suffix == ".mp4" and not p.name.endswith(TEMP_SUFFIX):
                try:
                    stat = p.stat()
                except OSError:
                    continue
                recordings.append({"name": p.name, "size_bytes": stat.st_size, "modified": stat.st_mtime})
    recordings.sort(key=lambda r: r["modified"], reverse=True)
    return jsonify({"recordings": recordings})


@bp.get("/api/recordings/<filename>")
def get_recording(filename: str):
    if not _CLIP_NAME_RE.match(filename) or filename.endswith(TEMP_SUFFIX):
        abort(404)

    output_dir = Path(_config().settings["recording"]["output_dir"]).resolve()
    file_path = (output_dir / filename).resolve()
    if output_dir not in file_path.parents or not file_path.is_file():
        abort(404)

    # conditional=True (Flask's default) makes this honor Range requests,
    # which <video> needs to seek without downloading the whole file.
    return send_file(file_path, mimetype="video/mp4", conditional=True)


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
