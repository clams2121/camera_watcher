"""Flask app factory for the camera_watcher fleet web UI."""
from __future__ import annotations

import hashlib

from flask import Flask

from ..fleet import CameraManager, FleetConfig
from ..retention import RetentionScheduler


def create_app(
    fleet_config: FleetConfig,
    camera_manager: CameraManager,
    retention_scheduler: RetentionScheduler,
    auth_token: str,
) -> Flask:
    app = Flask(__name__)
    app.config["FLEET_CONFIG"] = fleet_config
    app.config["CAMERA_MANAGER"] = camera_manager
    app.config["RETENTION_SCHEDULER"] = retention_scheduler
    app.config["AUTH_TOKEN"] = auth_token
    # Derived from the auth token (itself a real secret) rather than a
    # separate generated value -- one less thing to configure/rotate, and
    # session cookies naturally stop verifying once the token changes.
    app.secret_key = hashlib.sha256(f"session-signing:{auth_token}".encode()).digest()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    from .routes import bp

    app.register_blueprint(bp)
    return app
