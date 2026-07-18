import pytest
import yaml

from clip_classifier.config import Config, ConfigError


def _write_config(path, **extra_yaml):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(extra_yaml))
    return path


def test_missing_config_file_fails_loud_with_actionable_message(tmp_path):
    missing = tmp_path / "classifier.yaml"
    with pytest.raises(ConfigError) as exc_info:
        Config(missing)
    assert "cp config/classifier.example.yaml" in str(exc_info.value)


def test_data_root_is_required(tmp_path):
    path = _write_config(tmp_path / "classifier.yaml")
    with pytest.raises(ConfigError, match="data_root"):
        Config(path)


def test_data_root_blank_is_rejected(tmp_path):
    path = _write_config(tmp_path / "classifier.yaml", data_root="")
    with pytest.raises(ConfigError, match="data_root"):
        Config(path)


def test_invalid_backend_is_rejected(tmp_path):
    path = _write_config(tmp_path / "classifier.yaml", data_root="data", backend="gpu")
    with pytest.raises(ConfigError, match="backend"):
        Config(path)


def test_minimal_config_resolves_to_full_defaults(tmp_path):
    path = _write_config(tmp_path / "classifier.yaml", data_root="data")
    config = Config(path)
    resolved = config.resolved()

    assert resolved["data_root"] == str((tmp_path / "data").resolve())
    assert resolved["backend"] == "auto"
    assert resolved["thresholds"]["high_confidence"] == 0.5
    assert resolved["sampling"]["max_frames"] == 5
    assert resolved["watch"]["rescan_interval_seconds"] == 600


def test_relative_paths_resolve_against_config_dir_not_cwd(tmp_path, monkeypatch):
    config_dir = tmp_path / "nested" / "config"
    path = _write_config(config_dir / "classifier.yaml", data_root="../data", cpu={"model_path": "models/yolov8n.onnx"})
    config = Config(path)

    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = config.resolved()
    assert resolved["data_root"] == str((config_dir / "../data").resolve())
    assert resolved["cpu"]["model_path"] == str((config_dir / "models/yolov8n.onnx").resolve())


def test_absolute_paths_pass_through_unchanged(tmp_path):
    abs_data_root = tmp_path / "abs-data"
    path = _write_config(tmp_path / "classifier.yaml", data_root=str(abs_data_root))
    config = Config(path)
    assert config.resolved()["data_root"] == str(abs_data_root)


def test_explicit_settings_override_defaults(tmp_path):
    path = _write_config(
        tmp_path / "classifier.yaml",
        data_root="data",
        backend="cpu",
        thresholds={"high_confidence": 0.7},
        sampling={"max_frames": 3},
    )
    resolved = Config(path).resolved()
    assert resolved["backend"] == "cpu"
    assert resolved["thresholds"]["high_confidence"] == 0.7
    assert resolved["thresholds"]["review_large_object_area_frac"] == 0.05  # untouched default survives the merge
    assert resolved["sampling"]["max_frames"] == 3
