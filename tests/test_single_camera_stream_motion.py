import numpy as np

from single_camera_stream.motion import MotionDetector


def test_no_motion_on_a_static_background():
    detector = MotionDetector(analysis_width=64, min_area=10, var_threshold=16, history=20)
    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    result = detector.process(background)
    assert not result.motion_detected
    assert result.box is None


def test_motion_detected_on_a_real_change():
    detector = MotionDetector(analysis_width=64, min_area=10, var_threshold=16, history=20)
    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    moving = background.copy()
    moving[10:30, 10:30] = 255
    result = detector.process(moving)

    assert result.motion_detected
    assert result.score > 0
    assert result.box is not None


def test_box_covers_the_moved_region():
    detector = MotionDetector(analysis_width=64, min_area=10, var_threshold=16, history=20)
    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    moving = background.copy()
    moving[10:30, 10:30] = 255
    result = detector.process(moving)

    x, y, w, h = result.box
    center_x, center_y = x + w / 2, y + h / 2
    assert 15 <= center_x <= 25 and 15 <= center_y <= 25


def test_a_small_change_below_min_area_is_ignored():
    detector = MotionDetector(analysis_width=64, min_area=5000, var_threshold=16, history=20)
    background = np.zeros((64, 64, 3), dtype=np.uint8)
    for _ in range(15):
        detector.process(background)

    moving = background.copy()
    moving[10:15, 10:15] = 255  # tiny 5x5 change, well under min_area
    result = detector.process(moving)

    assert not result.motion_detected


def test_warmup_frames_never_report_motion_even_with_a_real_change():
    # MOG2's background model hasn't stabilized yet -- the first several
    # frames must never trigger a false positive.
    detector = MotionDetector(analysis_width=64, min_area=10, var_threshold=16, history=100)
    background = np.zeros((64, 64, 3), dtype=np.uint8)

    moving = background.copy()
    moving[10:30, 10:30] = 255
    result = detector.process(moving)  # very first frame -- still warming up

    assert not result.motion_detected


def test_analysis_size_reflects_the_downscale():
    detector = MotionDetector(analysis_width=32, min_area=10, var_threshold=16, history=20)
    frame = np.zeros((128, 256, 3), dtype=np.uint8)  # 2:1 aspect ratio
    result = detector.process(frame)
    assert result.analysis_size == (32, 16)
