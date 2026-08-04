import subprocess
import sys
import time

import pytest
import yaml

from single_camera_stream.config import Config
from single_camera_stream.main import _build_recorder_config, _parse_args, main


def test_config_arg_is_optional(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["single_camera_stream"])
    args = _parse_args()
    assert args.config is None


def test_config_arg_is_parsed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["single_camera_stream", "--config", "some/config.yaml"])
    args = _parse_args()
    assert args.config == "some/config.yaml"


def test_build_recorder_config_maps_every_setting(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "camera": {"name": "front-door", "host": "192.168.1.50"},
                "motion": {"draw_bounding_box": True, "box_padding_px": 20},
                "recording": {
                    "output_dir": "clips",
                    "pre_buffer_seconds": 3,
                    "post_buffer_seconds": 4,
                    "max_chunk_seconds": 90,
                    "overlap_seconds": 2,
                    "fallback_fps": 12,
                },
            }
        )
    )
    config = Config(config_path)

    rec_cfg = _build_recorder_config(config)

    assert rec_cfg.camera_name == "front-door"
    assert rec_cfg.output_dir == (tmp_path / "clips").resolve()
    assert rec_cfg.pre_buffer_seconds == 3
    assert rec_cfg.post_buffer_seconds == 4
    assert rec_cfg.max_chunk_seconds == 90
    assert rec_cfg.overlap_seconds == 2
    assert rec_cfg.fallback_fps == 12
    assert rec_cfg.draw_bounding_box is True
    assert rec_cfg.box_padding_px == 20


def test_main_fails_loud_on_a_missing_config_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["single_camera_stream", "--config", str(tmp_path / "nope.yaml")])
    with pytest.raises(SystemExit):
        main()
    assert "cp single_camera_stream/config.example.yaml" in capsys.readouterr().err


def test_main_fails_loud_when_another_instance_already_holds_the_lock(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"camera": {"name": "cam1", "host": "192.168.1.50"}}))
    pid_file = tmp_path / "single_camera_stream.pid"

    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.1)
    try:
        pid_file.write_text(str(live.pid))
        monkeypatch.setattr(sys, "argv", ["single_camera_stream", "--config", str(config_path)])

        with pytest.raises(SystemExit):
            main()

        assert "refusing to start a second instance" in capsys.readouterr().err
        assert int(pid_file.read_text().strip()) == live.pid  # untouched
    finally:
        live.kill()
        live.wait(timeout=5)
