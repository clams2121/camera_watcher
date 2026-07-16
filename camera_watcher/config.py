"""Configuration loading and persistence.

Settings are split across two files so credentials never end up in git:

- ``settings.yaml`` -- everything non-secret, safe to commit.
- ``secrets.yaml``  -- camera username/password only, gitignored.

Both are merged over :data:`DEFAULTS` / :data:`SECRET_DEFAULTS` so a partial
or missing file still yields a complete, usable configuration.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")
DEFAULT_SECRETS_PATH = Path("config/secrets.yaml")

DEFAULTS: dict[str, Any] = {
    "camera": {
        "name": "camera1",
        "host": "",
        "port": 554,
        "path": "/",
        "transport": "tcp",
    },
    "motion": {
        "enabled": True,
        "analysis_width": 320,
        "min_area": 500,
        "var_threshold": 25,
        "history": 300,
        "draw_bounding_box": False,  # burn a box around detected motion into recorded frames -- testing aid, off by default
        "box_padding_px": 12,  # gap kept between the box and the detected contour, at full frame resolution
        "heatmap_path": "data/motion_heatmap.npy",  # never-decaying per-pixel motion accumulator, cleared via the UI
    },
    "recording": {
        "output_dir": "data/clips",
        "pre_buffer_seconds": 10,
        "post_buffer_seconds": 10,
        "max_chunk_seconds": 180,
        "overlap_seconds": 5,
        "fourcc": "mp4v",
        "max_width": 1920,
        "event_log_path": "data/motion_events.jsonl",  # one JSON line per finalized clip with its motion bounding box; blank disables it
    },
    "retention": {
        "enabled": True,
        "max_age_days": 14,
        "max_total_gb": 50,
        "check_interval_seconds": 3600,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "preview_fps": 5,
    },
    "mask": {
        "path": "config/mask.json",
    },
}

# Only the camera credentials are considered secret.
SECRET_DEFAULTS: dict[str, Any] = {"camera": {"username": "", "password": ""}}


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    tmp_path.replace(path)
    try:
        # Best-effort: keep the secrets file from being world/group readable.
        path.chmod(0o600)
    except OSError:
        pass


class Config:
    """Thread-safe holder for settings + secrets, backed by two YAML files."""

    def __init__(
        self,
        settings_path: Path | str = DEFAULT_SETTINGS_PATH,
        secrets_path: Path | str = DEFAULT_SECRETS_PATH,
    ):
        self.settings_path = Path(settings_path)
        self.secrets_path = Path(secrets_path)
        self._lock = threading.RLock()
        self._settings: dict = {}
        self._secrets: dict = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._settings = _deep_merge(DEFAULTS, _load_yaml(self.settings_path))
            self._secrets = _deep_merge(SECRET_DEFAULTS, _load_yaml(self.secrets_path))

    @property
    def settings(self) -> dict:
        """A deep copy of the current non-secret settings."""
        with self._lock:
            return copy.deepcopy(self._settings)

    @property
    def secrets(self) -> dict:
        """A deep copy of the current secrets. Handle with care -- never log this."""
        with self._lock:
            return copy.deepcopy(self._secrets)

    def update_settings(self, patch: dict) -> dict:
        """Deep-merge ``patch`` into settings and persist to disk."""
        with self._lock:
            self._settings = _deep_merge(self._settings, patch)
            _save_yaml(self.settings_path, self._settings)
            return copy.deepcopy(self._settings)

    def update_secrets(self, patch: dict) -> None:
        """Deep-merge ``patch`` into secrets and persist to disk. Never returns the result."""
        with self._lock:
            self._secrets = _deep_merge(self._secrets, patch)
            _save_yaml(self.secrets_path, self._secrets)

    def has_credentials(self) -> bool:
        with self._lock:
            creds = self._secrets["camera"]
            return bool(creds.get("username") or creds.get("password"))

    def rtsp_url(self) -> str:
        """Build the full RTSP URL, including credentials. Never log or display this."""
        with self._lock:
            cam = self._settings["camera"]
            creds = self._secrets["camera"]
        user, password = creds.get("username", ""), creds.get("password", "")
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if (user or password) else ""
        path = cam["path"] if cam["path"].startswith("/") else f"/{cam['path']}"
        return f"rtsp://{auth}{cam['host']}:{cam['port']}{path}"

    def redacted_rtsp_url(self) -> str:
        """URL with credentials stripped, safe for logs and the UI."""
        with self._lock:
            cam = self._settings["camera"]
        path = cam["path"] if cam["path"].startswith("/") else f"/{cam['path']}"
        return f"rtsp://{cam['host']}:{cam['port']}{path}"
