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
