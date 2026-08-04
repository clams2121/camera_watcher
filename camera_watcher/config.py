"""Configuration loading and persistence for a single camera.

Each camera is configured by exactly one YAML file, one per camera under the
fleet's ``config/cameras/`` directory (see fleet.py's ``CameraManager``,
which discovers and owns these). There is no "current working directory"
assumption anywhere in this module: every relative path found in -- or
derived from -- a camera's config file resolves against the directory
*containing* that file.

``Config(path)`` requires the file to already exist -- it never silently
creates one; loading a path that was never created would otherwise hide a
typo behind what looks like a successful load. The one place allowed to
create a new camera config is ``Config.create()``, used by the fleet UI's
"add camera" flow, where creating a new file is exactly the intent rather
than a possible typo.

Camera credentials live in a separate file referenced *from* the main
config (``secrets_path``, default ``<camera.name>.secrets.yaml`` next to
the config file) so they can be gitignored independently and never show up
in a settings dump. The ignore-mask polygons work the same way
(``mask.path``, default ``<camera.name>.mask.json``).

``DEFAULTS`` / ``SECRET_DEFAULTS`` are merged under whatever's on disk, so a
minimal config (just enough to satisfy the required fields below) still
yields a complete, usable configuration.
"""
from __future__ import annotations

import copy
import re
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from .yaml_store import deep_merge, load_yaml, save_yaml

# Used in filenames (clip names, sidecar names) and as the systemd/fleet
# camera id -- keep it to characters that are safe in both contexts.
CAMERA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """Raised for any configuration problem that should stop startup with a
    clear, plain-English message instead of a raw traceback. Callers (see
    main.py) are expected to catch this at the top level, print `str(e)`,
    and exit nonzero -- never swallow it."""


DEFAULTS: dict[str, Any] = {
    # Shared root for this camera's output (clips, heatmap state, etc.).
    # Relative to this config file's directory unless given as an absolute
    # path. Multiple per-camera config files commonly point at the *same*
    # data_root (e.g. a shared /var/lib/camera-watcher/data) so retention can
    # sweep the whole fleet's clips from one place -- see retention.py.
    "data_root": "data",
    # "" = derive "<camera.name>.secrets.yaml" next to this config file.
    "secrets_path": "",
    "camera": {
        "name": "",  # required; validated against CAMERA_NAME_RE
        "host": "",
        "port": 554,
        # Recorded via passthrough stream-copy -- never decoded, so its
        # resolution/bitrate/codec don't affect CPU cost at all.
        "main_path": "/h264Preview_01_main",
        # Decoded for motion detection, live preview, and the mask editor
        # snapshot -- keep this the camera's lower-resolution substream.
        "sub_path": "/h264Preview_01_sub",
        "transport": "tcp",  # tcp or udp
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
        # "" = derive "<data_root>/clips/<camera.name>".
        "output_dir": "",
        # "" = derive "<data_root>/cache/<camera.name>" -- the rolling
        # passthrough segment cache (see segment_cache.py). Recommend
        # mounting this on tmpfs: constant small writes, fully disposable.
        "cache_dir": "",
        "segment_seconds": 2,  # length of each cached passthrough segment
        "pre_buffer_seconds": 10,
        "post_buffer_seconds": 10,
        "max_chunk_seconds": 180,
        "overlap_seconds": 5,
    },
    # Retention is fleet-wide, not per camera -- see fleet.py's FleetConfig
    # (retention windows/budget) and the in-process retention scheduler in
    # main.py, which sweeps every camera's clips under one shared data_root.
    #
    # The web UI itself is also fleet-wide now -- one process, one bind, one
    # auth token, all in FleetConfig -- so there is no per-camera web.host/
    # web.port here any more. preview_fps is the one camera-specific piece
    # of "web" behavior left (the live-preview MJPEG stream's frame rate).
    "web": {
        "preview_fps": 5,
    },
    "mask": {
        # "" = derive "<camera.name>.mask.json" next to this config file.
        "path": "",
    },
}

# Camera credentials -- never logged, never round-tripped through the
# settings API. The web UI's auth token now lives in FleetConfig (fleet.py)
# instead, since the UI is fleet-wide rather than per-camera.
SECRET_DEFAULTS: dict[str, Any] = {"camera": {"username": "", "password": ""}}


