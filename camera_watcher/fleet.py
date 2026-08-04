"""Fleet-wide configuration and camera supervision.

This is the core of the always-on supervisor: one :class:`FleetConfig`
(web bind host/port, auth token, retention settings -- everything that
used to be per-camera or a systemd-timer-only CLI flag) and one
:class:`CameraManager` (owns every camera's :class:`~.config.Config` and
:class:`~.pipeline.CameraPipeline`, and is deliberately tolerant of any
single camera's problems).

The key property CameraManager provides: a camera that fails to load (bad
YAML, blank camera.name, whatever) or fails to start never raises out of
``start()``, ``add_camera()``, or ``update_camera()`` in a way that could
take down the manager itself -- it's recorded as an *error* against that
one camera_id instead. This is what lets the web UI (see main.py) always
come up, regardless of how many cameras are individually broken.
"""
from __future__ import annotations

import copy
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .auth import generate_token
from .config import CAMERA_NAME_RE, Config, ConfigError
from .pipeline import CameraPipeline
from .yaml_store import deep_merge, load_yaml, save_yaml

logger = logging.getLogger(__name__)

FLEET_DEFAULTS: Dict[str, Any] = {
    # Shared root for every camera's clips/cache/heatmap data. New cameras
    # get this baked into their own config as an absolute path at creation
    # time (see CameraManager.add_camera) -- the whole fleet always shares
    # one root without any camera needing to know or type it.
    "data_root": "data",
    "web": {
        # "tailscale" resolves this host's Tailscale IPv4 address at startup
        # (see tailscale.py) and binds only there -- fails loud rather than
        # falling back to 0.0.0.0 if that can't be resolved. Set an explicit
        # literal host (e.g. "127.0.0.1") to bypass Tailscale entirely.
        "host": "tailscale",
        "port": 8080,
    },
    "retention": {
        "low_max_age_hours": 48.0,
        "high_max_age_days": 30.0,
        "review_max_age_days": 30.0,
        "max_total_gb": None,
        "interval_minutes": 60.0,
    },
}

FLEET_SECRET_DEFAULTS: Dict[str, Any] = {"web": {"auth_token": ""}}


class FleetConfig:
    """Thread-safe holder for fleet-wide settings + secrets.

    Unlike a per-camera :class:`~.config.Config`, this auto-bootstraps:
    ``fleet.yaml`` and ``fleet.secrets.yaml`` (the latter with a freshly
    generated auth token) are created automatically on first load if
    missing, so the UI can always come up with zero manual setup. There's
    no typo-risk to protect against here the way there is for per-camera
    configs -- there is exactly one fleet config, always at this one path.
    """

    def __init__(self, config_dir: Path | str):
        self.config_dir = Path(config_dir).resolve()
        self.config_path = self.config_dir / "fleet.yaml"
        self.secrets_path = self.config_dir / "fleet.secrets.yaml"
        self._lock = threading.RLock()
        self._settings: dict = {}
        self._secrets: dict = {}
        # Set only on the run that actually generated a fresh token, so
        # main.py can print/log it once, loudly, right after startup.
        self.bootstrapped_token: Optional[str] = None
        self.reload()

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (self.config_dir / p).resolve()

    def reload(self) -> None:
        with self._lock:
            first_boot = not self.config_path.exists()
            self._settings = deep_merge(FLEET_DEFAULTS, load_yaml(self.config_path))
            if first_boot:
                save_yaml(self.config_path, self._settings)
                logger.info("First boot: created %s with default fleet settings.", self.config_path)

            self._secrets = deep_merge(FLEET_SECRET_DEFAULTS, load_yaml(self.secrets_path))
            if not self._secrets["web"]["auth_token"]:
                token = generate_token()
                self._secrets["web"]["auth_token"] = token
                save_yaml(self.secrets_path, self._secrets)
                self.bootstrapped_token = token

    @property
    def settings(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._settings)

    @property
    def secrets(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._secrets)

    def resolved_data_root(self) -> Path:
        with self._lock:
            return self._resolve(self._settings["data_root"] or "data")

    def resolved_clips_root(self) -> Path:
        return self.resolved_data_root() / "clips"

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            self._settings = deep_merge(self._settings, patch)
            save_yaml(self.config_path, self._settings)
            return copy.deepcopy(self._settings)

    def update_secrets(self, patch: dict) -> None:
        with self._lock:
            self._secrets = deep_merge(self._secrets, patch)
            save_yaml(self.secrets_path, self._secrets)

    def rotate_auth_token(self) -> str:
        """Generates and persists a brand-new auth token, returning it once
        -- the only time it's available in plaintext outside the secrets
        file -- so the caller (the web UI) can show it to the operator
        exactly once, the same way the bootstrap token is shown."""
        token = generate_token()
        self.update_secrets({"web": {"auth_token": token}})
        return token


