import pytest

from camera_watcher.dependency_check import check_dependencies


def test_passes_when_all_importable():
    check_dependencies([("os", "n/a"), ("sys", "n/a")])  # stdlib, always present


def test_exits_with_helpful_message_when_missing(capsys):
    with pytest.raises(SystemExit) as exc_info:
        check_dependencies([("definitely_not_a_real_package_xyz", "fake-package")])

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "fake-package" in err
    assert "pip install -r requirements.txt" in err