class Config:
    """Thread-safe holder for one camera's settings + secrets."""

    def __init__(self, config_path: Path | str):
        self.config_path = Path(config_path).resolve()
        if not self.config_path.is_file():
            raise ConfigError(
                f"Config file not found: {self.config_path}\n"
                f"Expected a camera config at that exact path. To create one:\n"
                f"    cp config/camera.example.yaml {self.config_path}\n"
                f"then edit it -- at minimum camera.name and camera.host -- before starting."
            )
        self.config_dir = self.config_path.parent
        self._lock = threading.RLock()
        self._settings: dict = {}
        self._secrets: dict = {}
        self.secrets_path: Path = self.config_path  # placeholder until reload() resolves the real one
        self.reload()

    @classmethod
    def create(cls, config_path: Path | str, settings_patch: Optional[dict] = None) -> "Config":
        """Creates a brand-new camera config file at `config_path` from
        DEFAULTS deep-merged with `settings_patch`, then loads it normally.
        Raises ConfigError if a file already exists there. This is the one
        place allowed to create a camera config from nothing -- used by
        fleet.py's CameraManager.add_camera(), where "create a new one" is
        exactly the caller's intent, unlike __init__ above."""
        config_path = Path(config_path)
        if config_path.exists():
            raise ConfigError(f"{config_path} already exists -- refusing to overwrite it.")
        save_yaml(config_path, deep_merge(DEFAULTS, settings_patch or {}))
        return cls(config_path)

    def _resolve(self, raw: str) -> Path:
        """Resolves `raw` against this config file's directory, unless it's
        already absolute. Never touches cwd."""
        p = Path(raw)
        return p if p.is_absolute() else (self.config_dir / p).resolve()

    def reload(self) -> None:
        with self._lock:
            raw = load_yaml(self.config_path)
            settings = deep_merge(DEFAULTS, raw)

            camera_name = settings["camera"].get("name") or ""
            if not camera_name:
                raise ConfigError(f"{self.config_path}: camera.name is required and cannot be blank.")
            if not CAMERA_NAME_RE.match(camera_name):
                raise ConfigError(
                    f"{self.config_path}: camera.name {camera_name!r} is invalid -- "
                    f"it must match {CAMERA_NAME_RE.pattern!r} (letters, digits, '_' and '-' only). "
                    f"This name is used in clip filenames and (later) systemd instance names."
                )

            self._settings = settings
            secrets_rel = settings.get("secrets_path") or f"{camera_name}.secrets.yaml"
            self.secrets_path = self._resolve(secrets_rel)
            self._secrets = deep_merge(SECRET_DEFAULTS, load_yaml(self.secrets_path))

    @property
    def settings(self) -> dict:
        """A deep copy of the settings exactly as persisted -- relative
        paths and "derive a default" sentinels intact. This is what the web
        UI reads and writes; it never sees resolved absolute paths, so
        saving the form back never bakes an absolute path into the file."""
        with self._lock:
            return copy.deepcopy(self._settings)

    @property
    def secrets(self) -> dict:
        """A deep copy of the current secrets. Handle with care -- never log this."""
        with self._lock:
            return copy.deepcopy(self._secrets)

    def resolved(self) -> dict:
        """Settings with every path-valued field resolved to an absolute
        path (relative to this config file's directory) and "derive a
        default" sentinels filled in from data_root + camera.name. This is
        what the pipeline/recorder/mask store/retention should consume for
        any actual filesystem access -- never persisted back to disk."""
        with self._lock:
            settings = copy.deepcopy(self._settings)

        camera_name = settings["camera"]["name"]
        data_root = self._resolve(settings["data_root"] or "data")
        settings["data_root"] = str(data_root)

        settings["motion"]["heatmap_path"] = str(self._resolve(settings["motion"]["heatmap_path"]))

        mask_path = settings["mask"]["path"] or f"{camera_name}.mask.json"
        settings["mask"]["path"] = str(self._resolve(mask_path))

        output_dir = settings["recording"]["output_dir"]
        settings["recording"]["output_dir"] = (
            str(self._resolve(output_dir)) if output_dir else str(data_root / "clips" / camera_name)
        )

        cache_dir = settings["recording"]["cache_dir"]
        settings["recording"]["cache_dir"] = (
            str(self._resolve(cache_dir)) if cache_dir else str(data_root / "cache" / camera_name)
        )

        return settings

    def update_settings(self, patch: dict) -> dict:
        """Deep-merge ``patch`` into settings and persist to disk."""
        with self._lock:
            self._settings = deep_merge(self._settings, patch)
            save_yaml(self.config_path, self._settings)
            return copy.deepcopy(self._settings)

    def update_secrets(self, patch: dict) -> None:
        """Deep-merge ``patch`` into secrets and persist to disk. Never returns the result."""
        with self._lock:
            self._secrets = deep_merge(self._secrets, patch)
            save_yaml(self.secrets_path, self._secrets)

    def has_credentials(self) -> bool:
        with self._lock:
            creds = self._secrets["camera"]
            return bool(creds.get("username") or creds.get("password"))

    def rtsp_url(self, stream: str = "sub") -> str:
        """Build the full RTSP URL for ``stream`` ("main" or "sub"), including
        credentials. Never log or display this."""
        with self._lock:
            cam = self._settings["camera"]
            creds = self._secrets["camera"]
        user, password = creds.get("username", ""), creds.get("password", "")
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if (user or password) else ""
        raw_path = cam[self._stream_field(stream)]
        path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
        return f"rtsp://{auth}{cam['host']}:{cam['port']}{path}"

    def redacted_rtsp_url(self, stream: str = "sub") -> str:
        """URL with credentials stripped, safe for logs and the UI."""
        with self._lock:
            cam = self._settings["camera"]
        raw_path = cam[self._stream_field(stream)]
        path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
        return f"rtsp://{cam['host']}:{cam['port']}{path}"

    @staticmethod
    def _stream_field(stream: str) -> str:
        if stream not in ("main", "sub"):
            raise ValueError(f"stream must be 'main' or 'sub', got {stream!r}")
        return f"{stream}_path"
