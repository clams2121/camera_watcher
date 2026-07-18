import subprocess

import pytest

from camera_watcher.tailscale import TailscaleError, resolve_tailscale_ip


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_resolves_a_valid_tailscale_ip(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeResult(stdout="100.101.102.103\n")
    )
    assert resolve_tailscale_ip() == "100.101.102.103"


def test_fails_loud_when_tailscale_cli_is_missing(monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", raise_not_found)
    with pytest.raises(TailscaleError, match="isn't installed"):
        resolve_tailscale_ip()


def test_fails_loud_on_a_timeout(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tailscale ip -4", timeout=10)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    with pytest.raises(TailscaleError, match="timed out"):
        resolve_tailscale_ip()


def test_fails_loud_when_not_logged_in(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeResult(returncode=1, stderr="not logged in")
    )
    with pytest.raises(TailscaleError, match="not logged in"):
        resolve_tailscale_ip()


@pytest.mark.parametrize(
    "bogus_output",
    [
        "",  # empty
        "192.168.1.50\n",  # a LAN address, not Tailscale's range
        "not-an-ip\n",
        "8.8.8.8\n",
    ],
)
def test_fails_loud_on_output_outside_the_tailscale_cgnat_range(monkeypatch, bogus_output):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeResult(stdout=bogus_output))
    with pytest.raises(TailscaleError, match="doesn't look like a Tailscale address"):
        resolve_tailscale_ip()


def test_never_falls_back_to_0_0_0_0(monkeypatch):
    """No code path in resolve_tailscale_ip should ever return 0.0.0.0 --
    every failure mode must raise instead."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _FakeResult(returncode=1, stderr="boom")
    )
    with pytest.raises(TailscaleError):
        ip = resolve_tailscale_ip()
        assert ip != "0.0.0.0"  # unreachable if the raise above fires, which is the point
