"""Ensures only one instance of this process runs against one config
directory at a time, via a plain PID file next to that directory's config.

Deliberately the exact mechanism specified for this tool -- not a kernel
file lock (``flock``), which would be more airtight but isn't what was
asked for:

1. Before doing anything else, check for an existing PID file. If it names
   a PID that's still alive, refuse to start outright -- a second instance
   really is already running.
2. Otherwise (no PID file yet, or a stale one left behind by a crashed/
   killed run) write our own PID into it.
3. Immediately re-read the file back. This guards against the narrow race
   where a second instance passed its own check #1 in the same window --
   whichever process's write landed last on the filesystem is the one the
   file names now. If it isn't us, we lost that race and stop rather than
   both processes believing they hold the lock.

Checking whether an existing PID is actually still alive (rather than just
"the file exists") is a deliberate, minimal addition beyond a literal
existence check: without it, a single past crash (kill -9, an OOM kill,
the machine losing power mid-write) would permanently wedge this tool from
ever starting again until someone manually deleted the file -- an
unnecessary manual-recovery step for what should just be a routine
restart.
"""
from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunningError(Exception):
    """Raised when another live instance already holds the lock, or when
    this instance loses the startup race to one that grabbed it first."""


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by another user -- still alive
    return True


def _read_pid(pid_file: Path) -> "int | None":
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


def acquire(pid_file: Path) -> None:
    """Raises AlreadyRunningError if another live instance holds the lock
    (or wins the startup race); otherwise writes this process's own PID
    into `pid_file` and returns."""
    if pid_file.exists():
        existing_pid = _read_pid(pid_file)
        if existing_pid is not None and existing_pid != os.getpid() and _pid_is_alive(existing_pid):
            raise AlreadyRunningError(
                f"{pid_file} names PID {existing_pid}, which is still running -- "
                f"refusing to start a second instance against the same config."
            )
        # Missing, unreadable, or naming a PID that's no longer alive --
        # safe to take over.

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    written = _read_pid(pid_file)
    if written != os.getpid():
        raise AlreadyRunningError(
            f"Lost the startup race for {pid_file} to PID {written} -- stopping."
        )


def release(pid_file: Path) -> None:
    """Best-effort cleanup on clean shutdown -- only removes the file if it
    still names this process, so a lock a different (newer) instance has
    since taken over is never accidentally deleted out from under it."""
    if _read_pid(pid_file) == os.getpid():
        try:
            pid_file.unlink()
        except OSError:
            pass
