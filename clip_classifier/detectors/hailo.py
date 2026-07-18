"""Hailo-8L object detector -- optional backend, only ever constructed once
``hailo_probe.probe_hailo()`` confirms the device, HailoRT bindings, and HEF
file are all present (see ../backend.py). Kept import-optional: nothing at
module level here requires ``hailo_platform`` to be installed, so a
CPU-only deployment never needs it.

**Validation note**: this targets HailoRT's standard Python API
(``VDevice`` / ``HEF`` / ``InferVStreams``) as documented by Hailo, but
could not be exercised against real Hailo-8L hardware or HailoRT while
this was written (no device/runtime available in this environment -- see
hailo_probe.py, which every code path here is guarded by). Tests mock the
``hailo_platform`` module entirely, per the task's own instructions; a real
deployer should smoke-test this against their actual hardware/HailoRT
version before relying on the Hailo backend in production, and adjust the
output-decoding in ``_decode_raw_output`` if their installed HailoRT/HEF
combination shapes its output differently than assumed below.

**HEF compilation requirement**: compile the YOLOv8-family HEF *without*
Hailo's built-in NMS postprocessing (the Hailo Model Zoo's export tooling
offers this as an option), so this backend receives the same raw
(4 + num_classes, num_anchors) tensor shape the CPU/ONNX Runtime backend
does and can reuse the exact same, already-tested decode logic
(``detectors.cpu.postprocess``) -- rather than needing separate, harder to
verify code to parse whatever shape HailoRT's own on-device postprocessing
would otherwise emit.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import numpy as np

from ..detector import Detection
from .cpu import DEFAULT_CONFIDENCE_FLOOR, INPUT_SIZE, model_file_hash, postprocess, preprocess

logger = logging.getLogger(__name__)


class HailoModelLoadError(Exception):
    """Raised when the HEF file is missing or the Hailo device fails to initialize."""


class HailoYolov8Detector:
    name = "hailo"
    model = "yolov8n"

    def __init__(self, hef_path: Path, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR):
        # Imported here, not at module level -- see the module docstring.
        from hailo_platform import HEF, ConfigureParams, HailoStreamInterface, InferVStreams, VDevice

        if not hef_path.is_file():
            raise HailoModelLoadError(f"Hailo HEF file not found: {hef_path}")

        try:
            self._hef = HEF(str(hef_path))
            self._device = VDevice()
            configure_params = ConfigureParams.create_from_hef(
                self._hef, interface=HailoStreamInterface.PCIe
            )
            self._network_group = self._device.configure(self._hef, configure_params)[0]
            self._network_group_params = self._network_group.create_params()
            self._input_vstream_info = self._hef.get_input_vstream_infos()[0]
            self._output_vstream_info = self._hef.get_output_vstream_infos()[0]
        except Exception as e:
            raise HailoModelLoadError(f"Failed to initialize the Hailo-8L device with {hef_path}: {e}") from e

        self._InferVStreams = InferVStreams
        self._confidence_floor = confidence_floor
        self.model_version = model_file_hash(hef_path)

    def detect(self, frame: np.ndarray) -> List[Detection]:
        blob, scale, pad_x, pad_y = preprocess(frame, size=INPUT_SIZE)
        # HailoRT vstreams take HWC, not the NCHW our shared preprocess()
        # produces for ONNX Runtime -- undo the transpose/batch-dim it
        # added, keep the [0, 1] letterboxed image.
        hwc_input = blob[0].transpose(1, 2, 0)

        with self._InferVStreams(self._network_group, self._network_group_params) as infer_pipeline:
            with self._network_group.activate(self._network_group_params):
                results = infer_pipeline.infer({self._input_vstream_info.name: hwc_input[np.newaxis, ...]})

        raw_output = results[self._output_vstream_info.name]
        orig_height, orig_width = frame.shape[:2]
        return postprocess(
            np.asarray(raw_output), scale, pad_x, pad_y, orig_width, orig_height, self._confidence_floor
        )
