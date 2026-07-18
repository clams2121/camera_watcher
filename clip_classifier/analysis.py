"""Orchestrates sampling + detection + verdict for one clip, writing
<stem>.analysis.json -- the sole writer of that file (see watcher.py's
docstring for why its presence means "already classified").

Every clip that can't be processed for any reason still gets a sidecar --
verdict "error", with the reason naming what went wrong -- so nothing is
invisibly unclassified forever (the same file's absence is what makes a
clip look "still pending" to watcher.py, so silently skipping one would
mean it just spins forever on every backfill/rescan).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Tuple

from .detector import Detection, Detector
from .sampling import SamplingError, sample_frames
from .verdict import evaluate_verdict
from .watcher import analysis_path_for

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _read_metadata(clip_path: Path) -> dict:
    metadata_path = clip_path.with_suffix(".json")
    return json.loads(metadata_path.read_text())


def _write_analysis_sidecar(clip_path: Path, payload: dict) -> None:
    analysis_path = analysis_path_for(clip_path)
    tmp_path = analysis_path.with_name(analysis_path.stem + ".tmp.json")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(analysis_path)


def _base_payload(event_id: str, started: float) -> dict:
    return {
        "event_id": event_id,
        "schema_version": SCHEMA_VERSION,
        "processed_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "processing_seconds": round(time.monotonic() - started, 4),
    }


def _error_payload(event_id: str, reason: str, started: float) -> dict:
    payload = _base_payload(event_id, started)
    payload.update(
        {
            "verdict": "error",
            "labels": [],
            "reason": reason,
            "sampled_frame_offsets": [],
            "sampling_fallback": False,
            "backend": None,
            "model": None,
            "model_version": None,
        }
    )
    return payload


def analyze_clip(
    clip_path: Path,
    detector: Detector,
    max_frames: int,
    min_frame_spacing_seconds: float,
    thresholds: dict,
) -> dict:
    """Runs the full pipeline for one clip and always writes its analysis
    sidecar, even on failure (verdict="error"). Returns the written
    payload.

    Only raises for something genuinely unexpected (a bug here, not a bad
    clip) -- main.run() catches that case and logs it, and the clip stays
    "pending" (no sidecar written) so it gets retried on the next
    backfill/rescan rather than silently marked done."""
    started = time.monotonic()
    event_id = clip_path.stem

    try:
        metadata = _read_metadata(clip_path)
    except (OSError, ValueError) as e:
        logger.error("Failed to read metadata for %s: %s", clip_path, e)
        payload = _error_payload(event_id, f"failed to read metadata sidecar: {e}", started)
        _write_analysis_sidecar(clip_path, payload)
        return payload

    try:
        offsets, frames, used_fallback = sample_frames(clip_path, metadata, max_frames, min_frame_spacing_seconds)
    except SamplingError as e:
        logger.error("Sampling failed for %s: %s", clip_path, e)
        payload = _error_payload(event_id, f"sampling failed: {e}", started)
        _write_analysis_sidecar(clip_path, payload)
        return payload

    try:
        frame_detections: List[Tuple[float, List[Detection]]] = [
            (offset, detector.detect(frame)) for offset, frame in zip(offsets, frames)
        ]
    except Exception as e:
        logger.exception("Detector failed on %s", clip_path)
        payload = _error_payload(event_id, f"detector failed: {e}", started)
        _write_analysis_sidecar(clip_path, payload)
        return payload

    result = evaluate_verdict(frame_detections, metadata, thresholds)

    payload = _base_payload(event_id, started)
    payload.update(
        {
            "verdict": result.verdict,
            "labels": result.labels,
            "reason": result.reason,
            "sampled_frame_offsets": offsets,
            "sampling_fallback": used_fallback,
            "backend": detector.name,
            "model": detector.model,
            "model_version": detector.model_version,
        }
    )
    _write_analysis_sidecar(clip_path, payload)
    logger.info("Classified %s: %s (%s)", clip_path.name, result.verdict, result.reason)
    return payload


def build_process_fn(settings: dict) -> Callable[[Path], None]:
    """Builds the process_fn main.run() needs, with the detector
    constructed once up front and reused across every clip -- constructing
    it per-clip would reload the model file / reinitialize the Hailo
    device on every single event, for no benefit."""
    from .backend import build_detector

    detector = build_detector(settings)
    thresholds = settings["thresholds"]
    max_frames = settings["sampling"]["max_frames"]
    min_spacing = settings["sampling"]["min_frame_spacing_seconds"]

    def process_fn(clip_path: Path) -> None:
        analyze_clip(clip_path, detector, max_frames, min_spacing, thresholds)

    return process_fn
