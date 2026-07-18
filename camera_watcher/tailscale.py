"""Resolves this host's Tailscale IPv4 address for binding the web UI to.

Fleet cameras bind to Tailscale only, never a public/LAN interface -- see
main.py, which calls this when ``web.host == "tailscale"``. Every failure
mode here raises :class:`TailscaleError` with a plain-English fix; there is
no fallback to ``0.0.0.0`` anywhere in this path.
"""
from __future__ import annotations

import re
import subprocess

# Tailscale assigns addresses out of the CGNAT range 100.64.0.0/10
# (100.64.0.0 - 100.127.255.255) -- reject anything else outright rather
# than trusting whatever the CLI printed.
_TAILSCALE_IP_RE = re.compile(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}$")


class TailscaleError(Exception):
    """Raised when the Tailscale IPv4 address can't be resolved."""


def resolve_tailscale_ip(timeout: float = 10) -> str:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        raise TailscaleError(
            'web.host is "tailscale" but the `tailscale` CLI isn\'t installed or isn\'t on PATH. '
            "Install Tailscale (https://tailscale.com/download), or set web.host to an explicit "
            "address in the camera's config if you really want to bind somewhere else."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise TailscaleError(f"`tailscale ip -4` timed out after {timeout}s") from e

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise TailscaleError(
            f"`tailscale ip -4` failed: {detail}. Is this machine logged into Tailscale (`tailscale up`)?"
        )

    stdout = result.stdout.strip()
    ip = stdout.splitlines()[0].strip() if stdout else ""
    if not _TAILSCALE_IP_RE.match(ip):
        raise TailscaleError(
            f"`tailscale ip -4` returned {ip!r}, which doesn't look like a Tailscale address "
            "(expected something in 100.64.0.0/10) -- refusing to bind to it."
        )
    return ip
