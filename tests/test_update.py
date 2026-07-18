import subprocess
from pathlib import Path

import camera_watcher.update as update


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_pull_latest_reports_updated_when_head_moves(monkeypatch):
    commits = iter(["abc111", "def222"])  # before, after
    calls = []

    def fake_run(args, cwd, capture_output, text, timeout):
        calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return _FakeCompletedProcess(0, next(commits))
        assert args == ["git", "pull", "--ff-only"]
        return _FakeCompletedProcess(0, "Fast-forwarded to def222.")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result, updated = update.pull_latest(Path("/repo"))
    assert result.ok
    assert updated
    assert "Fast-forwarded" in result.message


def test_pull_latest_not_updated_when_already_current(monkeypatch):
    def fake_run(args, cwd, capture_output, text, timeout):
        if args[:2] == ["git", "rev-parse"]:
            return _FakeCompletedProcess(0, "abc111")  # same both times
        return _FakeCompletedProcess(0, "Already up to date.")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result, updated = update.pull_latest(Path("/repo"))
    assert result.ok
    assert not updated


def test_pull_latest_failure_is_reported_and_not_updated(monkeypatch):
    def fake_run(args, cwd, capture_output, text, timeout):
        if args[:2] == ["git", "rev-parse"]:
            return _FakeCompletedProcess(0, "abc111")
        return _FakeCompletedProcess(1, "", "fatal: Not possible to fast-forward, aborting.")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result, updated = update.pull_latest(Path("/repo"))
    assert not result.ok
    assert not updated
    assert "fast-forward" in result.message


def test_pull_latest_handles_missing_git_binary(monkeypatch):
    def fake_run(args, cwd, capture_output, text, timeout):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    result, updated = update.pull_latest(Path("/repo"))
    assert not result.ok
    assert not updated
    assert "not installed" in result.message


def test_pull_latest_handles_timeout(monkeypatch):
    def fake_run(args, cwd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result, updated = update.pull_latest(Path("/repo"), timeout=5)
    assert not result.ok
    assert not updated
    assert "timed out" in result.message


def test_install_dependencies_skipped_without_requirements_file(tmp_path):
    result = update.install_dependencies(tmp_path)
    assert result.ok
    assert "skipped" in result.message.lower()


def test_install_dependencies_runs_pip_and_reports_failure(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("flask\n")

    def fake_run(args, cwd, capture_output, text, timeout):
        assert args[1:3] == ["-m", "pip"]
        return _FakeCompletedProcess(1, "", "ERROR: could not find a version")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = update.install_dependencies(tmp_path)
    assert not result.ok
    assert "ERROR" in result.message


def test_repo_root_points_at_checkout_containing_the_package(tmp_path):
    root = update.repo_root()
    assert (root / "camera_watcher" / "update.py").is_file()
    assert (root / "requirements.txt").is_file()
