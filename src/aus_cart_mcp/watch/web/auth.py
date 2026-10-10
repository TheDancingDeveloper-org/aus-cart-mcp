"""Web UI login: one household password (scrypt hash in the environment) and an HMAC-signed session cookie."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

COOKIE = "acw_session"
SESSION_SECONDS = 30 * 24 * 3600
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """`scrypt:<salt b64>:<hash b64>`, for AUS_CARTWATCH_UI_PASSWORD_HASH.

    `:` rather than `$` as the separator: docker compose interpolates `$` in env values.
    """
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt:{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.replace("$", ":").split(":")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=_N, r=_R, p=_P, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue(secret: str, *, now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_SECONDS)
    payload = f"{expires}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(secret, payload)}"


def valid(secret: str, token: str | None, *, now: float | None = None) -> bool:
    if not secret or not token or token.count(".") != 2:
        return False
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return False
    try:
        return int(payload.split(".")[0]) > (now or time.time())
    except ValueError:
        return False
