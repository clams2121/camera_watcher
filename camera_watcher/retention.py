"""Disk retention for finalized clips.

Deliberately standalone (importable functions + CLI) rather than baked into
any one camera's process -- see deploy/camera-retention.service +
.timer, which invoke this on a schedule against the whole fleet's shared
data_root, replacing what used to be an in-process thread per camera. This
module intentionally has no third-party dependencies (unlike the rest of
the project, it needs no OpenCV/Flask/PyYAML) so it can run standalone
wherever Python 3 is available.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .constants import TEMP_SUFFIX

logger = logging.getLogger(__name__)


@dataclass
class RetentionConfig:
    output_dir: Path
    max_age_days: Optional[float] = None
    max_total_gb: Optional[float] = None


def classify_tier(metadata: dict) -> str:
    """Verdict-aware retention seam: always "unclassified" for now.

    Once a clip classifier exists (see the roadmap's tier-1 classifier),
    this is where its stored verdict would be read back out of a clip's
    metadata JSON, letting enforce_global_retention prioritize deleting
    low-value clips before high-value ones under storage pressure instead
    of pure oldest-first. A single constant return value today means
    _TIER_DELETE_PRIORITY has no actual effect yet -- every clip sorts
    equal on tier and falls back to age -- so wiring this in now doesn't
    change current behavior at all.
    """
    return "unclassified"


# Lower sorts first -- i.e. gets deleted first under a size budget.
_TIER_DELETE_PRIORITY = {"low_value": 0, "unclassified": 1, "high_value": 2}


def _finalized_clips(output_dir: Path) -> List[Path]:
    if not output_dir.exists():
        return []
    return [
        p
        for p in output_dir.iterdir()
        if p.is_file() and p.suffix == ".mp4" and not p.name.endswith(TEMP_SUFFIX)
    ]


def _discover_camera_dirs(clips_root: Path) -> List[Path]:
    if not clips_root.exists():
        return []
    return [p for p in clips_root.iterdir() if p.is_dir()]


def _read_metadata(clip: Path) -> dict:
    try:
        return json.loads(clip.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return {}


def _unlink_clip_and_metadata(clip: Path) -> None:
    """Removes a clip and its companion <clip stem>.json metadata file, if
    any. The metadata file is best-effort -- its absence/removal failure
    doesn't stop the clip itself from being removed."""
    clip.unlink()
    metadata_path = clip.with_suffix(".json")
    try:
        metadata_path.unlink(missing_ok=True)
    except OSError:
        logger.exception("Failed to remove metadata file %s", metadata_path)


def _sweep(clips: List[Path], max_age_days: Optional[float], max_total_gb: Optional[float], use_tiers: bool) -> List[Path]:
    """Shared age-then-size sweep logic, used by both the single-directory
    and whole-fleet entry points below. Never touches in-progress
    recordings -- their filenames carry a temp suffix and are excluded by
    the callers' _finalized_clips -- so this is safe to run concurrently
    with an active recorder."""
    removed: List[Path] = []

    if max_age_days:
        cutoff = time.time() - max_age_days * 86400
        for clip in clips:
            try:
                if clip.stat().st_mtime < cutoff:
                    _unlink_clip_and_metadata(clip)
                    removed.append(clip)
            except OSError:
                logger.exception("Failed to remove expired clip %s", clip)
    clips = [c for c in clips if c not in removed]

    if max_total_gb:
        max_bytes = max_total_gb * (1024**3)
        clips_with_stat: List[Tuple[Path, "os.stat_result"]] = []
        for clip in clips:
            try:
                clips_with_stat.append((clip, clip.stat()))
            except OSError:
                continue
        if use_tiers:
            clips_with_stat.sort(
                key=lambda cs: (_TIER_DELETE_PRIORITY.get(classify_tier(_read_metadata(cs[0])), 1), cs[1].st_mtime)
            )
        else:
            clips_with_stat.sort(key=lambda cs: cs[1].st_mtime)
        total = sum(st.st_size for _, st in clips_with_stat)
        i = 0
        while total > max_bytes and i < len(clips_with_stat):
            clip, st = clips_with_stat[i]
            try:
                _unlink_clip_and_metadata(clip)
                removed.append(clip)
                total -= st.st_size
            except OSError:
                logger.exception("Failed to remove clip %s over storage limit", clip)
            i += 1

    return removed


def enforce_retention(config: RetentionConfig) -> List[Path]:
    """Delete oldest finalized clips in a single directory exceeding
    age/size limits. Returns files removed."""
    removed = _sweep(_finalized_clips(config.output_dir), config.max_age_days, config.max_total_gb, use_tiers=False)
    if removed:
        logger.info("Retention removed %d clip(s)", len(removed))
    return removed


def enforce_global_retention(
    clips_root: Path, max_age_days: Optional[float] = None, max_total_gb: Optional[float] = None
) -> List[Path]:
    """Sweeps every camera's clips subdirectory under `clips_root` (i.e.
    `<data_root>/clips/<camera_name>/` for however many cameras share this
    data_root) against ONE shared budget. Age-based deletion is inherently
    per-clip and camera-agnostic; the size budget is enforced across the
    combined fleet -- lowest classify_tier priority, then oldest, first --
    not per-camera, so one busy camera can't starve a quiet one's clips out
    of a shared disk."""
    camera_dirs = _discover_camera_dirs(clips_root)
    all_clips: List[Path] = []
    for camera_dir in camera_dirs:
        all_clips.extend(_finalized_clips(camera_dir))

    removed = _sweep(all_clips, max_age_days, max_total_gb, use_tiers=True)
    if removed:
        logger.info("Global retention removed %d clip(s) across %d camera dir(s)", len(removed), len(camera_dirs))
    return removed


def _parse_args():
    parser = argparse.ArgumentParser(description="Enforce retention policy on saved motion clips.")
    parser.add_argument(
        "path",
        type=Path,
        help="A single camera's clips directory (default), or the fleet's shared "
        "<data_root>/clips directory when --global is given.",
    )
    parser.add_argument(
        "--global",
        dest="global_mode",
        action="store_true",
        help="Sweep every camera subdirectory under `path` against one shared budget, instead "
        "of treating `path` itself as one camera's clips directory.",
    )
    parser.add_argument("--max-age-days", type=float, default=None)
    parser.add_argument("--max-total-gb", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    if args.global_mode:
        removed = enforce_global_retention(args.path, max_age_days=args.max_age_days, max_total_gb=args.max_total_gb)
    else:
        removed = enforce_retention(
            RetentionConfig(output_dir=args.path, max_age_days=args.max_age_days, max_total_gb=args.max_total_gb)
        )
    for clip in removed:
        print(clip)


if __name__ == "__main__":
    main()
