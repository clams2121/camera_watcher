"""Work loop plumbing for the classifier service.

``run()`` is the generic serial loop -- one clip at a time, oldest first,
timed and logged, a single clip's failure never taking the whole service
down with it. It's parameterized by ``process_fn`` so it's fully testable
on its own; the real CLI entrypoint (wiring in the actual detector stack
via ``analysis.process_clip``) lives here too but is added once that
module exists.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from threading import Event
from typing import Callable

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
