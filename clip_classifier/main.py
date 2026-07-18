"""Entrypoint and work loop plumbing for the classifier service.

``run()`` is the generic serial loop -- one clip at a time, oldest first,
timed and logged, a single clip's failure never taking the whole service
down with it. It's parameterized by ``process_fn`` so it's fully testable
on its own, independent of the actual detector stack.
"""
from __future__ import annotations

# Checked before any of this project's own modules are imported -- see
# camera_watcher/main.py for why (a missing dependency should produce a
# clear message, not a raw ImportError traceback from deep inside the code).
from .dependency_check import check_dependencies

check_dependencies()

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from threading import Event
from typing import Callable

from .analysis import build_process_fn
from .backend import BackendError
from .config import Config, ConfigError
from .watcher import ClipWatcher

logger = logging.getLogger(__name__)


def _parse_args():
    parser = argparse.ArgumentParser(description="Classify camera_watcher clips (tier-1 detector + verdict).")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to this classifier's config YAML (e.g. config/classifier.yaml). All relative "
        "paths inside it resolve against its own directory, never the current working directory.",
    )
    return parser.parse_args()


def _fail(message: str) -> None:
    print(f"clip_classifier: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(watcher, process_fn: Callable[[Path], None], stop_event: Event) -> None:
    """Pulls clips off `watcher` one at a time until `stop_event` is set.

    `process_fn` is responsible for writing its own "error" verdict sidecar
    on a failure it understands (see analysis.py) -- if it raises anyway,
    that's an unexpected failure, logged loudly here rather than crashing
    the service over one bad clip. Either way, processing moves on to the
    next clip; nothing here retries automatically (a clip that keeps
    failing will keep showing up as "pending" -- see watcher.py -- and get
    tried again on the next backfill/rescan pass, not hammered in a tight
    loop).
    """
    while not stop_event.is_set():
        clip_path = watcher.get(timeout=0.5)
        if clip_path is None:
            continue

        started = time.monotonic()
        try:
            process_fn(clip_path)
        except Exception:
            logger.exception("Unhandled error processing %s -- continuing with the next clip", clip_path)
        elapsed = time.monotonic() - started
        logger.info("Processed %s in %.2fs", clip_path.name, elapsed)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()

    try:
        config = Config(args.config)
    except ConfigError as e:
        _fail(str(e))
        return  # unreachable; keeps type checkers happy about `config` below

    settings = config.resolved()

    try:
        process_fn = build_process_fn(settings)
    except BackendError as e:
        _fail(str(e))
        return

    watcher = ClipWatcher(
        data_root=Path(settings["data_root"]),
        queue_maxsize=settings["watch"]["queue_maxsize"],
        rescan_interval_seconds=settings["watch"]["rescan_interval_seconds"],
    )
    watcher.start()

    stop_event = Event()

    def _shutdown(signum, frame):
        logger.info("Shutting down (signal %s)...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        run(watcher, process_fn, stop_event)
    finally:
        watcher.stop()


if __name__ == "__main__":
    main()
