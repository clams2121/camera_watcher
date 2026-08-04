"""Token authentication for the web UI -- see web/routes.py for the Flask
wiring (the ``before_request`` guard, /api/login, /api/logout).

The fleet supervisor process has exactly one auth token for its one web UI,
stored in ``config/fleet.secrets.yaml`` under ``web.auth_token``. Unlike
per-camera secrets, this one *is* generated automatically -- see
fleet.py's ``FleetConfig`` -- the first time the process runs with none
configured, specifically so the always-on UI never needs any manual setup
before it can be reached. ``require_token`` below is still the fail-loud
safety net for the case where the configured token is present but too weak
to be a meaningful secret (e.g. hand-edited down to something short).
"""
from __future__ import annotations

import hmac
import secrets as secrets_module

TOKEN_MIN_LENGTH = 32


class AuthConfigError(Exception):
    """Raised when the configured auth token is missing or too weak to rely on."""


def generate_token() -> str:
    """A new, sufficiently random token -- used both by FleetConfig's
    automatic first-boot bootstrap and for manual rotation from the UI."""
    return secrets_module.token_urlsafe(32)


def require_token(secrets: dict) -> str:
    """Returns the configured auth token, or raises AuthConfigError with a
    fix-it message if it's missing or too short to be a meaningful secret.
    In normal operation FleetConfig's bootstrap means this should never
    actually fire -- it's the fail-loud safety net for a fleet.secrets.yaml
    hand-edited down to something too weak to rely on."""
    token = (secrets.get("web") or {}).get("auth_token") or ""
    if len(token) < TOKEN_MIN_LENGTH:
        raise AuthConfigError(
            f"web.auth_token is missing or too short (needs >= {TOKEN_MIN_LENGTH} characters) "
            "in config/fleet.secrets.yaml. Generate one:\n"
            '    python -c "from camera_watcher.auth import generate_token; print(generate_token())"\n'
            "then add it under `web:` in config/fleet.secrets.yaml:\n"
            "    web:\n"
            '      auth_token: "<paste the generated token here>"\n'
        )
    return token


def tokens_match(candidate: str, expected: str) -> bool:
    """Constant-time comparison -- never use `==` on secrets."""
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
