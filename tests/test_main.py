import socket
import sys

import pytest
import yaml

import camera_watcher.main as main_module
from camera_watcher.config import ConfigError
from camera_watcher.main import _check_port_available, _parse_args, _resolve_host, main
from camera_watcher.tailscale import TailscaleError


def test_config_arg_is_required(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["camera_watcher"])
    with pytest.raises(SystemExit):
        _parse_args()


def test_config_arg_is_parsed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["camera_watcher", "--config", "config/front-door.yaml"])
    args = _parse_args()
    assert args.config == "config/front-door.yaml"


def test_check_port_available_succeeds_on_a_free_port():
    # Grab an OS-assigned free port, release it, and immediately check it --
    # a small TOCTOU window exists in real usage but is fine for this test.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    _check_port_available("127.0.0.1", free_port)  # must not raise


def test_check_port_available_fails_loud_on_a_taken_port():
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken_port = holder.getsockname()[1]
    try:
        with pytest.raises(ConfigError, match=str(taken_port)):
            _check_port_available("127.0.0.1", taken_port)
    finally:
        holder.close()


def test_resolve_host_passes_a_literal_host_through_unchanged():
    assert _resolve_host("127.0.0.1") == "127.0.0.1"
    assert _resolve_host("192.168.1.50") == "192.168.1.50"


def test_resolve_host_resolves_the_tailscale_sentinel(monkeypatch):
    monkeypatch.setattr(main_module, "resolve_tailscale_ip", lambda: "100.64.1.2")
    assert _resolve_host("tailscale") == "100.64.1.2"


def test_resolve_host_fails_loud_and_never_falls_back_to_0_0_0_0(monkeypatch):
    def raise_err(*a, **k):
        raise TailscaleError("not logged in")

    monkeypatch.setattr(main_module, "resolve_tailscale_ip", raise_err)
    with pytest.raises(ConfigError, match="not logged in"):
        _resolve_host("tailscale")


def test_main_fails_loud_without_a_configured_auth_token(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "cam1.yaml"
    config_path.write_text(yaml.safe_dump({"camera": {"name": "cam1", "host": "127.0.0.1"}}))
    monkeypatch.setattr(sys, "argv", ["camera_watcher", "--config", str(config_path)])

    with pytest.raises(SystemExit):
        main()

    assert "auth_token" in capsys.readouterr().err
