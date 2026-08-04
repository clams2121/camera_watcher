"""Configuration for the single-camera-stream process.

Exactly one YAML file -- ``config.yaml`` in this same directory by default
(see main.py's ``--config`` to point at a different one). Every relative
path inside it (currently just ``recording.output_dir``) resolves against
the directory *that config file* lives in, never the current working
directory -- the same "no cwd assumptions" rule the rest of this project
follows.

``--config`` (or the default path) must point at a real file -- nothing is
auto-created for you here. Pointing it at a path that doesn't exist fails
immediately with the exact ``cp`` command needed, rather than silently
starting with defaults for a camera you never actually configured.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

# Used in clip filenames -- keep it to characters that are safe there.
CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# All the parameters this project's original, single-process version
# exposed for one camera: connection details, motion-detection tuning, and
# recording buffer/chunk timing. Deliberately excludes anything that only
# makes sense for the fleet supervisor (data_root, secrets_path, the web
# UI's host/port/auth_token, retention) -- this tool has no UI and no
# fleet to share a budget with.
DEFAULTS: dict[str, Any] = {
    "camera": {
        "name": "camera1",  # used in clip filenames
        "host": "",
        "port": 554,
        "path": "/h264Preview_01_main",
        "transport": "tcp",  # tcp or udp
        "username": "",
        "password": "",
    },
    "motion": {
        "enabled": True,
        "analysis_width": 320,  # frames are downscaled to this width before diffing, to save CPU
        "min_area": 500,  # minimum contour area (px, at analysis_width) to count as motion
        "var_threshold": 25,  # MOG2 sensitivity; lower = more sensitive
        "history": 300,  # number of frames of background history MOG2 considers
        "draw_bounding_box": False,  # burn a box around detected motion into recorded frames
        "box_padding_px": 12,  # gap kept between the drawn box and the actual moving object
    },
    "recording": {
        "output_dir": "output",  # resolved relative to this config file's directory
        "pre_buffer_seconds": 10,  # seconds of video kept before motion is confirmed
        "post_buffer_seconds": 10,  # seconds to keep recording after motion stops
        "max_chunk_seconds": 180,  # hard cap per file
        "overlap_seconds": 5,  # seconds repeated at the start of the next chunk on a forced split
        "fallback_fps": 15.0,  # used only if the frame buffer can't yet measure a real rate
    },
}

class ConfigError(Exception):
    """Raised for any configuration problem that should stop startup with a
    clear, plain-English message instead of a raw traceback."""


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """Loads and validates one camera's settings from a single YAML file."""

    def __init__(self, config_path: Path | str):
        self.config_path = Path(config_path).resolve()
        if not self.config_path.is_file():
            raise ConfigError(
                f"Config file not found: {self.config_path}\n"
                f"Copy the template and edit it first:\n"
                f"    cp single_camera_stream/config.example.yaml {self.config_path}\n"
                f"then edit it -- at minimum camera.host -- before starting."
            )
        self.config_dir = self.config_path.parent

        with self.config_path.open("r") as f:
            raw = yaml.safe_load(f) or {}
        self.settings = _deep_merge(DEFAULTS, raw)

        camera_name = self.settings["camera"].get("name") or ""
        if not camera_name:
            raise ConfigError(f"{self.config_path}: camera.name is required and cannot be blank.")
        if not CAMERA_NAME_RE.match(camera_name):
            raise ConfigError(
                f"{self.config_path}: camera.name {camera_name!r} is invalid -- "
                f"it must match {CAMERA_NAME_RE.pattern!r} (letters, digits, '_' and '-' only)."
            )

        host = self.settings["camera"].get("host") or ""
        if not host:
            raise ConfigError(f"{self.config_path}: camera.host is required and cannot be blank.")

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (self.config_dir / p).resolve()

    @property
    def output_dir(self) -> Path:
        return self._resolve(self.settings["recording"]["output_dir"])

    def rtsp_url(self) -> str:
        """Full RTSP URL including credentials. Never log or display this."""
        cam = self.settings["camera"]
        user, password = cam.get("username", ""), cam.get("password", "")
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if (user or password) else ""
        path = cam["path"] if cam["path"].startswith("/") else f"/{cam['path']}"
        return f"rtsp://{auth}{cam['host']}:{cam['port']}{path}"

    def redacted_rtsp_url(self) -> str:
        """URL with credentials stripped, safe for logs."""
        cam = self.settings["camera"]
        path = cam["path"] if cam["path"].startswith("/") else f"/{cam['path']}"
        return f"rtsp://{cam['host']}:{cam['port']}{path}"
