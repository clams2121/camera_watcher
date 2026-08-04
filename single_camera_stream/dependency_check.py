"""Verifies required third-party packages are installed before anything
else runs -- imported before any other module in this package so a
missing dependency produces a clear, actionable message instead of a raw
ImportError traceback.
"""
from __future__ import annotations

import importlib
import sys

REQUIRED_PACKAGES = [
    ("cv2", "opencv-python-headless"),
    ("numpy", "numpy"),
    ("yaml", "PyYAML"),
]


def check_dependencies(packages=REQUIRED_PACKAGES) -> None:
    missing = [(import_name, pip_name) for import_name, pip_name in packages if not _importable(import_name)]
    if not missing:
        return

    lines = ["", "single_camera_stream is missing required Python package(s):", ""]
    for import_name, pip_name in missing:
        lines.append(f"  - {pip_name}  (import name: {import_name})")
    lines += [
        "",
        "Install everything this tool needs with:",
        "",
        "    pip install -r single_camera_stream/requirements.txt",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)
    sys.exit(1)


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True
