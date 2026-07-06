"""Environment-driven configuration. No hardcoded hosts.

Load order: real environment vars win; a local .env file is loaded if present
(python-dotenv) so the wrapper/relay can be run from a project .env during
local development. Everything is portable so the relay can move to a VPS with
only env changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int) -> int:
    v = os.environ.get(key)
    return int(v) if v and v.strip() else default


def _float(key: str, default: float) -> float:
    v = os.environ.get(key)
    return float(v) if v and v.strip() else default


@dataclass
class RelayConfig:
    host: str = field(default_factory=lambda: _env("RELAY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("RELAY_PORT", 8765))
    client_token: str = field(default_factory=lambda: _env("CLIENT_TOKEN", "change-me-client"))
    wrapper_token: str = field(default_factory=lambda: _env("WRAPPER_TOKEN", "change-me-wrapper"))
    # Optional path to a built web client (web/dist) to serve from the same origin.
    static_dir: str = field(default_factory=lambda: _env("WEB_STATIC_DIR", ""))
    # Soft cap for per-client delta shedding. Must exceed the ring-buffer size
    # (RING_MAX_EVENTS, default 2000) so a full replay never sheds. The queue is
    # unbounded below this — replay integrity > memory.
    client_queue_cap: int = field(default_factory=lambda: _int("CLIENT_QUEUE_CAP", 4096))
    # Login gate: web clients must POST /api/login with this password to get a
    # short-lived HMAC session token (stored in localStorage), replacing the
    # static CLIENT_TOKEN that used to be baked into the JS bundle.
    login_password: str = field(default_factory=lambda: _env("LOGIN_PASSWORD", ""))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET", ""))
    session_ttl_seconds: int = field(default_factory=lambda: _int("SESSION_TTL_SECONDS", 7 * 24 * 3600))


@dataclass
class WrapperConfig:
    relay_url: str = field(default_factory=lambda: _env("RELAY_URL", "ws://127.0.0.1:8765/ws"))
    # Token the wrapper presents to the relay at WS upgrade (must match the
    # relay's WRAPPER_TOKEN). Same env name as the relay for convenience.
    wrapper_token: str = field(default_factory=lambda: _env("WRAPPER_TOKEN", "change-me-wrapper"))
    # cwd for the cc session. MUST match the resumed session's cwd, otherwise
    # --resume cannot locate the session jsonl under ~/.claude/projects/.
    cc_cwd: str = field(default_factory=lambda: _env("CC_CWD", os.getcwd()))
    resume_session_id: str = field(default_factory=lambda: _env("CC_RESUME_SESSION_ID", ""))
    ring_max_events: int = field(default_factory=lambda: _int("RING_MAX_EVENTS", 10000))
    ring_max_bytes: int = field(default_factory=lambda: _int("RING_MAX_BYTES", 24 * 1024 * 1024))
    tool_result_max: int = field(default_factory=lambda: _int("TOOL_RESULT_MAX", 65536))
    # Seconds to wait for the terminal ResultMessage after interrupt() before
    # forcing an SDK reconnect (drain safety net).
    drain_timeout: float = field(default_factory=lambda: _float("DRAIN_TIMEOUT", 15.0))
    state_dir: Path = field(default_factory=lambda: Path(_env("CC_REMOTE_STATE_DIR", str(Path.home() / ".cc-remote"))))


def relay_config() -> RelayConfig:
    return RelayConfig()


def wrapper_config() -> WrapperConfig:
    return WrapperConfig()
