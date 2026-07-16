"""Disk retention for finalized clips.

Deliberately standalone (importable function + CLI) rather than baked only
into the pipeline, so a future external process -- e.g. a cleanup service
managing several camera_watcher instances -- can invoke the same policy
directly against a clips directory. This module intentionally has no
third-party dependencies (unlike the rest of the project, it needs no
OpenCV/Flask/PyYAML) so it can run standalone wherever Python 3 is
available.
"""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .constants import TEMP_SUFFIX

logger = logging.getLogger(__name__)


@dataclass
class RetentionConfig:
    output_dir: Path
    max_age_days: Optional[float] = None
    max_total_gb: Optional[float] = None


def _finalized_clips(output_dir: Path) -> list:
    if not output_dir.exists():
        return []
    return [
        p
        for p in output_dir.iterdir()
        if p.is_file() and p.suffix == ".mp4" and not p.name.endswith(TEMP_SUFFIX)
    ]


def enforce_retention(config: RetentionConfig) -> list:
    """Delete oldest finalized clips exceeding age/size limits. Returns files removed.

    Never touches in-progress recordings -- their filenames carry a temp
    suffix and are excluded -- so this is safe to run concurrently with an
    active recorder.
    """
    clips = _finalized_clips(config.output_dir)
    removed = []

    if config.max_age_days:
        cutoff = time.time() - config.max_age_days * 86400
        for clip in clips:
            try:
                if clip.stat().st_mtime < cutoff:
                    clip.unlink()
                    removed.append(clip)
            except OSError:
                logger.exception("Failed to remove expired clip %s", clip)
    clips = [c for c in clips if c not in removed]

    if config.max_total_gb:
        max_bytes = config.max_total_gb * (1024**3)
        clips_with_stat = []
        for clip in clips:
            try:
                clips_with_stat.append((clip, clip.stat()))
            except OSError:
                continue
        clips_with_stat.sort(key=lambda cs: cs[1].st_mtime)
        total = sum(st.st_size for _, st in clips_with_stat)
        i = 0
        while total > max_bytes and i < len(clips_with_stat):
            clip, st = clips_with_stat[i]
            try:
                clip.unlink()
                removed.append(clip)
                total -= st.st_size
            except OSError:
                logger.exception("Failed to remove clip %s over storage limit", clip)
            i += 1

    if removed:
        logger.info("Retention removed %d clip(s)", len(removed))
    return removed


def _parse_args():
    parser = argparse.ArgumentParser(description="Enforce retention policy on saved motion clips.")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--max-age-days", type=float, default=None)
    parser.add_argument("--max-total-gb", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    config = RetentionConfig(
        output_dir=args.output_dir, max_age_days=args.max_age_days, max_total_gb=args.max_total_gb
    )
    for clip in enforce_retention(config):
        print(clip)


if __name__ == "__main__":
    main()
