"""Configuration loading for clip_classifier.

One classifier process watches a whole data root shared by however many
camera_watcher instances point at it -- not one config per camera. Passed
via ``--config /path/to/classifier.yaml``. Same rule as camera_watcher's
Config (see camera_watcher/config.py): every relative path in that file
resolves against the directory *containing* the config file, never the
current working directory, so this can run from any working directory
(e.g. as a systemd service).

``--config`` must point at a real file. Nothing here silently creates one --
see camera_watcher/config.py's docstring for why.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised for any configuration problem that should stop startup with a
    clear, plain-English message instead of a raw traceback."""


DEFAULTS: dict[str, Any] = {
    # Required -- no sensible default. Must point at the same data_root
    # camera_watcher's fleet shares (i.e. the directory containing
    # clips/<camera_name>/...), so clips from every camera get picked up.
    "data_root": "",
    "backend": "auto",  # auto | cpu | hailo -- see detector.py
    "cpu": {
        # Resolved relative to this config file. Fetched via
        # `python -m clip_classifier.fetch_model` -- see that module for the
        # pinned download URL and checksum (not configurable here on
        # purpose: the checksum is what makes the pin meaningful, so it
        # isn't something a config file should be able to silently change).
        "model_path": "models/yolov8n.onnx",
    },
    "hailo": {
        "hef_path": "models/yolov8n.hef",
    },
    "thresholds": {
        "high_confidence": 0.5,
        "review_large_object_area_frac": 0.05,
        "review_persistent_detection_frac": 0.6,
        "review_persistent_motion_detection_size": 0.05,
        "review_persistent_motion_frame_ratio": 0.6,
    },
    "sampling": {
        "max_frames": 5,
        "min_frame_spacing_seconds": 1.0,
    },
    "watch": {
        "queue_maxsize": 256,
        # Safety-net re-scan of the whole data root -- inotify events can be
        # missed (a brief watcher restart, an NFS-backed data_root, etc.).
        # This bounds how long a clip could stay invisibly unclassified if
        # that happens, without needing an operator to notice and restart
        # the service by hand.
        "rescan_interval_seconds": 600,
    },
}


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


class Config:
    """Thread-safe-enough holder for the classifier's settings -- this
    process has no concurrent writers to its own config (nothing else edits
    classifier.yaml at runtime), unlike camera_watcher's per-camera Config."""

    def __init__(self, config_path: Path | str):
        self.config_path = Path(config_path).resolve()
        if not self.config_path.is_file():
            raise ConfigError(
                f"Config file not found: {self.config_path}\n"
                f"Expected the classifier's config at that exact path. To create one:\n"
                f"    cp config/classifier.example.yaml {self.config_path}\n"
                f"then edit it -- at minimum data_root -- before starting."
            )
        self.config_dir = self.config_path.parent
        self.reload()

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (self.config_dir / p).resolve()

    def reload(self) -> None:
        raw = _load_yaml(self.config_path)
        settings = _deep_merge(DEFAULTS, raw)

        if not settings["data_root"]:
            raise ConfigError(f"{self.config_path}: data_root is required and cannot be blank.")
        if settings["backend"] not in ("auto", "cpu", "hailo"):
            raise ConfigError(
                f"{self.config_path}: backend must be one of auto/cpu/hailo, got {settings['backend']!r}."
            )

        self._settings = settings

    def resolved(self) -> dict:
        """Settings with every path-valued field resolved to an absolute
        path. Never persisted -- this config is never written back to disk
        by this process."""
        settings = copy.deepcopy(self._settings)
        settings["data_root"] = str(self._resolve(settings["data_root"]))
        settings["cpu"]["model_path"] = str(self._resolve(settings["cpu"]["model_path"]))
        settings["hailo"]["hef_path"] = str(self._resolve(settings["hailo"]["hef_path"]))
        return settings
