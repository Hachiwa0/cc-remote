"""Authentication: Bearer token (wrapper) + password login -> HMAC session
token (web clients). Session tokens replace the static CLIENT_TOKEN that used
to be baked into the web bundle — now a browser must log in to get one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from cc_remote.config import RelayConfig


def authenticate_token(token: str, cfg: RelayConfig) -> Optional[str]:
    """Validate a bare token string -> 'wrapper' | 'client' | None (legacy static)."""
    if not token:
        return None
    if hmac.compare_digest(token, cfg.wrapper_token):
        return "wrapper"
    if hmac.compare_digest(token, cfg.client_token):
        return "client"
    return None


def authenticate(auth_header: str, cfg: RelayConfig) -> Optional[str]:
    """Validate a Bearer Authorization header -> 'wrapper' | 'client' | None."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    return authenticate_token(auth_header[7:].strip(), cfg)


def verify_password(password: str, expected: str) -> bool:
    """Constant-time password check. Fails closed if no password is configured."""
    if not expected or not password:
        return False
    return hmac.compare_digest(password, expected)


def make_session_token(secret: str, ttl: int) -> tuple[str, int]:
    """Sign a session token: base64(payload).base64(HMAC(payload)). Returns
    (token, exp_epoch)."""
    exp = int(time.time()) + ttl
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    return f"{payload}.{sig}", exp


def verify_session_token(token: str, secret: str) -> bool:
    """Verify HMAC signature + expiry."""
    if not token or not secret:
        return False
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return False
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("exp", 0)) > time.time()
    except Exception:
        return False
