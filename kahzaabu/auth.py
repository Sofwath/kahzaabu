# SPDX-License-Identifier: Apache-2.0
"""Password hashing + session-cookie auth for the web admin.

Session strategy: signed HMAC cookie carrying (username, role, issued_at).
No DB session store — purely stateless. Logout = clear the cookie.

Secret comes from $KAHZAABU_SECRET_KEY env var; falls back to a generated
file at ~/.config/kahzaabu/session_secret on first launch.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

# Silence the harmless passlib bcrypt-version-detection warning
# (bcrypt >=4.1 dropped __about__; passlib 1.7.4 catches the AttributeError
# but still emits a Traceback at WARNING level).
logging.getLogger("passlib.handlers.bcrypt").setLevel(logging.ERROR)

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

SESSION_COOKIE = "kahzaabu_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 1 week

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def _secret_path() -> Path:
    return Path.home() / ".config" / "kahzaabu" / "session_secret"


def _load_or_create_secret() -> str:
    env = os.environ.get("KAHZAABU_SECRET_KEY")
    if env:
        return env
    p = _secret_path()
    if p.exists():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    p.write_text(secret)
    p.chmod(0o600)
    return secret


_serializer: Optional[URLSafeTimedSerializer] = None


def serializer() -> URLSafeTimedSerializer:
    """URL-safe serializer — encodes payload to base64 so it's cookie-safe.

    Previously we used TimestampSigner with raw JSON, which produced cookies
    containing commas and quotes that cookie jars mangled on round-trip.
    URLSafeTimedSerializer handles encoding for us.
    """
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_load_or_create_secret(),
                                              salt="kahzaabu-session")
    return _serializer


def sign_session(username: str, role: str) -> str:
    return serializer().dumps({"u": username, "r": role})


def verify_session(token: str) -> Optional[dict]:
    """Return session dict {u: username, r: role} or None."""
    if not token:
        return None
    try:
        d = serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(d, dict) or "u" not in d:
        return None
    return d
