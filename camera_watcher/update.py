"""Self-update: fast-forward-only `git pull` the running checkout, optionally
reinstall dependencies, then (from routes.py/main.py) restart into the new
code.

Deliberately conservative:

- ``--ff-only`` refuses to create a merge commit or silently discard local
  history -- it just fails if the local branch has diverged, rather than
  doing something surprising.
- Never reports "updated" unless HEAD actually moved, so callers don't
  restart the server for no reason.
- Dependency installation only runs after a successful pull, and its
  failure is reported distinctly so a caller can choose not to restart into
  a checkout whose new dependencies didn't install cleanly.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def repo_root() -> Path:
    """The checkout root, assuming this file lives at <root>/camera_watcher/update.py."""
    return Path(__file__).resolve().parent.parent


@dataclass
class CommandResult:
    ok: bool
    message: str


def _run(args: List[str], cwd: Path, timeout: float) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"{args[0]} is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return False, f"`{' '.join(args)}` timed out after {timeout}s"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, output


def current_commit(cwd: Path) -> Optional[str]:
    ok, output = _run(["git", "rev-parse", "HEAD"], cwd, timeout=10)
    return output if ok else None


def pull_latest(cwd: Path, timeout: float = 30) -> Tuple[CommandResult, bool]:
    """Runs `git pull --ff-only`. Returns (result, updated) -- `updated` is
    only True if HEAD actually moved as a result."""
    before = current_commit(cwd)
    ok, output = _run(["git", "pull", "--ff-only"], cwd, timeout)
    if not ok:
        return CommandResult(False, output or "git pull failed"), False
    after = current_commit(cwd)
    updated = before is not None and after is not None and before != after
    return CommandResult(True, output or "Already up to date."), updated


def install_dependencies(cwd: Path, timeout: float = 300) -> CommandResult:
    requirements = cwd / "requirements.txt"
    if not requirements.exists():
        return CommandResult(True, "No requirements.txt found; skipped.")
    ok, output = _run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)], cwd, timeout
    )
    return CommandResult(ok, output or ("Dependencies installed." if ok else "pip install failed"))
