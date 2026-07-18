"""Fetches and verifies the CPU backend's YOLOv8n ONNX model.

Never runs automatically -- the CPU detector (detectors/cpu.py) fails loud
with this module's own invocation as the fix-it command if the model file
isn't already there. Run it once, offline from the classifier service:

    python -m clip_classifier.fetch_model --config config/classifier.yaml

MODEL_URL / MODEL_SHA256 below are the pin: every download is verified
against MODEL_SHA256 before being installed, and a mismatch is refused
loudly rather than silently used -- a corrupted download or a compromised
mirror never becomes "the" model just because a file showed up.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

from .config import Config, ConfigError

logger = logging.getLogger(__name__)

MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.onnx"

# Deliberately blank. This project was built in a sandboxed environment
# whose outbound network policy blocks GitHub's release-asset CDN (see
# /root/.ccr/README.md's "403/407 from the proxy" section, if you're
# looking at a similar setup) -- MODEL_URL could not actually be reached to
# compute and verify a checksum before shipping this. Before relying on the
# CPU backend in a real deployment:
#   1. From a network that CAN reach MODEL_URL, run:
#        python -m clip_classifier.fetch_model --print-hash-only
#   2. Confirm you trust that source and the printed hash.
#   3. Hard-code the printed SHA-256 as MODEL_SHA256 below.
# Until this is filled in, fetch_and_verify() refuses to install a
# downloaded file as "the" model at all -- see below.
MODEL_SHA256 = ""


class FetchError(Exception):
    """Raised for any problem fetching or verifying the model file."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path, timeout: float = 120) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
                shutil.copyfileobj(response, tmp)
                tmp_path = Path(tmp.name)
    except OSError as e:
        raise FetchError(f"Failed to download {url}: {e}") from e

    try:
        tmp_path.replace(dest)
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        raise FetchError(f"Failed to install downloaded file to {dest}: {e}") from e


def fetch_and_verify(model_path: Path) -> None:
    if not MODEL_SHA256:
        raise FetchError(
            "MODEL_SHA256 is not pinned in clip_classifier/fetch_model.py -- refusing to install an "
            "unverified model file. Run:\n"
            "    python -m clip_classifier.fetch_model --print-hash-only\n"
            "from a network that can reach MODEL_URL, confirm you trust the source, then hard-code the "
            "printed SHA-256 as MODEL_SHA256 before running this for real."
        )
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_download = Path(tmp_dir) / "download"
        download(MODEL_URL, tmp_download)
        actual = _sha256(tmp_download)
        if actual != MODEL_SHA256:
            raise FetchError(
                f"Checksum mismatch for {MODEL_URL}: expected {MODEL_SHA256}, got {actual}. Refusing to "
                f"install a model file that doesn't match its pin -- the download may be corrupted, or "
                f"the file at that URL may have changed since this was pinned."
            )
        model_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_download), str(model_path))


def _default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "classifier.yaml"


def _parse_args():
    parser = argparse.ArgumentParser(description="Fetch and verify the CPU backend's YOLOv8n ONNX model.")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to classifier.yaml -- determines where the model is installed (cpu.model_path). "
        f"Defaults to {_default_config_path()}.",
    )
    parser.add_argument(
        "--print-hash-only",
        action="store_true",
        help="Download to a temp location, print its SHA-256, and exit without installing anything -- "
        "for pinning MODEL_SHA256 the first time.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()

    if args.print_hash_only:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_download = Path(tmp_dir) / "download"
            try:
                download(MODEL_URL, tmp_download)
            except FetchError as e:
                print(f"clip_classifier.fetch_model: {e}", file=sys.stderr)
                raise SystemExit(1)
            print(_sha256(tmp_download))
        return

    config_path = Path(args.config) if args.config else _default_config_path()
    try:
        config = Config(config_path)
    except ConfigError as e:
        print(f"clip_classifier.fetch_model: {e}", file=sys.stderr)
        raise SystemExit(1)

    model_path = Path(config.resolved()["cpu"]["model_path"])
    try:
        fetch_and_verify(model_path)
    except FetchError as e:
        print(f"clip_classifier.fetch_model: {e}", file=sys.stderr)
        raise SystemExit(1)

    print(f"clip_classifier.fetch_model: installed verified model to {model_path}")


if __name__ == "__main__":
    main()
