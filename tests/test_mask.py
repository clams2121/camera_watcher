from camera_watcher.mask import MaskStore


def test_save_and_reload(tmp_path):
    path = tmp_path / "mask.json"
    store = MaskStore(path)
    assert store.polygons == []

    polygons = [[[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]]
    store.save(polygons)

    reloaded = MaskStore(path)
    assert reloaded.polygons == polygons


def test_keep_mask_rasterizes_ignored_region(tmp_path):
    store = MaskStore(tmp_path / "mask.json")
    store.save([[[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]])  # left half ignored

    mask = store.keep_mask(width=100, height=100)
    assert mask[50, 10] == 0  # inside ignored left half
    assert mask[50, 90] == 255  # outside, right half


def test_invalid_polygons_are_dropped(tmp_path):
    store = MaskStore(tmp_path / "mask.json")
    store.save([[[0.0, 0.0], [1.0, 1.0]]])  # only 2 points -- not a polygon
    assert store.polygons == []
