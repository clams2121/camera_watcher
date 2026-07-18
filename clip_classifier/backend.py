"""Selects and constructs the detector backend per classifier.yaml's
``backend`` setting (auto/cpu/hailo) -- see hailo_probe.py for the
device/runtime/HEF presence check this decision is built on.

- ``cpu``: always CpuYolov8Detector, never even probes for Hailo.
- ``hailo``: requires the device, HailoRT bindings, and HEF file all
  present -- fails loud (BackendError) listing exactly what's missing if
  not. No fallback in this mode; the caller explicitly asked for Hailo.
- ``auto`` (default): uses Hailo if fully available, otherwise falls back
  to CPU -- the one sanctioned fallback in this codebase's "fail loud, no
  silent fallbacks" rule, and it's loud about it: a prominent warning
  naming exactly what's missing, plus ``status()`` reflecting the
  fallback so it's visible outside the logs too (see main.py's status
  output).
"""
from __future__ import annotations

import logging
from pathlib import Path

from .detector import Detector
from .hailo_probe import probe_hailo

logger = logging.getLogger(__name__)


class BackendError(Exception):
    """Raised when the requested backend can't be constructed at all."""


def build_detector(settings: dict) -> Detector:
    backend = settings["backend"]
    cpu_model_path = Path(settings["cpu"]["model_path"])
    hef_path = Path(settings["hailo"]["hef_path"])

    if backend == "cpu":
        return _build_cpu(cpu_model_path)

    if backend == "hailo":
        probe = probe_hailo(hef_path)
        if not probe.fully_available:
            raise BackendError(
                f"backend: hailo was explicitly requested, but it isn't usable: {probe.missing_summary()}. "
                f"No fallback in this mode -- set backend: auto if you want CPU fallback, or backend: cpu "
                f"to skip Hailo entirely."
            )
        return _build_hailo(hef_path)

    if backend == "auto":
        probe = probe_hailo(hef_path)
        if probe.fully_available:
            logger.info("Hailo-8L detected and available -- using the hailo backend.")
            return _build_hailo(hef_path)
        logger.warning(
            "backend: auto -- Hailo-8L is not fully available (%s); falling back to the CPU backend. "
            "This is expected on a host with no Hailo accelerator; if you expected Hailo to be used here, "
            "see the message above for exactly what's missing.",
            probe.missing_summary(),
        )
        return _build_cpu(cpu_model_path)

    raise BackendError(f"Unknown backend: {backend!r} (expected auto, cpu, or hailo)")


def _build_cpu(model_path: Path) -> Detector:
    from .detectors.cpu import CpuYolov8Detector, ModelLoadError

    try:
        return CpuYolov8Detector(model_path)
    except ModelLoadError as e:
        raise BackendError(str(e)) from e


def _build_hailo(hef_path: Path) -> Detector:
    from .detectors.hailo import HailoModelLoadError, HailoYolov8Detector

    try:
        return HailoYolov8Detector(hef_path)
    except HailoModelLoadError as e:
        raise BackendError(str(e)) from e
