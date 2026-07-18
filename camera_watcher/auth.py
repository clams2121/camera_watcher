"""Token authentication for the web UI -- see web/routes.py for the Flask
wiring (the ``before_request`` guard, /api/login, /api/logout).

Every camera process requires its own auth token, stored in that camera's
``<name>.secrets.yaml`` under ``web.auth_token`` -- never generated or
persisted automatically (that would be exactly the kind of silent fallback
this project avoids elsewhere). A missing or too-short token fails startup
loud, in main.py, rather than silently running the web UI unauthenticated.
"""
from __future__ import annotations

import hmac
import secrets as secrets_module

TOKEN_MIN_LENGTH = 32


class AuthConfigError(Exception):
    """Raised when the configured auth token is missing or too weak to rely on."""


def generate_token() -> str:
    """A new, sufficiently random token for an operator to paste into
    <camera>.secrets.yaml -- never called automatically."""
    return secrets_module.token_urlsafe(32)


def require_token(secrets: dict) -> str:
    """Returns the configured auth token, or raises AuthConfigError with a
    fix-it message if it's missing or too short to be a meaningful secret."""
    token = (secrets.get("web") or {}).get("auth_token") or ""
    if len(token) < TOKEN_MIN_LENGTH:
        raise AuthConfigError(
            f"web.auth_token is missing or too short (needs >= {TOKEN_MIN_LENGTH} characters) "
            "in this camera's *.secrets.yaml file. Generate one:\n"
            '    python -c "from camera_watcher.auth import generate_token; print(generate_token())"\n'
            "then add it under `web:` in <camera>.secrets.yaml:\n"
            "    web:\n"
            '      auth_token: "<paste the generated token here>"\n'
        )
    return token


def tokens_match(candidate: str, expected: str) -> bool:
    """Constant-time comparison -- never use `==` on secrets."""
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
