"""Flask app factory for the camera_watcher web UI."""
from __future__ import annotations

from flask import Flask

from ..config import Config
from ..pipeline import CameraPipeline


def create_app(config: Config, pipeline: CameraPipeline) -> Flask:
    app = Flask(__name__)
    app.config["CAMERA_CONFIG"] = config
    app.config["CAMERA_PIPELINE"] = pipeline

    from .routes import bp

    app.register_blueprint(bp)
    return app
