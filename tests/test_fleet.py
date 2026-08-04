from pathlib import Path

import yaml

from camera_watcher.config import Config, ConfigError
from camera_watcher.fleet import CameraManager, FleetConfig


# ---------- FleetConfig ----------


def test_fleet_config_bootstraps_files_and_a_token_on_first_load(tmp_path):
    config_dir = tmp_path / "config"
    fc = FleetConfig(config_dir)

    assert (config_dir / "fleet.yaml").exists()
    assert (config_dir / "fleet.secrets.yaml").exists()
    assert fc.bootstrapped_token is not None
    assert len(fc.bootstrapped_token) >= 32
    assert fc.secrets["web"]["auth_token"] == fc.bootstrapped_token


def test_fleet_config_does_not_regenerate_an_existing_token(tmp_path):
    config_dir = tmp_path / "config"
    first = FleetConfig(config_dir)
    original_token = first.secrets["web"]["auth_token"]

    second = FleetConfig(config_dir)
    assert second.bootstrapped_token is None
    assert second.secrets["web"]["auth_token"] == original_token


def test_fleet_config_leaves_a_hand_edited_weak_token_alone(tmp_path):
    # Blank/missing tokens get auto-generated (see above), but a present,
    # if too-short, token was a deliberate edit and must never be silently
    # clobbered by the bootstrap -- require_token()'s fail-loud check (see
    # test_main.py) is what catches this case instead.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "fleet.secrets.yaml").write_text(yaml.safe_dump({"web": {"auth_token": "short"}}))

    fc = FleetConfig(config_dir)
    assert fc.bootstrapped_token is None
    assert fc.secrets["web"]["auth_token"] == "short"


