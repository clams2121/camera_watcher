"""Configuration loading and persistence for a single camera process.

Each camera is configured by exactly one YAML file, passed via
``main.py --config /path/to/<camera>.yaml``. There is no "current working
directory" assumption anywhere in this module: every relative path found in
-- or derived from -- that file resolves against the directory *containing*
the config file. This is what makes it safe to run several camera processes
(systemd template units, cron, whatever) from any working directory.

``--config`` must point at a real file. Nothing here silently creates one:
seeding a config the caller explicitly named would hide a typo ("did I
really mean to start a fresh camera named front-door2?") behind what looks
like a successful startup, which is exactly the kind of silent fallback
these tools are built to avoid. Copy the template and edit it first:

    cp config/camera.example.yaml config/<name>.yaml

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
from typing import Any
from urllib.parse import quote

import yaml

# Used in filenames (clip names, sidecar names) and will be used as a
# systemd template-unit instance name later -- keep it to characters that
# are safe in both contexts.
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
    "retention": {
        "enabled": True,
        "max_age_days": 14,
        "max_total_gb": 50,
        "check_interval_seconds": 3600,
    },
    "web": {
        # "tailscale" resolves this host's Tailscale IPv4 address at startup
        # (see tailscale.py) and binds only there -- fails loud rather than
        # falling back to 0.0.0.0 if that can't be resolved. Set an explicit
        # literal host (e.g. "127.0.0.1") to bypass Tailscale entirely.
        "host": "tailscale",
        "port": 8080,
        "preview_fps": 5,
    },
    "mask": {
        # "" = derive "<camera.name>.mask.json" next to this config file.
        "path": "",
    },
}

# Camera credentials and the web UI's auth token -- never logged, never
# round-tripped through the settings API.
SECRET_DEFAULTS: dict[str, Any] = {"camera": {"username": "", "password": ""}, "web": {"auth_token": ""}}


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

    def _resolve(self, raw: str) -> Path:
        """Resolves `raw` against this config file's directory, unless it's
        already absolute. Never touches cwd."""
        p = Path(raw)
        return p if p.is_absolute() else (self.config_dir / p).resolve()

    def reload(self) -> None:
        with self._lock:
            raw = _load_yaml(self.config_path)
            settings = _deep_merge(DEFAULTS, raw)

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
            self._secrets = _deep_merge(SECRET_DEFAULTS, _load_yaml(self.secrets_path))

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
            self._settings = _deep_merge(self._settings, patch)
            _save_yaml(self.config_path, self._settings)
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
