"""Stitches a slice of cached passthrough segments (see segment_cache.py)
into one final clip via ffmpeg's concat demuxer with ``-c copy`` -- zero
re-encoding, so the recorded clip's codec/resolution/bitrate exactly match
whatever the camera sent.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List


class AssemblyError(Exception):
    """Raised when ffmpeg fails to concatenate the given segments."""


def assemble_clip(segments: List[Path], output_path: Path) -> None:
    """Concatenates ``segments`` (in the given order) into ``output_path``.

    Writes directly to ``output_path`` -- callers that need atomic
    finalization (so a reader never sees a half-written file under its final
    name) should pass a temporary path and rename it themselves once this
    returns.
    """
    if not segments:
        raise AssemblyError(f"No cached segments available to assemble {output_path.name}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, list_path_str = tempfile.mkstemp(suffix=".txt", dir=str(output_path.parent))
    list_path = Path(list_path_str)
    try:
        with open(fd, "w") as list_file:
            for seg in segments:
                # Concat demuxer file syntax: single-quoted, with embedded
                # single quotes escaped by closing/reopening the quote.
                escaped = str(seg.resolve()).replace("'", "'\\''")
                list_file.write(f"file '{escaped}'\n")

        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        # ffmpeg's concat demuxer can exit 0 even after hitting a real error
        # mid-stream (e.g. a listed segment vanishing) as long as it managed
        # to write *something* first -- at -loglevel error, any stderr output
        # at all is a reliable sign something actually went wrong, not just a
        # non-zero exit code.
        if result.returncode != 0 or result.stderr.strip() or not output_path.exists():
            raise AssemblyError(
                f"ffmpeg failed to assemble {output_path.name} from {len(segments)} segment(s): "
                f"{result.stderr.strip()[-2000:]}"
            )
    finally:
        list_path.unlink(missing_ok=True)
