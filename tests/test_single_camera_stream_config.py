import yaml

from single_camera_stream.config import Config, ConfigError


def _write(tmp_path, data, name="config.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def test_missing_config_file_fails_loud_with_a_cp_command(tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    try:
        Config(missing)
        assert False, "expected ConfigError"
    except ConfigError as e:
        assert "cp single_camera_stream/config.example.yaml" in str(e)


def test_blank_host_is_rejected(tmp_path):
    path = _write(tmp_path, {"camera": {"name": "cam1", "host": ""}})
    try:
        Config(path)
        assert False, "expected ConfigError"
    except ConfigError as e:
        assert "camera.host" in str(e)


def test_blank_name_is_rejected(tmp_path):
    path = _write(tmp_path, {"camera": {"name": "", "host": "192.168.1.50"}})
    try:
        Config(path)
        assert False, "expected ConfigError"
    except ConfigError as e:
        assert "camera.name" in str(e)


def test_invalid_name_characters_are_rejected(tmp_path):
    path = _write(tmp_path, {"camera": {"name": "front door!", "host": "192.168.1.50"}})
    try:
        Config(path)
        assert False, "expected ConfigError"
    except ConfigError as e:
        assert "camera.name" in str(e)


def test_defaults_are_merged_under_a_minimal_config(tmp_path):
    path = _write(tmp_path, {"camera": {"name": "cam1", "host": "192.168.1.50"}})
    config = Config(path)

    assert config.settings["camera"]["port"] == 554
    assert config.settings["motion"]["min_area"] == 500
    assert config.settings["recording"]["pre_buffer_seconds"] == 10


def test_explicit_values_override_defaults(tmp_path):
    path = _write(
        tmp_path,
        {
            "camera": {"name": "cam1", "host": "192.168.1.50", "port": 8554},
            "motion": {"min_area": 999},
        },
    )
    config = Config(path)

    assert config.settings["camera"]["port"] == 8554
    assert config.settings["motion"]["min_area"] == 999
    assert config.settings["motion"]["var_threshold"] == 25  # untouched default survives the merge


def test_output_dir_resolves_relative_to_the_config_files_directory(tmp_path):
    path = _write(tmp_path, {"camera": {"name": "cam1", "host": "192.168.1.50"}, "recording": {"output_dir": "clips"}})
    config = Config(path)

    assert config.output_dir == (tmp_path / "clips").resolve()


def test_output_dir_absolute_path_passes_through_unchanged(tmp_path):
    absolute = tmp_path / "elsewhere" / "clips"
    path = _write(
        tmp_path, {"camera": {"name": "cam1", "host": "192.168.1.50"}, "recording": {"output_dir": str(absolute)}}
    )
    config = Config(path)

    assert config.output_dir == absolute


def test_rtsp_url_includes_credentials_but_redacted_does_not(tmp_path):
    path = _write(
        tmp_path,
        {"camera": {"name": "cam1", "host": "192.168.1.50", "username": "admin", "password": "hunter2"}},
    )
    config = Config(path)

    assert "admin:hunter2@" in config.rtsp_url()
    assert "192.168.1.50" in config.rtsp_url()
    assert "hunter2" not in config.redacted_rtsp_url()
    assert "admin" not in config.redacted_rtsp_url()


def test_rtsp_url_with_no_credentials_has_no_userinfo(tmp_path):
    path = _write(tmp_path, {"camera": {"name": "cam1", "host": "192.168.1.50"}})
    config = Config(path)

    assert config.rtsp_url() == "rtsp://192.168.1.50:554/h264Preview_01_main"