def test_fleet_config_resolves_relative_data_root_against_config_dir(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    assert fc.resolved_data_root() == (tmp_path / "config" / "data").resolve()
    assert fc.resolved_clips_root() == (tmp_path / "config" / "data" / "clips").resolve()


def test_fleet_config_update_settings_persists_and_merges(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    fc.update_settings({"web": {"port": 9999}})

    assert fc.settings["web"]["port"] == 9999
    assert fc.settings["web"]["host"] == "tailscale"  # untouched default survives the merge

    reloaded = FleetConfig(tmp_path / "config")
    assert reloaded.settings["web"]["port"] == 9999


def test_fleet_config_rotate_auth_token_persists_a_new_one(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    original = fc.secrets["web"]["auth_token"]

    new_token = fc.rotate_auth_token()

    assert new_token != original
    assert fc.secrets["web"]["auth_token"] == new_token
    reloaded = FleetConfig(tmp_path / "config")
    assert reloaded.secrets["web"]["auth_token"] == new_token


# ---------- CameraManager ----------


def test_camera_manager_start_with_no_cameras_configured_does_nothing(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.start()  # must not raise
    assert manager.list_cameras() == []


def test_camera_manager_discovers_and_starts_a_preexisting_camera_config(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    Config.create(manager.cameras_dir / "front-door.yaml", {"camera": {"name": "front-door", "host": "192.0.2.1"}})

    manager.start()
    try:
        assert manager.exists("front-door")
        assert manager.get_error("front-door") is None
        assert manager.get_pipeline("front-door") is not None
    finally:
        manager.stop()


def test_camera_manager_isolates_a_broken_camera_config_from_the_rest(tmp_path):
    # A hand-corrupted config (blank camera.name -- fails Config's own
    # validation) must never stop discovery/startup of the other, valid
    # cameras, and must never raise out of start() at all.
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.cameras_dir.mkdir(parents=True, exist_ok=True)
    (manager.cameras_dir / "broken.yaml").write_text(yaml.safe_dump({"camera": {"host": "x"}}))  # no name
    Config.create(manager.cameras_dir / "good.yaml", {"camera": {"name": "good", "host": "192.0.2.2"}})

    manager.start()  # must not raise
    try:
        assert manager.exists("broken")
        assert manager.get_error("broken") is not None
        assert manager.get_pipeline("broken") is None

        assert manager.exists("good")
        assert manager.get_error("good") is None
        assert manager.get_pipeline("good") is not None
    finally:
        manager.stop()


def test_add_camera_creates_config_and_secrets_and_starts_it(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)

    result = manager.add_camera(
        {"camera": {"name": "front-door", "host": "192.0.2.1"}}, {"camera": {"username": "admin", "password": "hunter2"}}
    )
    try:
        assert result["id"] == "front-door"
        assert result["error"] is None
        assert (manager.cameras_dir / "front-door.yaml").exists()
        assert (manager.cameras_dir / "front-door.secrets.yaml").exists()

        config = manager.get_config("front-door")
        assert config.has_credentials()
        # every camera shares the fleet's data_root, baked in as absolute
        assert config.settings["data_root"] == str(fc.resolved_data_root())
    finally:
        manager.stop()


def test_add_camera_rejects_a_blank_name(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    try:
        manager.add_camera({"camera": {"name": "", "host": "x"}})
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_add_camera_rejects_an_invalid_name(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    try:
        manager.add_camera({"camera": {"name": "not valid!", "host": "x"}})
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_add_camera_rejects_a_duplicate_name(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.add_camera({"camera": {"name": "front-door", "host": "192.0.2.1"}})
    try:
        try:
            manager.add_camera({"camera": {"name": "front-door", "host": "192.0.2.9"}})
            assert False, "expected ConfigError"
        except ConfigError:
            pass
    finally:
        manager.stop()


def test_update_camera_applies_settings_to_the_running_pipeline(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.add_camera({"camera": {"name": "front-door", "host": "192.0.2.1"}})
    try:
        manager.update_camera("front-door", settings_patch={"camera": {"host": "192.0.2.9"}})
        assert manager.get_config("front-door").settings["camera"]["host"] == "192.0.2.9"
        # apply_settings() re-derives the RTSP URL from the new host immediately
        assert "192.0.2.9" in manager.get_pipeline("front-door").config.rtsp_url()
    finally:
        manager.stop()


def test_update_camera_raises_for_an_unknown_camera(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    try:
        manager.update_camera("nope", settings_patch={"camera": {"host": "x"}})
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_update_camera_retries_starting_a_previously_errored_camera(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.cameras_dir.mkdir(parents=True, exist_ok=True)
    Config.create(manager.cameras_dir / "broken.yaml", {"camera": {"name": "broken", "host": "bad-host"}})
    manager._errors["broken"] = "simulated startup failure"  # never actually started

    try:
        manager.update_camera("broken", settings_patch={"camera": {"host": "192.0.2.5"}})
        assert manager.get_error("broken") is None
        assert manager.get_pipeline("broken") is not None
    finally:
        manager.stop()


def test_remove_camera_deletes_its_config_and_stops_it(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.add_camera({"camera": {"name": "front-door", "host": "192.0.2.1"}})

    manager.remove_camera("front-door")

    assert not manager.exists("front-door")
    assert not (manager.cameras_dir / "front-door.yaml").exists()
    assert not (manager.cameras_dir / "front-door.secrets.yaml").exists()


def test_remove_camera_with_delete_data_removes_its_clips_and_cache_dirs(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.add_camera({"camera": {"name": "front-door", "host": "192.0.2.1"}})
    resolved = manager.get_config("front-door").resolved()
    output_dir = Path(resolved["recording"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "clip.mp4").write_bytes(b"x")

    manager.remove_camera("front-door", delete_data=True)

    assert not output_dir.exists()


def test_get_config_for_editing_loads_a_fresh_config_for_an_errored_camera(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    manager.cameras_dir.mkdir(parents=True, exist_ok=True)
    Config.create(manager.cameras_dir / "broken.yaml", {"camera": {"name": "broken", "host": "bad-host"}})
    manager._errors["broken"] = "simulated startup failure"

    config = manager.get_config_for_editing("broken")
    assert config is not None
    assert config.settings["camera"]["host"] == "bad-host"


def test_get_config_for_editing_returns_none_for_an_unknown_camera(tmp_path):
    fc = FleetConfig(tmp_path / "config")
    manager = CameraManager(fc)
    assert manager.get_config_for_editing("nope") is None
