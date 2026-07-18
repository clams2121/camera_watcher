"""Rebuilds a flat, sorted index of every finalized clip + its companion
metadata sidecar (see recorder.py's _write_metadata) under a clips
directory -- useful after copying/reorganizing clips onto a new machine, or
before pointing any later analysis stage at a directory for the first time.

Usage:
    python -m camera_watcher.rebuild_index <clips_dir> [--index-path PATH]

No third-party dependencies -- runs standalone with just Python 3, same as
retention.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator

from .constants import TEMP_SUFFIX


def _iter_metadata(clips_dir: Path) -> Iterator[dict]:
    for json_path in sorted(clips_dir.glob("*.json")):
        video_path = json_path.with_suffix(".mp4")
        if not video_path.exists():
            print(
                f"camera_watcher.rebuild_index: WARNING: {json_path.name} has no matching clip -- skipping",
                file=sys.stderr,
            )
            continue
        try:
            metadata = json.loads(json_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"camera_watcher.rebuild_index: WARNING: failed to read {json_path.name}: {e} -- skipping",
                file=sys.stderr,
            )
            continue
        yield metadata


def _warn_about_orphaned_clips(clips_dir: Path) -> None:
    for p in sorted(clips_dir.glob("*.mp4")):
        if p.name.endswith(TEMP_SUFFIX):
            continue
        if not p.with_suffix(".json").exists():
            print(
                f"camera_watcher.rebuild_index: WARNING: {p.name} has no metadata sidecar -- skipping",
                file=sys.stderr,
            )


def rebuild_index(clips_dir: Path, index_path: Path) -> int:
    """Writes a start_time-sorted, one-record-per-line index of every clip's
    metadata to `index_path`. Returns the number of entries written."""
    entries = sorted(_iter_metadata(clips_dir), key=lambda m: m.get("start_time") or "")
    _warn_about_orphaned_clips(clips_dir)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    with tmp_path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    tmp_path.replace(index_path)
    return len(entries)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("clips_dir", type=Path)
    parser.add_argument(
        "--index-path", type=Path, default=None, help="Defaults to <clips_dir>/index.jsonl"
    )
    args = parser.parse_args()

    clips_dir = args.clips_dir.resolve()
    if not clips_dir.is_dir():
        print(f"camera_watcher.rebuild_index: {clips_dir} is not a directory", file=sys.stderr)
        raise SystemExit(1)

    index_path = (args.index_path or (clips_dir / "index.jsonl")).resolve()
    count = rebuild_index(clips_dir, index_path)
    print(f"camera_watcher.rebuild_index: wrote {count} entries to {index_path}")


if __name__ == "__main__":
    main()
