"""Exercises update.sh end-to-end against real, throwaway git repos rather
than mocking git away -- the whole point of this script is careful git
plumbing (dirty-tree refusal, fast-forward-only), so faking it out would
leave the actual behavior untested.
"""
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

UPDATE_SH = Path(__file__).resolve().parent.parent / "update.sh"


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"},
    )


def _fake_python(clone_dir: Path, script: str = '#!/bin/sh\necho "pip ok"\nexit 0\n') -> None:
    venv_bin = clone_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    python = venv_bin / "python"
    python.write_text(script)
    python.chmod(0o755)


def _make_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "VERSION").write_text("v1\n")
    (origin / "requirements.txt").write_text("# no deps\n")
    (origin / "requirements-classifier.txt").write_text("-r requirements.txt\n")
    (origin / ".gitignore").write_text(".venv/\n")
    shutil.copy(UPDATE_SH, origin / "update.sh")
    (origin / "update.sh").chmod(0o755)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "initial")
    return origin


def _clone(origin: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "-q", str(origin), dest.name)
    return dest


def _run_update(clone_dir: Path, *args, env=None):
    return subprocess.run(
        [str(clone_dir / "update.sh"), *args], cwd=clone_dir, capture_output=True, text=True, env=env
    )


def _fake_systemctl_and_sudo(clone_dir: Path, camera_watcher_deployed=False, classifier_present=False):
    """A fake `systemctl` (+ `sudo` that just execs through to it) on PATH,
    standing in for a real systemd this sandbox doesn't have running (see
    test_gracefully_reports_no_systemd_units_when_none_are_installed --
    the real systemctl here always reports zero units, since there's no
    bus to connect to). Returns (env, restart_log_path) -- restart_log
    records every `systemctl restart <unit>` call, in order.

    Deliberately written OUTSIDE clone_dir (a real git working tree) --
    update.sh refuses to run against a dirty tree, and these support files
    would otherwise show up as untracked changes in it.
    """
    bin_dir = clone_dir.parent / f"{clone_dir.name}-fakebin"
    bin_dir.mkdir(exist_ok=True)
    restart_log = clone_dir.parent / f"{clone_dir.name}-restart.log"

    camera_line = "camera-watcher.service loaded active running"
    classifier_line = "clip-classifier.service loaded active running"

    # Only emit a printf when there's actually something to list -- matching
    # real systemctl's true behavior of zero lines of output for zero units
    # (an unconditional printf would emit one blank line even for "none",
    # which the `grep -q .` check in update.sh must correctly treat as
    # "not deployed" rather than a false match).
    camera_block = f"printf '%s\\n' {shlex.quote(camera_line)}" if camera_watcher_deployed else ":"
    classifier_block = f"printf '%s\\n' {shlex.quote(classifier_line)}" if classifier_present else ":"

    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        f"""#!/bin/bash
if [ "$1" = "list-units" ]; then
  pattern="${{@: -1}}"
  case "$pattern" in
    'camera-watcher.service') {camera_block} ;;
    'clip-classifier.service') {classifier_block} ;;
  esac
  exit 0
elif [ "$1" = "restart" ]; then
  echo "$2" >> {shlex.quote(str(restart_log))}
  exit 0
elif [ "$1" = "is-active" ]; then
  echo "active"
  exit 0
fi
exit 0
"""
    )
    systemctl.chmod(0o755)

    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\nexec \"$@\"\n")
    sudo.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env, restart_log


def test_refuses_a_dirty_working_tree(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)
    (clone / "VERSION").write_text("dirty\n")

    result = _run_update(clone, "--no-fetch")

    assert result.returncode == 1
    assert "working tree is not clean" in result.stderr
    assert (clone / "VERSION").read_text() == "dirty\n"  # never touched further


