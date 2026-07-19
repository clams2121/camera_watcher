"""Tiny shared YAML read/write helpers used by both config.py (per-camera)
and fleet.py (fleet-wide + retention settings) -- kept here instead of
duplicated so the atomic-write/chmod behavior only has one implementation
to get right.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    tmp_path.replace(path)
    try:
        # Best-effort: keep secrets files from being world/group readable.
        # Harmless (if slightly redundant) on non-secret files too.
        path.chmod(0o600)
    except OSError:
        pass
