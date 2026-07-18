"""Verifies required third-party packages are installed before anything else runs.

Deliberately has zero imports beyond the standard library, and is imported
before any other ``camera_watcher`` module in every entrypoint, so a missing
dependency produces a clear, actionable message instead of a raw
``ImportError`` traceback from deep inside the code.
"""
from __future__ import annotations

import importlib
import sys

# (import name, pip package name) -- pip name matches requirements.txt.
REQUIRED_PACKAGES = [
    ("cv2", "opencv-python-headless"),
    ("numpy", "numpy"),
    ("flask", "Flask"),
    ("yaml", "PyYAML"),
    ("waitress", "waitress"),
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

    lines = ["", "camera_watcher is missing required Python package(s):", ""]
    for import_name, pip_name in missing:
        lines.append(f"  - {pip_name}  (import name: {import_name})")
    lines += [
        "",
        "Install everything this project needs with:",
        "",
        "    pip install -r requirements.txt",
        "",
        "If you haven't created a virtual environment yet:",
        "",
        "    python3 -m venv .venv",
        "    source .venv/bin/activate    # on Windows: .venv\\Scripts\\activate",
        "    pip install -r requirements.txt",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)
    sys.exit(1)
