"""Disk retention for finalized clips.

The importable functions here (``enforce_retention``, ``enforce_global_
retention``, ``classify_tier``) have no third-party dependencies (unlike
most of the project, this needs no OpenCV/Flask/PyYAML) so they -- and the
``python -m camera_watcher.retention`` CLI below -- can run standalone
wherever Python 3 is available, e.g. for manual/diagnostic sweeps.
``RetentionScheduler`` at the bottom is the fleet supervisor's in-process
equivalent of what used to be an external systemd timer: a background
thread that calls the same functions on a schedule, reading settings live
from FleetConfig each cycle so UI edits apply on the next run without a
restart.

Verdict-aware since clip_classifier exists: each clip is sorted into a
retention tier (classify_tier below) by reading its <stem>.analysis.json /
<stem>.review.json sidecars -- this module only ever reads them, never
writes them (clip_classifier and camera_watcher.web.routes are the sole
writers respectively).
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .constants import TEMP_SUFFIX

logger = logging.getLogger(__name__)

# Default per-tier age windows -- all independently configurable, see
# RetentionConfig / enforce_global_retention / the CLI flags below.
DEFAULT_LOW_MAX_AGE_HOURS = 48.0
DEFAULT_HIGH_MAX_AGE_DAYS = 30.0
DEFAULT_REVIEW_MAX_AGE_DAYS = 30.0

_SIDECAR_SUFFIXES = (".json", ".analysis.json", ".review.json")


@dataclass
class RetentionConfig:
    output_dir: Path
    low_max_age_hours: Optional[float] = DEFAULT_LOW_MAX_AGE_HOURS
    high_max_age_days: Optional[float] = DEFAULT_HIGH_MAX_AGE_DAYS
    review_max_age_days: Optional[float] = DEFAULT_REVIEW_MAX_AGE_DAYS
    max_total_gb: Optional[float] = None
    # When true, nothing is actually deleted -- everything that would have
    # been removed (and why) is still computed and logged/returned, just
    # with the unlink calls skipped. See _expire_by_age/_enforce_budget.
    dry_run: bool = False


def _sidecar_path(clip: Path, suffix: str) -> Path:
    return clip.parent / f"{clip.stem}{suffix}"


def _read_json_sidecar(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("Failed to read %s -- treating it as absent", path)
        return None


def classify_tier(clip: Path) -> str:
    """Sorts a clip into a retention tier by reading its classifier verdict
    and (if any) human review decision -- "low", "review", or "high".

    - No `<stem>.analysis.json` yet, or verdict == "error": "high". Fail
      loud in the sense that matters here -- a clip clip_classifier hasn't
      reached (or choked on) is never quietly treated as disposable, it's
      kept at the same priority as a confirmed detection until proven
      otherwise.
    - verdict == "low": "low".
    - verdict == "high": "high".
    - verdict == "review": "review", UNLESS a `<stem>.review.json` sidecar
      records decision == "keep", in which case a human already vouched
      for it and it's promoted to "high". (decision == "discard" isn't
      handled specially here since the web layer deletes the clip
      immediately on discard -- there's normally nothing left to classify.)
    """
    analysis = _read_json_sidecar(_sidecar_path(clip, ".analysis.json"))
    verdict = analysis.get("verdict") if analysis else None

    if verdict == "low":
        return "low"
    if verdict == "review":
        review = _read_json_sidecar(_sidecar_path(clip, ".review.json"))
        if review and review.get("decision") == "keep":
            return "high"
        return "review"
    # verdict == "high", verdict == "error", or no analysis sidecar at all.
    return "high"


# Lower sorts first -- i.e. gets deleted first under a size budget. "review"
# and "high" share a priority: once every "low" clip is gone, budget
# pressure deletes the oldest of whichever's left, high-value or not (see
# _enforce_budget's logging when that happens).
_TIER_DELETE_PRIORITY = {"low": 0, "review": 1, "high": 1}


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


def _unlink_clip_and_sidecars(clip: Path) -> None:
    """Removes a clip and its whole sidecar family: the recorder's own
    <stem>.json, clip_classifier's <stem>.analysis.json, and any human
    <stem>.review.json -- whichever of those exist. Each sidecar is
    best-effort: a missing or unremovable one never stops the others (or
    the clip itself) from being removed."""
    clip.unlink()
    for suffix in _SIDECAR_SUFFIXES:
        sidecar_path = _sidecar_path(clip, suffix)
        try:
            sidecar_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove sidecar file %s", sidecar_path)


def _tier_max_age_seconds(tier: str, cfg: RetentionConfig) -> Optional[float]:
    if tier == "low":
        return cfg.low_max_age_hours * 3600 if cfg.low_max_age_hours else None
    if tier == "review":
        return cfg.review_max_age_days * 86400 if cfg.review_max_age_days else None
    return cfg.high_max_age_days * 86400 if cfg.high_max_age_days else None


def _expire_by_age(clips: List[Path], cfg: RetentionConfig) -> List[Path]:
    """Unconditionally removes clips past their own tier's age window --
    runs before the size-budget phase, so "expired anything" always goes
    first regardless of how much headroom is left in the budget. Under
    dry_run, everything that would have been removed is still computed
    and logged, just never actually unlinked."""
    verb = "Would remove" if cfg.dry_run else "Removing"
    removed: List[Path] = []
    now = time.time()
    for clip in clips:
        tier = classify_tier(clip)
        max_age_seconds = _tier_max_age_seconds(tier, cfg)
        if max_age_seconds is None:
            continue
        try:
            age_seconds = now - clip.stat().st_mtime
        except OSError:
            continue
        if age_seconds < max_age_seconds:
            continue
        if tier == "review":
            logger.warning(
                "%s a clip flagged for human review that was never reviewed -- aged past "
                "the %.1f-day review window: %s",
                verb,
                cfg.review_max_age_days,
                clip,
            )
        if cfg.dry_run:
            removed.append(clip)
            continue
        try:
            _unlink_clip_and_sidecars(clip)
            removed.append(clip)
        except OSError:
            logger.exception("Failed to remove expired clip %s", clip)
    return removed


def _enforce_budget(clips: List[Path], max_total_gb: Optional[float], dry_run: bool) -> List[Path]:
    """Deletes clips oldest-first within tier priority (low, then
    review/high together) until the combined size of what's left is back
    under budget. Logs a warning each time budget pressure -- not age or a
    human decision -- is what took down a "high" or "review" tier clip.
    Under dry_run, sizes are still tallied against the budget so the
    dry-run output reflects real deletion order, just without unlinking."""
    if not max_total_gb:
        return []
    verb = "Would forcibly delete" if dry_run else "Budget pressure is forcing deletion of"
    max_bytes = max_total_gb * (1024**3)
    clips_with_stat: List[Tuple[Path, "os.stat_result", str]] = []
    for clip in clips:
        try:
            clips_with_stat.append((clip, clip.stat(), classify_tier(clip)))
        except OSError:
            continue
    clips_with_stat.sort(key=lambda cst: (_TIER_DELETE_PRIORITY.get(cst[2], 1), cst[1].st_mtime))

    total = sum(st.st_size for _, st, _ in clips_with_stat)
    removed: List[Path] = []
    i = 0
    while total > max_bytes and i < len(clips_with_stat):
        clip, st, tier = clips_with_stat[i]
        if tier != "low":
            logger.warning("%s a %s-tier clip: %s", verb, tier, clip)
        if dry_run:
            removed.append(clip)
            total -= st.st_size
            i += 1
            continue
        try:
            _unlink_clip_and_sidecars(clip)
            removed.append(clip)
            total -= st.st_size
        except OSError:
            logger.exception("Failed to remove clip %s over storage limit", clip)
        i += 1
    return removed


def _sweep(clips: List[Path], cfg: RetentionConfig) -> List[Path]:
    """Shared age-then-size sweep logic, used by both the single-directory
    and whole-fleet entry points below. Never touches in-progress
    recordings -- their filenames carry a temp suffix and are excluded by
    the callers' _finalized_clips -- so this is safe to run concurrently
    with an active recorder."""
    removed = _expire_by_age(clips, cfg)
    remaining = [c for c in clips if c not in removed]
    removed += _enforce_budget(remaining, cfg.max_total_gb, cfg.dry_run)
    return removed


def enforce_retention(config: RetentionConfig) -> List[Path]:
    """Delete clips in a single directory past their tier's age window or
    (once still over max_total_gb) oldest-within-tier-priority. Returns
    files removed (or, under config.dry_run, files that WOULD have been
    removed -- nothing is actually touched)."""
    removed = _sweep(_finalized_clips(config.output_dir), config)
    if removed:
        verb = "would remove" if config.dry_run else "removed"
        logger.info("Retention %s %d clip(s)", verb, len(removed))
    return removed


def enforce_global_retention(
    clips_root: Path,
    low_max_age_hours: Optional[float] = DEFAULT_LOW_MAX_AGE_HOURS,
    high_max_age_days: Optional[float] = DEFAULT_HIGH_MAX_AGE_DAYS,
    review_max_age_days: Optional[float] = DEFAULT_REVIEW_MAX_AGE_DAYS,
    max_total_gb: Optional[float] = None,
    dry_run: bool = False,
) -> List[Path]:
    """Sweeps every camera's clips subdirectory under `clips_root` (i.e.
    `<data_root>/clips/<camera_name>/` for however many cameras share this
    data_root) against ONE shared budget. Age-based expiry is inherently
    per-clip and camera-agnostic; the size budget is enforced across the
    combined fleet -- lowest tier priority, then oldest, first -- not
    per-camera, so one busy camera can't starve a quiet one's clips out of
    a shared disk. Under dry_run, nothing is actually deleted -- see
    RetentionConfig.dry_run."""
    camera_dirs = _discover_camera_dirs(clips_root)
    all_clips: List[Path] = []
    for camera_dir in camera_dirs:
        all_clips.extend(_finalized_clips(camera_dir))

    cfg = RetentionConfig(
        output_dir=clips_root,
        low_max_age_hours=low_max_age_hours,
        high_max_age_days=high_max_age_days,
        review_max_age_days=review_max_age_days,
        max_total_gb=max_total_gb,
        dry_run=dry_run,
    )
    removed = _sweep(all_clips, cfg)
    if removed:
        verb = "would remove" if dry_run else "removed"
        logger.info("Global retention %s %d clip(s) across %d camera dir(s)", verb, len(removed), len(camera_dirs))
    return removed


class RetentionScheduler:
    """Background thread run inside the fleet supervisor process, calling
    enforce_global_retention on a schedule -- the in-process replacement
    for what used to be an external camera-retention.service/.timer pair.

    `clips_root_provider`/`settings_provider` are callables (not static
    values) so every cycle -- and every `run_once()` call, e.g. from a
    "run now" button in the UI -- picks up whatever's currently saved in
    FleetConfig, including live edits made since the thread started.
    `settings_provider` must return a dict shaped like FleetConfig's
    `settings["retention"]` (low_max_age_hours, high_max_age_days,
    review_max_age_days, max_total_gb, interval_minutes).
    """

    def __init__(self, clips_root_provider: Callable[[], Path], settings_provider: Callable[[], dict]):
        self._clips_root_provider = clips_root_provider
        self._settings_provider = settings_provider
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # {"at": epoch seconds, "dry_run": bool, "removed": [str, ...]} for
        # the last completed sweep -- read by the UI's fleet status view.
        self.last_run: Optional[dict] = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="retention-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def run_once(self, dry_run: bool = False) -> List[Path]:
        """Runs a single sweep immediately with the current settings.
        Safe to call from a request thread (e.g. a UI "run retention now"
        button) concurrently with the scheduled loop -- enforce_global_
        retention has no shared mutable state beyond the filesystem itself."""
        settings = self._settings_provider()
        removed = enforce_global_retention(
            self._clips_root_provider(),
            low_max_age_hours=settings.get("low_max_age_hours"),
            high_max_age_days=settings.get("high_max_age_days"),
            review_max_age_days=settings.get("review_max_age_days"),
            max_total_gb=settings.get("max_total_gb"),
            dry_run=dry_run,
        )
        self.last_run = {"at": time.time(), "dry_run": dry_run, "removed": [str(p) for p in removed]}
        return removed

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once(dry_run=False)
            except Exception:
                logger.exception("Scheduled retention sweep failed")
            interval_minutes = self._settings_provider().get("interval_minutes") or 60.0
            if self._stop_event.wait(max(float(interval_minutes), 1.0) * 60):
                break


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
    parser.add_argument(
        "--low-max-age-hours",
        type=float,
        default=DEFAULT_LOW_MAX_AGE_HOURS,
        help=f"Max age for verdict=low clips, in hours (default {DEFAULT_LOW_MAX_AGE_HOURS}). 0 disables.",
    )
    parser.add_argument(
        "--high-max-age-days",
        type=float,
        default=DEFAULT_HIGH_MAX_AGE_DAYS,
        help="Max age for verdict=high clips (and error/not-yet-classified/review+keep, which are "
        f"treated as high), in days (default {DEFAULT_HIGH_MAX_AGE_DAYS}). 0 disables.",
    )
    parser.add_argument(
        "--review-max-age-days",
        type=float,
        default=DEFAULT_REVIEW_MAX_AGE_DAYS,
        help="Max age for verdict=review clips that were never reviewed, in days "
        f"(default {DEFAULT_REVIEW_MAX_AGE_DAYS}). 0 disables.",
    )
    parser.add_argument("--max-total-gb", type=float, default=None, help="Shared size budget in GB. Omit for none.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print/log what would be removed (and why) without deleting anything.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    if args.global_mode:
        removed = enforce_global_retention(
            args.path,
            low_max_age_hours=args.low_max_age_hours,
            high_max_age_days=args.high_max_age_days,
            review_max_age_days=args.review_max_age_days,
            max_total_gb=args.max_total_gb,
            dry_run=args.dry_run,
        )
    else:
        removed = enforce_retention(
            RetentionConfig(
                output_dir=args.path,
                low_max_age_hours=args.low_max_age_hours,
                high_max_age_days=args.high_max_age_days,
                review_max_age_days=args.review_max_age_days,
                max_total_gb=args.max_total_gb,
                dry_run=args.dry_run,
            )
        )
    prefix = "[dry-run] would remove: " if args.dry_run else ""
    for clip in removed:
        print(f"{prefix}{clip}")


if __name__ == "__main__":
    main()