def test_fails_loud_without_a_venv(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    # no .venv created at all

    result = _run_update(clone, "--no-fetch")

    assert result.returncode == 1
    assert ".venv" in result.stderr
    assert "not found or not executable" in result.stderr


def test_no_fetch_reinstalls_deps_and_reports_no_units_to_restart(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)

    result = _run_update(clone, "--no-fetch")

    assert result.returncode == 0, result.stderr
    assert "pip ok" in result.stdout
    assert "Update complete" in result.stdout
    assert "skipping fetch/merge" in result.stdout


def test_fetch_and_fast_forward_applies_a_real_upstream_update(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)

    # advance origin with a genuinely new commit, without disturbing its
    # checked-out branch (mirrors how a real remote gets updated)
    _git(origin, "checkout", "-q", "--detach")
    (origin / "VERSION").write_text("v2\n")
    _git(origin, "add", "VERSION")
    _git(origin, "commit", "-q", "-m", "v2")
    _git(origin, "branch", "-f", "main", "HEAD")
    _git(origin, "checkout", "-q", "main")

    result = _run_update(clone)
    assert result.returncode == 0, result.stderr
    assert "Fast-forward" in result.stdout or "Updated" in result.stdout
    assert (clone / "VERSION").read_text() == "v2\n"


def test_diverged_branch_fails_loud_and_changes_nothing(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)

    # the clone gets a local commit origin never sees...
    (clone / "local_only.txt").write_text("local work\n")
    _git(clone, "add", "local_only.txt")
    _git(clone, "commit", "-q", "-m", "local-only commit")
    before = _git(clone, "rev-parse", "HEAD").stdout.strip()

    # ...while origin moves forward independently
    _git(origin, "checkout", "-q", "--detach")
    (origin / "VERSION").write_text("v2\n")
    _git(origin, "add", "VERSION")
    _git(origin, "commit", "-q", "-m", "v2")
    _git(origin, "branch", "-f", "main", "HEAD")
    _git(origin, "checkout", "-q", "main")

    result = _run_update(clone)

    assert result.returncode == 1
    assert "diverged" in result.stderr
    assert _git(clone, "rev-parse", "HEAD").stdout.strip() == before  # untouched


def test_pip_failure_gives_a_rollback_recipe_and_does_not_restart_anything(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone, script='#!/bin/sh\necho "pip explosion" >&2\nexit 1\n')
    before = _git(clone, "rev-parse", "HEAD").stdout.strip()

    result = _run_update(clone, "--no-fetch")

    assert result.returncode == 1
    assert "pip explosion" in result.stderr
    assert "dependency install failed" in result.stderr
    assert f"git reset --hard {before}" in result.stderr
    assert "NOT restarting" in result.stderr


def test_rejects_unknown_arguments(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)

    result = _run_update(clone, "--bogus")

    assert result.returncode == 1
    assert "unknown argument" in result.stderr


def test_fails_loud_in_detached_head_state(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)
    _git(clone, "checkout", "-q", "--detach")

    result = _run_update(clone)

    assert result.returncode == 1
    assert "detached HEAD" in result.stderr


def test_gracefully_reports_no_systemd_units_when_none_are_installed(tmp_path):
    # This sandbox does have a real `systemctl`, but no camera-watcher.service
    # registered -- exercises the real (not mocked) systemctl list-units
    # call finding nothing, rather than assuming it's absent.
    if shutil.which("systemctl") is None:
        pytest.skip("systemctl not available in this environment")

    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)

    result = _run_update(clone, "--no-fetch")

    assert result.returncode == 0, result.stderr
    assert "No camera-watcher.service found" in result.stdout


# ---------- camera-watcher.service / clip-classifier.service detection+restart (fake systemctl+sudo) ----------


def test_restarts_camera_watcher_only_when_no_classifier_is_deployed(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)
    env, restart_log = _fake_systemctl_and_sudo(clone, camera_watcher_deployed=True, classifier_present=False)

    result = _run_update(clone, "--no-fetch", env=env)

    assert result.returncode == 0, result.stderr
    assert "camera-watcher.service" in result.stdout
    assert "clip-classifier" not in result.stdout
    assert restart_log.read_text().splitlines() == ["camera-watcher.service"]


def test_installs_classifier_deps_and_restarts_it_when_deployed(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)
    env, restart_log = _fake_systemctl_and_sudo(clone, camera_watcher_deployed=True, classifier_present=True)

    result = _run_update(clone, "--no-fetch", env=env)

    assert result.returncode == 0, result.stderr
    assert "clip-classifier.service is deployed" in result.stdout
    assert "clip_classifier dependencies installed" in result.stdout
    assert set(restart_log.read_text().splitlines()) == {"camera-watcher.service", "clip-classifier.service"}


def test_classifier_alone_with_no_camera_watcher_still_gets_restarted(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    _fake_python(clone)
    env, restart_log = _fake_systemctl_and_sudo(clone, camera_watcher_deployed=False, classifier_present=True)

    result = _run_update(clone, "--no-fetch", env=env)

    assert result.returncode == 0, result.stderr
    assert restart_log.read_text().splitlines() == ["clip-classifier.service"]


def test_classifier_dependency_failure_gives_a_rollback_recipe_and_does_not_restart(tmp_path):
    origin = _make_origin(tmp_path)
    clone = _clone(origin, tmp_path / "clone")
    # First pip call (requirements.txt) succeeds, second (requirements-classifier.txt) fails.
    _fake_python(
        clone,
        script=(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *requirements-classifier.txt*) echo \"classifier pip explosion\" >&2; exit 1 ;;\n"
            '  *) echo "pip ok"; exit 0 ;;\n'
            "esac\n"
        ),
    )
    env, restart_log = _fake_systemctl_and_sudo(clone, camera_watcher_deployed=True, classifier_present=True)
    before = _git(clone, "rev-parse", "HEAD").stdout.strip()

    result = _run_update(clone, "--no-fetch", env=env)

    assert result.returncode == 1
    assert "classifier pip explosion" in result.stderr
    assert "clip_classifier dependency install failed" in result.stderr
    assert f"git reset --hard {before}" in result.stderr
    assert not restart_log.exists()  # nothing was restarted at all
