"""Verifies required third-party packages are installed before anything else
runs -- same pattern as camera_watcher/dependency_check.py, and deliberately
has zero imports beyond the standard library for the same reason: a missing
dependency should produce a clear, actionable message instead of a raw
``ImportError`` traceback from deep inside the code.
"""
from __future__ import annotations

import importlib
import sys

# (import name, pip package name) -- pip name matches requirements-classifier.txt.
REQUIRED_PACKAGES = [
    ("cv2", "opencv-python-headless"),
    ("numpy", "numpy"),
    ("yaml", "PyYAML"),
    ("watchdog", "watchdog"),
    ("onnxruntime", "onnxruntime"),
]


def check_dependencies(packages=REQUIRED_PACKAGES) -> None:
    """Exit with a helpful message if any required package can't be imported."""
    missing = []
    for import_name, pip_name in packages:
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append((import_name, pip_name))

    if not missing:
        return

    lines = ["", "clip_classifier is missing required Python package(s):", ""]
    for import_name, pip_name in missing:
        lines.append(f"  - {pip_name}  (import name: {import_name})")
    lines += [
        "",
        "Install everything this needs with:",
        "",
        "    pip install -r requirements-classifier.txt",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)
    sys.exit(1)