class CameraManager:
    """Owns every camera's Config + CameraPipeline, discovered from
    ``<config_dir>/cameras/*.yaml``. Every public method here is safe to
    call from a Flask request thread while camera worker threads are
    running concurrently."""

    def __init__(self, fleet_config: FleetConfig):
        self.fleet_config = fleet_config
        self.cameras_dir = fleet_config.config_dir / "cameras"
        self.cameras_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._configs: Dict[str, Config] = {}
        self._pipelines: Dict[str, CameraPipeline] = {}
        self._errors: Dict[str, str] = {}

    def _config_path(self, camera_id: str) -> Path:
        return self.cameras_dir / f"{camera_id}.yaml"

    def start(self) -> None:
        """Discovers every camera config file and starts a worker for each,
        one at a time -- a failure on any one is recorded and never stops
        the rest from being tried."""
        with self._lock:
            for path in sorted(self.cameras_dir.glob("*.yaml")):
                if path.name.endswith(".secrets.yaml"):
                    continue
                self._start_worker(path.stem, path)

    def _start_worker(self, camera_id: str, config_path: Path) -> None:
        """Must be called with self._lock held. Never raises -- any failure
        loading the config or starting the pipeline is recorded against
        camera_id instead."""
        try:
            config = Config(config_path)
            pipeline = CameraPipeline(config)
            pipeline.start()
        except Exception as e:
            logger.exception("Camera %r failed to start", camera_id)
            self._errors[camera_id] = str(e)
            self._configs.pop(camera_id, None)
            self._pipelines.pop(camera_id, None)
            return
        self._configs[camera_id] = config
        self._pipelines[camera_id] = pipeline
        self._errors.pop(camera_id, None)
        logger.info("Camera %r started", camera_id)

    def stop(self) -> None:
        with self._lock:
            pipelines = list(self._pipelines.values())
        for pipeline in pipelines:
            try:
                pipeline.stop()
            except Exception:
                logger.exception("Error stopping a camera pipeline")

    def exists(self, camera_id: str) -> bool:
        with self._lock:
            return camera_id in self._configs or camera_id in self._errors

    def get_config(self, camera_id: str) -> Optional[Config]:
        with self._lock:
            return self._configs.get(camera_id)

    def get_config_for_editing(self, camera_id: str) -> Optional[Config]:
        """Like get_config(), but for a camera that's currently errored
        (never successfully started), tries a fresh load instead of
        returning None -- used by routes that need to read/write a
        camera's persisted settings regardless of whether its pipeline is
        currently running. Returns None only if no such camera exists at
        all; raises ConfigError (propagated to the caller) if the camera
        exists but its config genuinely can't be loaded (e.g. corrupt
        YAML)."""
        with self._lock:
            config = self._configs.get(camera_id)
            if config is not None:
                return config
            config_path = self._config_path(camera_id)
            if not config_path.exists():
                return None
            return Config(config_path)

    def get_pipeline(self, camera_id: str) -> Optional[CameraPipeline]:
        with self._lock:
            return self._pipelines.get(camera_id)

    def get_error(self, camera_id: str) -> Optional[str]:
        with self._lock:
            return self._errors.get(camera_id)

    def _summarize(self, camera_id: str) -> dict:
        """Must be called with self._lock held."""
        config = self._configs.get(camera_id)
        pipeline = self._pipelines.get(camera_id)
        entry = {
            "id": camera_id,
            "name": camera_id,
            "error": self._errors.get(camera_id),
            "connected": False,
            "recording": False,
            "has_credentials": False,
            "host": None,
        }
        if config is not None:
            settings = config.settings
            entry["host"] = settings["camera"]["host"]
            entry["has_credentials"] = config.has_credentials()
        if pipeline is not None:
            status = pipeline.status()
            entry["connected"] = status["connected"]
            entry["recording"] = status["recording"]
        return entry

    def list_cameras(self) -> List[dict]:
        with self._lock:
            camera_ids = sorted(set(self._configs) | set(self._errors))
            return [self._summarize(cid) for cid in camera_ids]

    def add_camera(self, settings_patch: dict, secrets_patch: Optional[dict] = None) -> dict:
        camera_name = str((settings_patch.get("camera") or {}).get("name") or "").strip()
        if not camera_name:
            raise ConfigError("camera.name is required.")
        if not CAMERA_NAME_RE.match(camera_name):
            raise ConfigError(
                f"camera.name {camera_name!r} is invalid -- it must match "
                f"{CAMERA_NAME_RE.pattern!r} (letters, digits, '_' and '-' only)."
            )

        config_path = self._config_path(camera_name)
        with self._lock:
            if self.exists(camera_name) or config_path.exists():
                raise ConfigError(f"A camera named {camera_name!r} already exists.")

            # Every camera shares the fleet's one data_root -- baked in as
            # an absolute path here so retention/rebuild_index/etc. never
            # need each camera to agree on it independently.
            full_patch = deep_merge(settings_patch, {"data_root": str(self.fleet_config.resolved_data_root())})
            config = Config.create(config_path, full_patch)
            if secrets_patch:
                config.update_secrets(secrets_patch)

            self._start_worker(camera_name, config_path)
            return self._summarize(camera_name)

    def update_camera(
        self, camera_id: str, settings_patch: Optional[dict] = None, secrets_patch: Optional[dict] = None
    ) -> dict:
        with self._lock:
            config_path = self._config_path(camera_id)
            config = self._configs.get(camera_id)
            if config is None:
                if not config_path.exists():
                    raise ConfigError(f"No such camera: {camera_id!r}")
                # Previously errored (or never started) -- still need a
                # Config to write the patch to; may itself raise
                # ConfigError if the file is genuinely broken, which
                # propagates to the caller as-is.
                config = Config(config_path)

            if settings_patch:
                config.update_settings(settings_patch)
            if secrets_patch:
                config.update_secrets(secrets_patch)

            pipeline = self._pipelines.get(camera_id)
            if pipeline is not None:
                # Let a failure here propagate to the caller -- the config
                # on disk is already updated either way, and the existing
                # (still-running, pre-update) pipeline is left in place
                # rather than torn down over a bad settings change.
                pipeline.apply_settings(config.resolved())
            else:
                # Was errored / never started -- the config just changed,
                # so retry starting it now.
                self._start_worker(camera_id, config_path)
            return self._summarize(camera_id)

    def remove_camera(self, camera_id: str, delete_data: bool = False) -> None:
        with self._lock:
            pipeline = self._pipelines.pop(camera_id, None)
            config = self._configs.pop(camera_id, None)
            self._errors.pop(camera_id, None)

            if pipeline is not None:
                try:
                    pipeline.stop()
                except Exception:
                    logger.exception("Error stopping camera %r while removing it", camera_id)

            config_path = self._config_path(camera_id)
            if config is None and config_path.exists():
                try:
                    config = Config(config_path)
                except ConfigError:
                    config = None
            resolved = config.resolved() if config is not None else None

            for p in (
                config_path,
                self.cameras_dir / f"{camera_id}.secrets.yaml",
                self.cameras_dir / f"{camera_id}.mask.json",
            ):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    logger.exception("Failed to remove %s while deleting camera %r", p, camera_id)

            if delete_data and resolved is not None:
                for dir_path in (Path(resolved["recording"]["output_dir"]), Path(resolved["recording"]["cache_dir"])):
                    try:
                        shutil.rmtree(dir_path, ignore_errors=True)
                    except Exception:
                        logger.exception(
                            "Failed to remove data directory %s while deleting camera %r", dir_path, camera_id
                        )
