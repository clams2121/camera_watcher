import socket
import sys

import pytest

from camera_watcher.config import ConfigError
from camera_watcher.main import _check_port_available, _parse_args


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
