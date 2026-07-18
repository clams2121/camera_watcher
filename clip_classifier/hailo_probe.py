"""Detects whether this host can actually run the Hailo-8L backend --
device node present, HailoRT's Python bindings importable, and the HEF
model file present. Kept separate from detectors/hailo.py (which does the
real inference) and importable with zero optional dependencies, so
backend.py can always probe safely regardless of whether hailo_platform is
installed.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEVICE_PATH = Path("/dev/hailo0")
HAILORT_MODULE_NAME = "hailo_platform"


@dataclass(frozen=True)
class HailoProbeResult:
    device_present: bool
    runtime_importable: bool
    hef_present: bool

    @property
    def fully_available(self) -> bool:
        return self.device_present and self.runtime_importable and self.hef_present

    def missing_summary(self) -> str:
        """Plain-English list of exactly what's missing -- used both by the
        loud auto-fallback warning and by the hard failure in explicit
        `backend: hailo` mode."""
        missing = []
        if not self.device_present:
            missing.append(f"the Hailo-8L device node ({DEFAULT_DEVICE_PATH}) -- is the accelerator installed?")
        if not self.runtime_importable:
            missing.append(
                f"the HailoRT Python bindings (`{HAILORT_MODULE_NAME}`) -- install HailoRT "
                "(https://hailo.ai/developer-zone/) for this Python environment"
            )
        if not self.hef_present:
            missing.append(
                "the compiled YOLOv8-family HEF file (classifier.yaml's hailo.hef_path) -- see the "
                "README's 'Hailo backend' section for where to get one from the Hailo Model Zoo"
            )
        return "; ".join(missing)


def probe_hailo(hef_path: Path, device_path: Path = DEFAULT_DEVICE_PATH) -> HailoProbeResult:
    return HailoProbeResult(
        device_present=device_path.exists(),
        runtime_importable=importlib.util.find_spec(HAILORT_MODULE_NAME) is not None,
        hef_present=hef_path.is_file(),
    )
