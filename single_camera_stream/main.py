"""Entrypoint: capture, motion detection, and recording for one camera.

Usage:
    python -m single_camera_stream.main                    # uses config.yaml next to this file
    python -m single_camera_stream.main --config other.yaml

On startup, acquires a singleton lock (a PID file next to whatever config
was loaded -- see pidlock.py) so a second instance pointed at the same
config directory refuses to start instead of two processes fighting over
the same output files.
"""
from __future__ import annotations

# Checked before any of this package's own modules that need it, so a
# missing dependency produces a clear message instead of a raw
# ImportError traceback.
from .dependency_check import check_dependencies

check_dependencies()

import argparse
import logging
import os
import queue
import signal
import sys
import threading
from pathlib import Path

from . import pidlock
from .capture import RtspCapture
from .config import Config, ConfigError
from .frame_buffer import FrameBuffer
from .motion import MotionDetector
from .recorder import RecorderConfig, SingleStreamRecorder

logger = logging.getLogger(__name__)

PID_FILENAME = "single_camera_stream.pid"

# Extra headroom above pre_buffer_seconds so the frame buffer always has
# enough history to serve a full pre-roll even with some scheduling jitter.
_FRAME_BUFFER_MARGIN_SECONDS = 5.0


def _parse_args():
    parser = argparse.ArgumentParser(description="Watch one RTSP camera and record motion clips.")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml. Defaults to config.yaml next to this file.",
    )
    return parser.parse_args()


def _fail(message: str) -> None:
    print(f"single_camera_stream: {message}", file=sys.stderr)
    raise SystemExit(1)


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def _build_recorder_config(config: Config) -> RecorderConfig:
    settings = config.settings
    motion_cfg = settings["motion"]
    rec_cfg = settings["recording"]
    return RecorderConfig(
        output_dir=config.output_dir,
        camera_name=settings["camera"]["name"],
        pre_buffer_seconds=rec_cfg["pre_buffer_seconds"],
        post_buffer_seconds=rec_cfg["post_buffer_seconds"],
        max_chunk_seconds=rec_cfg["max_chunk_seconds"],
        overlap_seconds=rec_cfg["overlap_seconds"],
        fallback_fps=rec_cfg["fallback_fps"],
        draw_bounding_box=motion_cfg["draw_bounding_box"],
        box_padding_px=motion_cfg["box_padding_px"],
    )


def run(config: Config, stop_event: threading.Event) -> None:
    """Wires capture -> a bounded processing queue -> motion detection ->
    the recorder, and blocks until `stop_event` is set. Decoupling capture
    from processing via a queue means a slow disk write (recording) can
    never stall the RTSP read loop -- if processing falls behind, the
    queue sheds the oldest-pending frames rather than growing without
    bound or blocking capture."""
    settings = config.settings
    motion_cfg = settings["motion"]
    rec_cfg = settings["recording"]

    frame_buffer = FrameBuffer(max_seconds=rec_cfg["pre_buffer_seconds"] + _FRAME_BUFFER_MARGIN_SECONDS)
    motion_detector = MotionDetector(
        analysis_width=motion_cfg["analysis_width"],
        min_area=motion_cfg["min_area"],
        var_threshold=motion_cfg["var_threshold"],
        history=motion_cfg["history"],
    )
    recorder = SingleStreamRecorder(frame_buffer, _build_recorder_config(config))

    frame_queue: "queue.Queue" = queue.Queue(maxsize=128)
    dropped_frames = 0

    def on_frame(timestamp: float, frame) -> None:
        nonlocal dropped_frames
        frame_buffer.append(timestamp, frame)
        try:
            frame_queue.put_nowait((timestamp, frame))
        except queue.Full:
            dropped_frames += 1
            if dropped_frames == 1 or dropped_frames % 50 == 0:
                logger.warning("Frame processing is falling behind; dropped %d frame(s) so far", dropped_frames)

    capture = RtspCapture(url_factory=config.rtsp_url, on_frame=on_frame, transport=settings["camera"]["transport"])

    process_stop = threading.Event()

    def _process_loop() -> None:
        while True:
            try:
                timestamp, frame = frame_queue.get(timeout=0.2)
            except queue.Empty:
                if process_stop.is_set():
                    return
                continue

            motion_detected, box, analysis_size = False, None, None
            if motion_cfg["enabled"]:
                try:
                    result = motion_detector.process(frame)
                    motion_detected, box, analysis_size = result.motion_detected, result.box, result.analysis_size
                except Exception:
                    logger.exception("Motion detection failed on a frame")

            try:
                recorder.handle_frame(timestamp, frame, motion_detected, box=box, analysis_size=analysis_size)
            except Exception:
                logger.exception("Recording failed on a frame")

    process_thread = threading.Thread(target=_process_loop, name="frame-processor", daemon=True)

    logger.info("Starting capture from %s", config.redacted_rtsp_url())
    process_thread.start()
    capture.start()
    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
    finally:
        logger.info("Stopping...")
        capture.stop()
        process_stop.set()
        process_thread.join(timeout=5)
        recorder.stop()
        logger.info("Stopped.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()

    config_path = Path(args.config) if args.config else _default_config_path()
    try:
        config = Config(config_path)
    except ConfigError as e:
        _fail(str(e))
        return  # unreachable; keeps type checkers happy about `config` below

    pid_file = config.config_dir / PID_FILENAME
    try:
        pidlock.acquire(pid_file)
    except pidlock.AlreadyRunningError as e:
        _fail(str(e))
        return
    logger.info("Acquired singleton lock %s (pid %d)", pid_file, os.getpid())

    stop_event = threading.Event()

    def _shutdown(signum, frame):
        logger.info("Received signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        run(config, stop_event)
    finally:
        pidlock.release(pid_file)


if __name__ == "__main__":
    main()
