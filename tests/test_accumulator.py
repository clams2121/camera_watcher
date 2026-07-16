import cv2
import numpy as np

from camera_watcher.accumulator import MotionAccumulator


def _decode_alpha(png_bytes):
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    bgra = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    assert bgra.shape[2] == 4
    return bgra[..., 3]


def test_add_marks_hit_pixels_and_leaves_others_transparent(tmp_path):
    acc = MotionAccumulator(tmp_path / "heatmap.npy", width=10, height=10)
    mask = np.zeros((10, 10), dtype=np.uint8)
    mask[2:4, 2:4] = 255

    acc.add(mask)

    alpha = _decode_alpha(acc.heatmap_png())
    assert alpha[3, 3] > 0
    assert alpha[8, 8] == 0


def test_counts_never_decay_only_reset_clears_them(tmp_path):
    acc = MotionAccumulator(tmp_path / "heatmap.npy", width=10, height=10)
    mask = np.zeros((10, 10), dtype=np.uint8)
    mask[5, 5] = 255

    for _ in range(5):
        acc.add(mask)
    assert acc.counts[5, 5] == 5

    acc.add(mask)
    assert acc.counts[5, 5] == 6  # kept growing, no implicit decay

    acc.reset()
    assert int(acc.counts.max()) == 0
    alpha = _decode_alpha(acc.heatmap_png())
    assert int(alpha.max()) == 0


def test_resizes_a_differently_shaped_mask_to_its_own_resolution(tmp_path):
    acc = MotionAccumulator(tmp_path / "heatmap.npy", width=10, height=10)
    big_mask = np.zeros((100, 100), dtype=np.uint8)
    big_mask[40:60, 40:60] = 255  # roughly the center

    acc.add(big_mask)  # must not raise despite the resolution mismatch

    assert acc.counts.shape == (10, 10)
    assert acc.counts[5, 5] > 0


def test_persists_across_instances(tmp_path):
    path = tmp_path / "heatmap.npy"
    acc = MotionAccumulator(path, width=8, height=8)
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[1, 1] = 255
    acc.add(mask)
    acc.save()

    reloaded = MotionAccumulator(path, width=8, height=8)
    assert reloaded.counts[1, 1] == 1


def test_resolution_change_starts_fresh_instead_of_crashing(tmp_path):
    path = tmp_path / "heatmap.npy"
    acc = MotionAccumulator(path, width=8, height=8)
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[1, 1] = 255
    acc.add(mask)
    acc.save()

    resized = MotionAccumulator(path, width=16, height=12)  # e.g. analysis_width setting changed
    assert resized.counts.shape == (12, 16)
    assert int(resized.counts.max()) == 0
