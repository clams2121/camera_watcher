from single_camera_stream.frame_buffer import FrameBuffer


def test_append_and_latest():
    fb = FrameBuffer(max_seconds=10)
    fb.append(1.0, "frame-a")
    fb.append(2.0, "frame-b")
    assert fb.latest() == (2.0, "frame-b")


def test_latest_on_an_empty_buffer_is_none():
    fb = FrameBuffer(max_seconds=10)
    assert fb.latest() is None


def test_old_frames_are_pruned_past_max_seconds():
    fb = FrameBuffer(max_seconds=5)
    fb.append(0.0, "old")
    fb.append(6.0, "new")  # pushes "old" (age 6s) past the 5s window
    assert len(fb) == 1
    assert fb.latest() == (6.0, "new")


def test_since_returns_frames_at_or_after_the_given_timestamp():
    fb = FrameBuffer(max_seconds=100)
    for i in range(5):
        fb.append(float(i), f"frame-{i}")
    result = fb.since(2.0)
    assert [ts for ts, _ in result] == [2.0, 3.0, 4.0]


def test_measured_fps_with_fewer_than_two_frames_is_none():
    fb = FrameBuffer(max_seconds=10)
    assert fb.measured_fps() is None
    fb.append(1.0, "a")
    assert fb.measured_fps() is None


def test_measured_fps_reflects_the_actual_interval():
    fb = FrameBuffer(max_seconds=10)
    for i in range(11):  # 10 intervals of 0.1s -> 10 fps
        fb.append(i * 0.1, f"frame-{i}")
    assert abs(fb.measured_fps() - 10.0) < 0.01
