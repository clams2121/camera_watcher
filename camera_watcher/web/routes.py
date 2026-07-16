"""HTTP routes for the camera_watcher web UI.

Deliberately small: a settings form, a mask editor, and a live preview --
nothing else. All state changes are written straight through to disk via
:class:`~camera_watcher.config.Config` and applied to the running pipeline
immediately, with no separate "apply" step.
"""
from __future__ import annotations

import logging
import time

import cv2
from flask import Blueprint, Response, current_app, jsonify, render_template, request

bp = Blueprint("camera_watcher", __name__)
logger = logging.getLogger(__name__)


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
