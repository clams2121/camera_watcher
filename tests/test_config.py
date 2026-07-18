import os

import pytest
import yaml

from camera_watcher.config import Config, ConfigError


def _write_config(path, camera_name="front-door", **extra_yaml):
    data = {"camera": {"name": camera_name, "host": "192.168.1.50"}}
    data.update(extra_yaml)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
    return path


def test_missing_config_file_fails_loud_with_actionable_message(tmp_path):
    missing = tmp_path / "front-door.yaml"
    with pytest.raises(ConfigError) as exc_info:
        Config(missing)
    message = str(exc_info.value)
    assert str(missing) in message
    assert "camera.example.yaml" in message  # tells the user how to fix it


def test_camera_name_is_required(tmp_path):
    path = tmp_path / "front-door.yaml"
    path.write_text(yaml.safe_dump({"camera": {"host": "192.168.1.50"}}))  # no name
    with pytest.raises(ConfigError, match="camera.name is required"):
        Config(path)


@pytest.mark.parametrize("bad_name", ["front door", "front/door", "front.door", "café"])
def test_camera_name_is_validated(tmp_path, bad_name):
    path = _write_config(tmp_path / "cam.yaml", camera_name=bad_name)
    with pytest.raises(ConfigError, match="invalid"):
        Config(path)


def test_defaults_and_save(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml")
    config = Config(path)

    assert config.settings["camera"]["port"] == 554
    assert config.has_credentials() is False

    config.update_settings({"camera": {"host": "10.0.0.5", "port": 8554}})
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk["camera"]["host"] == "10.0.0.5"
    assert on_disk["camera"]["port"] == 8554
    assert on_disk["recording"]["pre_buffer_seconds"] == 10  # untouched default preserved


def test_secrets_stay_in_a_separate_file(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml")
    config = Config(path)

    config.update_secrets({"camera": {"username": "admin", "password": "hunter2"}})
    assert config.has_credentials()

    assert "hunter2" not in path.read_text()
    secrets_on_disk = yaml.safe_load(config.secrets_path.read_text())
    assert secrets_on_disk["camera"]["password"] == "hunter2"


def test_secrets_path_defaults_to_camera_name_next_to_config(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml", camera_name="front-door")
    config = Config(path)
    assert config.secrets_path == tmp_path / "front-door.secrets.yaml"


def test_secrets_path_explicit_override_is_config_dir_relative(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml", secrets_path="shared/creds.yaml")
    config = Config(path)
    assert config.secrets_path == (tmp_path / "shared" / "creds.yaml").resolve()


def test_rtsp_url_building_and_redaction(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml")
    config = Config(path)
    config.update_settings({"camera": {"host": "192.168.1.10", "port": 554, "path": "/stream1"}})
    config.update_secrets({"camera": {"username": "user", "password": "p@ss/word"}})

    url = config.rtsp_url()
    assert url.startswith("rtsp://user:")
    assert "p@ss/word" not in url  # must be percent-encoded, not embedded raw
    assert "192.168.1.10:554/stream1" in url

    redacted = config.redacted_rtsp_url()
    assert "user" not in redacted
    assert "192.168.1.10:554/stream1" in redacted


def test_resolved_paths_are_relative_to_config_dir_not_cwd(tmp_path, monkeypatch):
    config_dir = tmp_path / "nested" / "config"
    path = _write_config(config_dir / "front-door.yaml", camera_name="front-door")
    config = Config(path)

    elsewhere = tmp_path / "somewhere-else-entirely"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # prove resolution doesn't depend on cwd

    resolved = config.resolved()
    assert resolved["recording"]["output_dir"] == str(config_dir / "data" / "clips" / "front-door")
    assert resolved["motion"]["heatmap_path"] == str(config_dir / "data" / "motion_heatmap.npy")
    assert resolved["mask"]["path"] == str(config_dir / "front-door.mask.json")


def test_absolute_paths_pass_through_unchanged(tmp_path):
    abs_output = tmp_path / "abs" / "clips"
    path = _write_config(tmp_path / "front-door.yaml", recording={"output_dir": str(abs_output)})
    config = Config(path)
    assert config.resolved()["recording"]["output_dir"] == str(abs_output)


def test_data_root_absolute_override(tmp_path):
    shared_root = tmp_path / "shared-data"
    path = _write_config(tmp_path / "front-door.yaml", data_root=str(shared_root))
    config = Config(path)
    resolved = config.resolved()
    assert resolved["data_root"] == str(shared_root)
    assert resolved["recording"]["output_dir"] == str(shared_root / "clips" / "front-door")


def test_settings_property_stays_raw_while_resolved_computes_absolute_paths(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml")
    config = Config(path)

    # Raw settings keep the "derive a default" sentinel -- never resolved,
    # so saving the web UI's form back never bakes an absolute path in.
    assert config.settings["recording"]["output_dir"] == ""
    assert config.settings["mask"]["path"] == ""

    assert config.resolved()["recording"]["output_dir"] != ""
    assert os.path.isabs(config.resolved()["recording"]["output_dir"])


def test_event_log_path_blank_means_disabled_not_a_derived_default(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml", recording={"event_log_path": ""})
    config = Config(path)
    assert config.resolved()["recording"]["event_log_path"] == ""


def test_reload_picks_up_external_edits(tmp_path):
    path = _write_config(tmp_path / "front-door.yaml")
    config = Config(path)
    assert config.settings["camera"]["host"] == "192.168.1.50"

    path.write_text(yaml.safe_dump({"camera": {"name": "front-door", "host": "10.9.9.9"}}))
    config.reload()
    assert config.settings["camera"]["host"] == "10.9.9.9"
