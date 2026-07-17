import yaml

from camera_watcher.config import Config


def test_defaults_and_save(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config = Config(settings_path, secrets_path)

    assert config.settings["camera"]["port"] == 554
    assert config.has_credentials() is False

    config.update_settings({"camera": {"host": "10.0.0.5", "port": 8554}})
    assert settings_path.exists()
    on_disk = yaml.safe_load(settings_path.read_text())
    assert on_disk["camera"]["host"] == "10.0.0.5"
    assert on_disk["camera"]["port"] == 8554
    assert on_disk["recording"]["pre_buffer_seconds"] == 10  # untouched default preserved


def test_secrets_stay_separate(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    secrets_path = tmp_path / "secrets.yaml"
    config = Config(settings_path, secrets_path)

    config.update_secrets({"camera": {"username": "admin", "password": "hunter2"}})
    assert config.has_credentials()

    settings_on_disk = settings_path.read_text() if settings_path.exists() else ""
    assert "hunter2" not in settings_on_disk

    secrets_on_disk = yaml.safe_load(secrets_path.read_text())
    assert secrets_on_disk["camera"]["password"] == "hunter2"


def test_rtsp_url_building_and_redaction(tmp_path):
    config = Config(tmp_path / "settings.yaml", tmp_path / "secrets.yaml")
    config.update_settings({"camera": {"host": "192.168.1.10", "port": 554, "path": "/stream1"}})
    config.update_secrets({"camera": {"username": "user", "password": "p@ss/word"}})

    url = config.rtsp_url()
    assert url.startswith("rtsp://user:")
    assert "p@ss/word" not in url  # must be percent-encoded, not embedded raw
    assert "192.168.1.10:554/stream1" in url

    redacted = config.redacted_rtsp_url()
    assert "user" not in redacted
    assert "192.168.1.10:554/stream1" in redacted


def test_seeds_settings_from_example_on_first_run(tmp_path):
    example_path = tmp_path / "settings.example.yaml"
    example_path.write_text("camera:\n  host: 10.1.1.1\n  name: frontdoor\n")
    settings_path = tmp_path / "settings.yaml"

    config = Config(settings_path, tmp_path / "secrets.yaml")

    assert settings_path.exists()
    assert settings_path.read_text() == example_path.read_text()
    assert config.settings["camera"]["host"] == "10.1.1.1"
    assert config.settings["camera"]["name"] == "frontdoor"


def test_does_not_overwrite_settings_that_already_exist(tmp_path):
    example_path = tmp_path / "settings.example.yaml"
    example_path.write_text("camera:\n  host: 10.1.1.1\n")
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("camera:\n  host: 192.168.9.9\n")  # user already has real settings

    config = Config(settings_path, tmp_path / "secrets.yaml")

    assert config.settings["camera"]["host"] == "192.168.9.9"  # untouched, not clobbered by the example


def test_missing_example_falls_back_to_builtin_defaults(tmp_path):
    settings_path = tmp_path / "settings.yaml"  # no settings.example.yaml alongside it
    config = Config(settings_path, tmp_path / "secrets.yaml")

    assert not settings_path.exists()  # nothing to seed from, and nothing written until a save happens
    assert config.settings["camera"]["port"] == 554  # built-in DEFAULTS still apply
