"""List Codex sessions from ~/.codex/sessions rollout files.

The Codex analog of the SDK's list_sessions (which only knows Claude sessions
under ~/.claude/projects/). Codex writes one rollout .jsonl per thread under
~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl; the first line is a
`session_meta` record carrying the thread id + cwd. Read-only + best-effort.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from cc_remote.log import logger

log = logger("cc_remote.wrapper.codex_sessions")

_CONFIG = os.path.expanduser("~/.codex/config.toml")

_ROOT = os.path.expanduser("~/.codex/sessions")
_GLOB = os.path.join(_ROOT, "**", "rollout-*.jsonl")


def list_codex_sessions(limit: int = 60) -> list[dict]:
    """Most-recently-modified Codex sessions -> [{session_id, cwd, last_modified,
    first_prompt}]. Sorted newest first."""
    try:
        files = glob.glob(_GLOB, recursive=True)
    except Exception:
        return []
    files.sort(key=_mtime, reverse=True)
    cur = codex_current_provider().strip()
    out: list[dict] = []
    hidden = 0
    for path in files:
        if len(out) >= limit:
            break
        meta = _read_meta(path)
        if not meta or not meta.get("id"):
            continue
        # Different vendor -> different sessions: hide rollouts created under a
        # provider other than the one configured now (they'd fail to resume —
        # provider-encrypted reasoning). Unknown/blank provider is shown (can't
        # classify), and if no current provider is set we don't filter at all.
        prov = (meta.get("model_provider") or "").strip()
        if cur and prov and prov != cur:
            hidden += 1
            continue
        out.append({
            "session_id": meta["id"],
            "cwd": meta.get("cwd"),
            "last_modified": _mtime_iso(path),
            "first_prompt": _first_user_prompt(path),
        })
    if hidden:
        log.info("codex sessions filtered by provider", provider=cur, hidden=hidden, shown=len(out))
    return out


def codex_session_cwd(session_id: str) -> Optional[str]:
    """The cwd a Codex thread was started in (for resume). None if not found."""
    path = _rollout_path(session_id)
    if not path:
        return None
    meta = _read_meta(path)
    return meta.get("cwd") if meta else None


def codex_rollout_path(session_id: str) -> Optional[str]:
    """Public: the rollout .jsonl for a Codex thread (for history replay)."""
    return _rollout_path(session_id)


def codex_model(default: str = "gpt-5-codex") -> str:
    """The model Codex is configured to use (from ~/.codex/config.toml). Used to
    show the right model readout for live Codex sessions (not a Claude model)."""
    return _config_value("model", default)


def codex_effort(default: str = "high") -> str:
    """The default reasoning effort from ~/.codex/config.toml (model_reasoning_effort)."""
    return _config_value("model_reasoning_effort", default)


def codex_current_provider() -> str:
    """The provider Codex is configured for right now (config.toml model_provider).
    A codex rollout carries provider-encrypted reasoning, so a session from a
    DIFFERENT provider can't be resumed here — the list is filtered to this one."""
    return _config_value("model_provider", "")


def codex_context_window(default: int = 256000) -> int:
    """Fallback context window (tokens) for a fresh session before any turn has
    reported one. The AUTHORITATIVE value comes from the live server's
    thread/tokenUsage/updated (tokenUsage.modelContextWindow) and overrides this;
    ~/.codex/config.toml's model_context_window is only a user-declared estimate
    (it can disagree with the server, e.g. 400000 in config vs 258400 live)."""
    try:
        return int(_config_value("model_context_window", str(default)))
    except (ValueError, TypeError):
        return default


def codex_fast_enabled() -> bool:
    """True if ~/.codex/config.toml currently has a top-level service_tier = "fast"."""
    return (_config_value("service_tier", "") or "").lower() == "fast"


def set_codex_config_fast(on: bool) -> bool:
    """Toggle a top-level `service_tier = "fast"` line in ~/.codex/config.toml —
    this is what the user validates against and what codex reads at app-server
    start (so the caller reconnects the app-server to apply it). `on` adds/ensures
    the line, `off` removes it. Only touches that one line; the rest is preserved.
    Returns True on success."""
    try:
        with open(_CONFIG) as f:
            lines = f.readlines()
    except Exception as e:
        log.warning("read config.toml failed", error=str(e))
        return False
    # Top-level keys live before the first [table] header; only edit there so we
    # never inject service_tier under some [section].
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    kept: list[str] = []
    for i, l in enumerate(lines):
        if i < first_table and re.match(r"\s*service_tier\s*=", l):
            continue  # drop any existing top-level service_tier line
        kept.append(l)
    if on:
        entry = 'service_tier = "fast"\n'
        # place it right after the `model = ...` line if present, else at the top.
        after = next((i for i, l in enumerate(kept) if re.match(r"\s*model\s*=", l)), None)
        kept.insert(after + 1 if after is not None else 0, entry)
    try:
        with open(_CONFIG, "w") as f:
            f.writelines(kept)
        log.info("codex config service_tier toggled", fast=on)
        return True
    except Exception as e:
        log.warning("write config.toml failed", error=str(e))
        return False


def _config_value(key: str, default: str) -> str:
    try:
        with open(_CONFIG) as f:
            for line in f:
                s = line.strip()
                # exact key match: `model = ...` must not match `model_provider = ...`
                if s.startswith(key) and "=" in s:
                    lhs = s.split("=", 1)[0].strip()
                    if lhs == key:
                        return s.split("=", 1)[1].strip().strip('"').strip("'") or default
    except Exception:
        pass
    return default


# ---- internals ----
def _rollout_path(session_id: str) -> Optional[str]:
    try:
        m = glob.glob(os.path.join(_ROOT, "**", f"*{session_id}*.jsonl"), recursive=True)
        return m[0] if m else None
    except Exception:
        return None


def _read_meta(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            d = json.loads(f.readline())
        if d.get("type") == "session_meta" and isinstance(d.get("payload"), dict):
            return d["payload"]
    except Exception:
        pass
    return None


def _first_user_prompt(path: str, max_lines: int = 80) -> Optional[str]:
    """First real user text — skips the <environment_context>/<permissions>
    envelope messages Codex injects."""
    try:
        with open(path) as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "response_item":
                    continue
                p = d.get("payload") or {}
                if p.get("type") == "message" and p.get("role") == "user":
                    for c in (p.get("content") or []):
                        t = c.get("text") if isinstance(c, dict) else None
                        if t and not t.lstrip().startswith("<"):
                            return " ".join(t.split())[:100]
    except Exception:
        pass
    return None


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def _mtime_iso(path: str) -> Optional[str]:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    except Exception:
        return None
