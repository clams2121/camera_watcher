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
import os
import signal
import sys

from .config import DEFAULT_SECRETS_PATH, DEFAULT_SETTINGS_PATH, Config
from .pipeline import CameraPipeline
from .web import create_app


def _parse_args():
    parser = argparse.ArgumentParser(description="Watch an RTSP camera and record motion clips.")
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS_PATH), help="Path to settings.yaml")
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS_PATH), help="Path to secrets.yaml")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()

    config = Config(settings_path=args.settings, secrets_path=args.secrets)
    pipeline = CameraPipeline(config)
    pipeline.start()

    def _shutdown(signum, frame):
        logging.getLogger(__name__).info("Shutting down (signal %s)...", signum)
        pipeline.stop()
        raise SystemExit(0)

    def _restart(signum, frame):
        # Re-exec explicitly via `-m camera_watcher.main` (rather than
        # forwarding sys.argv as-is) so relative imports still work
        # afterwards regardless of how this process was originally launched.
        logging.getLogger(__name__).info("Restarting to pick up updated code...")
        pipeline.stop()
        python = sys.executable
        module_args = ["-m", "camera_watcher.main", "--settings", args.settings, "--secrets", args.secrets]
        try:
            os.execv(python, [python] + module_args)
        except OSError:
            logging.getLogger(__name__).exception("Restart failed; exiting instead")
            raise SystemExit(1)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    if hasattr(signal, "SIGUSR1"):  # not available on Windows
        signal.signal(signal.SIGUSR1, _restart)

    app = create_app(config, pipeline)
    web_cfg = config.settings["web"]
    try:
        app.run(host=web_cfg["host"], port=web_cfg["port"], threaded=True)
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
