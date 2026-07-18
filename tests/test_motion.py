import numpy as np

from camera_watcher.mask import MaskStore
from camera_watcher.motion import MotionDetector


def test_motion_detected_on_change(tmp_path):
    mask_store = MaskStore(tmp_path / "mask.json")
    detector = MotionDetector(mask_store, analysis_width=64, min_area=10, var_threshold=16, history=20)

    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    moving = background.copy()
    moving[10:30, 10:30] = 255
    result = detector.process(moving)
    assert result.motion_detected


def test_motion_ignored_within_mask(tmp_path):
    mask_store = MaskStore(tmp_path / "mask.json")
    mask_store.save([[[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]])  # ignore top-left quadrant

    detector = MotionDetector(mask_store, analysis_width=64, min_area=10, var_threshold=16, history=20)

    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    moving = background.copy()
    moving[5:20, 5:20] = 255  # inside the ignored quadrant
    result = detector.process(moving)
    assert not result.motion_detected


def test_boxes_are_returned_for_detected_contours(tmp_path):
    mask_store = MaskStore(tmp_path / "mask.json")
    detector = MotionDetector(mask_store, analysis_width=64, min_area=10, var_threshold=16, history=20)

    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    moving = background.copy()
    moving[10:30, 10:30] = 255
    result = detector.process(moving)

    assert result.analysis_size == (64, 64)  # no resize distortion at this size
    assert len(result.boxes) == 1
    x, y, w, h = result.boxes[0]
    # Blur + dilation grow the contour a bit beyond the exact 20x20 square,
    # so check the box is in the right neighborhood rather than pixel-exact.
    center_x, center_y = x + w / 2, y + h / 2
    assert 15 <= center_x <= 25 and 15 <= center_y <= 25
    assert 10 <= w <= 32 and 10 <= h <= 32


def test_raw_foreground_shows_motion_even_inside_the_ignore_mask(tmp_path):
    """The accumulator/heatmap is meant to reveal motion even in currently
    ignored zones (so a user can decide whether to extend one), so
    raw_foreground must not have the ignore mask applied -- unlike boxes/
    motion_detected, which must respect it."""
    mask_store = MaskStore(tmp_path / "mask.json")
    mask_store.save([[[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]])  # ignore top-left quadrant

    detector = MotionDetector(mask_store, analysis_width=64, min_area=10, var_threshold=16, history=20)

    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    moving = background.copy()
    moving[5:20, 5:20] = 255  # inside the ignored quadrant
    result = detector.process(moving)

    assert not result.motion_detected  # respects the mask
    assert result.boxes == []
    assert result.raw_foreground is not None
    assert result.raw_foreground[10, 10] > 0  # but the raw signal is still there
