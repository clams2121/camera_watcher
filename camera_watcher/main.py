"""Entrypoint: start the capture/motion/recording pipeline and the web UI together."""
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

import waitress

from .auth import AuthConfigError, require_token
from .config import Config, ConfigError
from .pipeline import CameraPipeline
from .tailscale import TailscaleError, resolve_tailscale_ip
from .web import create_app


def _parse_args():
    parser = argparse.ArgumentParser(description="Watch an RTSP camera and record motion clips.")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to this camera's config YAML (e.g. config/front-door.yaml). All relative "
        "paths inside it resolve against its own directory, never the current working directory.",
    )
    return parser.parse_args()


def _check_port_available(host: str, port: int) -> None:
    """Fails loud, with a clear message, if `port` is already bound on
    `host` -- rather than letting the web server die moments later with a
    lower-level, more cryptic bind error. This doesn't close the race
    against something else grabbing the port between this check and the
    real bind, but that's an acceptable window for a small, manually
    managed fleet of camera processes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        raise ConfigError(
            f"Cannot bind web.host={host!r} web.port={port} -- {e}.\n"
            f"Another camera_watcher instance (or something else) is probably already using this "
            f"port. Each camera's config needs its own web.port."
        ) from e
    finally:
        sock.close()


def _fail(message: str) -> None:
    print(f"camera_watcher: {message}", file=sys.stderr)
    raise SystemExit(1)


def _resolve_host(configured_host: str) -> str:
    """"tailscale" resolves this host's Tailscale IPv4 address and binds
    only there; anything else is used as a literal host/IP. Never falls
    back to 0.0.0.0 on failure -- fails loud instead."""
    if configured_host != "tailscale":
        return configured_host
    try:
        return resolve_tailscale_ip()
    except TailscaleError as e:
        raise ConfigError(str(e)) from e


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger(__name__)
    args = _parse_args()

    try:
        config = Config(args.config)
    except ConfigError as e:
        _fail(str(e))
        return  # unreachable; keeps type checkers happy about `config` below

    try:
        auth_token = require_token(config.secrets)
    except AuthConfigError as e:
        _fail(str(e))
        return

    web_cfg = config.settings["web"]
    try:
        host = _resolve_host(web_cfg["host"])
        _check_port_available(host, web_cfg["port"])
    except ConfigError as e:
        _fail(str(e))
        return

    pipeline = CameraPipeline(config)
    pipeline.start()

    def _shutdown(signum, frame):
        logger.info("Shutting down (signal %s)...", signum)
        pipeline.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    app = create_app(config, pipeline, auth_token)
    logger.info("Serving on %s:%s", host, web_cfg["port"])
    try:
        waitress.serve(app, host=host, port=web_cfg["port"], threads=8)
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
