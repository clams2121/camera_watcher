"""Entrypoint: the always-on fleet supervisor.

Starts the web UI first and foremost -- it always comes up, regardless of
how many cameras are configured or how badly any one of them is broken --
then every configured camera's capture/motion/recording pipeline (each
isolated from the others' and the UI's failures via fleet.py's
CameraManager) and the in-process retention scheduler. There is exactly one
process, one bind, one auth token for the whole fleet; cameras themselves
are added, edited, and removed entirely through the UI once it's up.
"""
from __future__ import annotations

# Checked before any of this project's own modules are imported, since those
# transitively import the third-party packages being checked here -- this
# way a missing dependency produces a clear message instead of a raw
# ImportError traceback.
from .dependency_check import check_dependencies

check_dependencies()

import argparse
import logging
import signal
import socket
import sys
from pathlib import Path

import waitress

from .auth import AuthConfigError, require_token
from .config import ConfigError
from .fleet import CameraManager, FleetConfig
from .retention import RetentionScheduler
from .tailscale import TailscaleError, resolve_tailscale_ip
from .web import create_app


def _parse_args():
    parser = argparse.ArgumentParser(description="Run the camera_watcher fleet supervisor + web UI.")
    parser.add_argument(
        "--config-dir",
        required=True,
        help="Path to the fleet's config directory. Created automatically on first run, along with "
        "fleet.yaml (default settings) and fleet.secrets.yaml (a freshly generated auth token) if "
        "they don't exist yet -- no manual setup is required before the UI comes up. Individual "
        "cameras live under <config-dir>/cameras/, managed entirely through the UI. All relative "
        "paths inside the fleet's config resolve against this directory, never the current working "
        "directory.",
    )
    return parser.parse_args()


def _check_port_available(host: str, port: int) -> None:
    """Fails loud, with a clear message, if `port` is already bound on
    `host` -- rather than letting the web server die moments later with a
    lower-level, more cryptic bind error. This doesn't close the race
    against something else grabbing the port between this check and the
    real bind, but that's an acceptable window for a single supervisor
    process per host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        raise ConfigError(
            f"Cannot bind web.host={host!r} web.port={port} -- {e}.\n"
            f"Something else is already using this port. Change web.port in config/fleet.yaml "
            f"(or stop whatever else is bound there), then restart."
        ) from e
    finally:
        sock.close()


def _fail(message: str) -> None:
    print(f"camera_watcher: {message}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_host(configured_host: str) -> str:
    """"tailscale" resolves this host's Tailscale IPv4 address and binds
    only there; anything else is used as a literal host/IP. Never falls
    back to 0.0.0.0 on failure -- fails loud instead. Unlike a broken
    camera, a bind failure here really does mean there's no UI at all to
    fall back on, so this is the one thing in this file still allowed to
    stop the whole process at startup."""
    if configured_host != "tailscale":
        return configured_host
    try:
        return resolve_tailscale_ip()
    except TailscaleError as e:
        raise ConfigError(str(e)) from e


def _log_bootstrapped_token(logger: logging.Logger, fleet_config: FleetConfig) -> None:
    if not fleet_config.bootstrapped_token:
        return
    bar = "=" * 70
    logger.warning(
        "\n%s\n"
        "No auth token was configured -- generated a new one.\n"
        "Log in to the web UI with this token (also saved in %s):\n\n"
        "    %s\n\n"
        "Change it any time from Fleet Settings once logged in.\n%s",
        bar,
        fleet_config.secrets_path,
        fleet_config.bootstrapped_token,
        bar,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger(__name__)
    args = _parse_args()

    fleet_config = FleetConfig(Path(args.config_dir))
    _log_bootstrapped_token(logger, fleet_config)

    try:
        auth_token = require_token(fleet_config.secrets)
    except AuthConfigError as e:
        _fail(str(e))
        return  # unreachable; keeps type checkers happy about `auth_token` below

    web_cfg = fleet_config.settings["web"]
    try:
        host = _resolve_host(web_cfg["host"])
        _check_port_available(host, web_cfg["port"])
    except ConfigError as e:
        _fail(str(e))
        return

    # Discovers and starts every camera under config/cameras/ -- any single
    # camera that fails to load or connect is recorded against just that
    # camera_id (see fleet.py), never raised here. The UI below comes up
    # regardless of how this goes.
    camera_manager = CameraManager(fleet_config)
    camera_manager.start()

    retention_scheduler = RetentionScheduler(
        clips_root_provider=fleet_config.resolved_clips_root,
        settings_provider=lambda: fleet_config.settings["retention"],
    )
    retention_scheduler.start()

    def _shutdown(signum, frame):
        logger.info("Shutting down (signal %s)...", signum)
        retention_scheduler.stop()
        camera_manager.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    app = create_app(fleet_config, camera_manager, retention_scheduler, auth_token)
    logger.info("Serving on %s:%s", host, web_cfg["port"])
    try:
        waitress.serve(app, host=host, port=web_cfg["port"], threads=8)
    finally:
        retention_scheduler.stop()
        camera_manager.stop()


if __name__ == "__main__":
    main()
