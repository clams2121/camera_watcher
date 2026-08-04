"""Exercises pidlock against real PIDs (a real spawned-and-killed
subprocess, and this test process's own PID) rather than mocking os.kill
-- the whole point of this module is correctly telling a live PID from a
dead one, so faking that out would leave the actual behavior untested.
"""
import os
import subprocess
import sys
import time

import pytest

from single_camera_stream.pidlock import AlreadyRunningError, acquire, release


def _spawn_and_reap() -> int:
    """Returns a PID that's guaranteed to no longer be running."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


@pytest.fixture
def live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.1)  # let it actually start
    yield proc
    proc.kill()
    proc.wait(timeout=5)


def test_acquire_on_a_fresh_directory_writes_our_own_pid(tmp_path):
    pid_file = tmp_path / "single_camera_stream.pid"
    acquire(pid_file)

    assert pid_file.exists()
    assert int(pid_file.read_text().strip()) == os.getpid()


def test_acquire_refuses_when_a_live_process_holds_the_lock(tmp_path, live_process):
    pid_file = tmp_path / "single_camera_stream.pid"
    pid_file.write_text(str(live_process.pid))

    with pytest.raises(AlreadyRunningError, match=str(live_process.pid)):
        acquire(pid_file)

    # never touched -- still names the genuinely running process
    assert int(pid_file.read_text().strip()) == live_process.pid


def test_acquire_takes_over_a_stale_pid_file(tmp_path):
    dead_pid = _spawn_and_reap()
    pid_file = tmp_path / "single_camera_stream.pid"
    pid_file.write_text(str(dead_pid))

    acquire(pid_file)  # must not raise -- the old PID is no longer running

    assert int(pid_file.read_text().strip()) == os.getpid()


def test_acquire_takes_over_an_unreadable_pid_file(tmp_path):
    pid_file = tmp_path / "single_camera_stream.pid"
    pid_file.write_text("not-a-number")

    acquire(pid_file)

    assert int(pid_file.read_text().strip()) == os.getpid()


def test_acquire_creates_parent_directories_if_needed(tmp_path):
    pid_file = tmp_path / "nested" / "dir" / "single_camera_stream.pid"
    acquire(pid_file)
    assert pid_file.exists()


def test_release_removes_a_pid_file_that_still_names_us(tmp_path):
    pid_file = tmp_path / "single_camera_stream.pid"
    acquire(pid_file)

    release(pid_file)

    assert not pid_file.exists()


def test_release_leaves_a_pid_file_owned_by_someone_else_alone(tmp_path):
    pid_file = tmp_path / "single_camera_stream.pid"
    pid_file.write_text("999999999")  # not our PID

    release(pid_file)

    assert pid_file.exists()  # untouched -- not ours to delete


def test_release_is_safe_when_no_pid_file_exists(tmp_path):
    pid_file = tmp_path / "single_camera_stream.pid"
    release(pid_file)  # must not raise
