"""Codex session metadata and rollout helpers.

The app-server state DB is authoritative for sidebar metadata such as names and
archive state. Rollout files remain the source for history, cwd fallback, and
per-turn settings.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import stat
import tempfile
from typing import Any, Optional

from cc_remote.log import logger
from cc_remote.wrapper.codex_rpc import codex_rpc

log = logger("cc_remote.wrapper.codex_sessions")

_CONFIG = os.path.expanduser("~/.codex/config.toml")
_CONFIG_MAX_BYTES = 4 * 1024 * 1024

_ROOT = os.path.expanduser("~/.codex/sessions")
_GLOB = os.path.join(_ROOT, "**", "rollout-*.jsonl")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
MAX_JSONL_RECORD_BYTES = 16 * 1024 * 1024
MAX_META_RECORD_BYTES = 1024 * 1024
_LIST_PAGE_SIZE = 100
_LIST_MAX_PER_ARCHIVE_STATE = 200
_LIST_MAX_PAGES = 20
_THREAD_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})


async def list_codex_sessions(limit: int = 60) -> list[dict[str, Any]]:
    """List active and archived app-server threads, newest first.

    ``limit`` is applied independently to active and archived threads so a busy
    active list cannot make the archived group disappear. Both result sets are
    bounded and paginated with opaque app-server cursors.
    """
    per_state_limit = max(1, min(limit, _LIST_MAX_PER_ARCHIVE_STATE))
    provider = codex_current_provider().strip()
    by_id: dict[str, dict[str, Any]] = {}

    for archived in (False, True):
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        received = 0
        for _ in range(_LIST_MAX_PAGES):
            remaining = per_state_limit - received
            if remaining <= 0:
                break
            params: dict[str, Any] = {
                "limit": min(_LIST_PAGE_SIZE, remaining),
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": archived,
            }
            if cursor:
                params["cursor"] = cursor
            if provider:
                params["modelProviders"] = [provider]

            response = await codex_rpc("thread/list", params)
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise RuntimeError("codex thread/list returned an invalid response")
            page = response["data"][:remaining]
            received += len(page)
            for thread in page:
                normalized = _normalize_thread(thread, archived=archived)
                if normalized is not None:
                    by_id[normalized["session_id"]] = normalized

            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("codex thread/list repeated its pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    return sorted(
        by_id.values(), key=lambda item: _updated_sort_key(item.get("last_modified")),
        reverse=True,
    )


def _normalize_thread(thread: Any, *, archived: bool) -> Optional[dict[str, Any]]:
    if not isinstance(thread, dict):
        return None
    session_id = thread.get("id")
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        return None

    git_info = thread.get("gitInfo")
    branch = git_info.get("branch") if isinstance(git_info, dict) else None
    forked_from = thread.get("forkedFromId")
    if not isinstance(forked_from, str) or not _SAFE_SESSION_ID.fullmatch(forked_from):
        forked_from = None
    raw_status = thread.get("status")
    status = raw_status.get("type") if isinstance(raw_status, dict) else None
    if status not in _THREAD_STATUSES:
        status = None

    updated_at = thread.get("updatedAt")
    if (isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))
            or not math.isfinite(updated_at) or updated_at < 0):
        last_modified = None
    else:
        last_modified = str(updated_at)

    return {
        "session_id": session_id,
        "summary": _bounded_text(thread.get("name"), 500),
        "first_prompt": _bounded_text(thread.get("preview"), 2000),
        "cwd": _bounded_text(thread.get("cwd"), 4096),
        "last_modified": last_modified,
        "git_branch": _bounded_text(branch, 500),
        "forked_from_id": forked_from,
        "status": status,
        "tag": "archived" if archived else None,
    }


def _bounded_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value[:limit] or None


def _updated_sort_key(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return -1.0
    return parsed if math.isfinite(parsed) else -1.0


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
    return _config_value("model", default)[:256]


def codex_effort(default: str = "high") -> str:
    """The default reasoning effort from ~/.codex/config.toml (model_reasoning_effort)."""
    return _config_value("model_reasoning_effort", default)[:64]


def codex_current_provider() -> str:
    """The provider Codex is configured for right now (config.toml model_provider).
    A codex rollout carries provider-encrypted reasoning, so a session from a
    DIFFERENT provider can't be resumed here — the list is filtered to this one."""
    return _config_value("model_provider", "")[:256]


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
    """Toggle a top-level `service_tier = "fast"` line in ~/.codex/config.toml.

    Unlike model/effort — which are per-turn turn/start params, so the live session
    honors a change immediately — codex reads `service_tier` ONCE at app-server
    start. A per-turn override can't turn it back OFF while the config still says
    "fast", so this really must be written, and the caller reconnects to apply it.
    """
    ok = set_codex_config_key("service_tier", "fast" if on else None)
    if ok:
        log.info("codex config service_tier toggled", fast=on)
    return ok


def set_codex_config_key(key: str, value: Optional[str]) -> bool:
    """Set/replace a TOP-LEVEL `key = "value"` line in ~/.codex/config.toml
    (value=None removes it). Only that one line is touched; every other line — and
    the file's line ORDER — is kept byte-for-byte. Top-level keys live before the
    first [table] header, so we never inject into a [section].

    NOTE we deliberately do NOT write `model`/`model_reasoning_effort` here on a
    per-session switch. codex keeps those two concerns apart — `thread/settings/update`
    changes one thread and never touches config.toml (measured), while `config/write`
    is a separate, explicit "change my default" call. config.toml is the default a
    NEW session (and the user's terminal codex) inherits; a session's own model/effort
    live in its rollout. See codex_session_settings().
    """
    target = os.path.realpath(_CONFIG)
    try:
        info = os.stat(target)
        if info.st_size > _CONFIG_MAX_BYTES:
            raise ValueError("config.toml exceeds size limit")
        with open(target) as f:
            content = f.read(_CONFIG_MAX_BYTES + 1)
        if len(content.encode("utf-8", "surrogatepass")) > _CONFIG_MAX_BYTES:
            raise ValueError("config.toml exceeds size limit")
        lines = content.splitlines(keepends=True)
    except Exception as e:
        log.warning("read config.toml failed", error=str(e))
        return False
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    # `model\s*=` never matches model_provider / model_reasoning_effort (they have
    # more name chars before the `=`), so anchoring on the key name is exact.
    pat = re.compile(r"\s*" + re.escape(key) + r"\s*=")
    hits = [i for i, l in enumerate(lines) if i < first_table and pat.match(l)]
    entry = f'{key} = "{value}"\n'
    if value is None:
        for i in reversed(hits):
            del lines[i]
    elif hits:
        lines[hits[0]] = entry              # replace IN PLACE — never reshuffle the file
        for i in reversed(hits[1:]):
            del lines[i]                    # drop any duplicate (invalid TOML anyway)
    else:
        after = next((i for i, l in enumerate(lines)
                      if i < first_table and re.match(r"\s*model\s*=", l)), None)
        lines.insert(after + 1 if after is not None else 0, entry)
    temp_path = ""
    temp_fd: int | None = None
    try:
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=".config.toml.cc-remote-", dir=os.path.dirname(target))
        stream = os.fdopen(temp_fd, "w")
        temp_fd = None  # stream owns it from here
        with stream as f:
            os.fchmod(f.fileno(), stat.S_IMODE(info.st_mode))
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
        temp_path = ""
        log.info("codex config key set", key=key, value=value)
        return True
    except Exception as e:
        log.warning("write config.toml failed", error=str(e))
        return False
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def codex_session_settings(
    session_id: str, max_bytes: int = 64 * 1024 * 1024,
) -> dict:
    """The model/effort THIS session last ran with, read from its own rollout.

    codex appends a `turn_context` record per turn carrying `model` and `effort`.
    That — not config.toml — is a resumed session's truth. config.toml holds ONE
    global default, so seeding a resumed session from it silently reverted a session
    the user had switched to gpt-5.6-luna back to the config's gpt-5.6-sol, and in
    multi-session it leaked one session's pick into another.

    Returns {} when the rollout is missing/unreadable; the caller falls back to the
    config defaults (correct for a brand-new session).
    """
    path = _rollout_path(session_id)
    if not path:
        return {}
    try:
        if os.path.getsize(path) > max_bytes:
            log.warning("codex session settings source too large",
                        session_id=session_id)
            return {}
    except OSError:
        return {}
    out: dict = {}
    try:
        with open(path) as f:
            for line in _bounded_lines(f, MAX_JSONL_RECORD_BYTES):
                # cheap prefilter: most lines are messages, not turn contexts
                if '"turn_context"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "turn_context":
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                for key in ("model", "effort"):
                    val = payload.get(key)
                    if isinstance(val, str) and val:
                        out[key] = val[:256 if key == "model" else 64]
                        # last one wins = the session's current setting
    except Exception as e:
        log.warning("read codex session settings failed", session_id=session_id, error=str(e))
    return out


def _config_value(key: str, default: str) -> str:
    try:
        target = os.path.realpath(_CONFIG)
        if os.path.getsize(target) > _CONFIG_MAX_BYTES:
            return default
        with open(target) as f:
            for line in f:
                s = line.strip()
                # exact key match: `model = ...` must not match `model_provider = ...`
                if s.startswith(key) and "=" in s:
                    lhs = s.split("=", 1)[0].strip()
                    if lhs == key:
                        value = s.split("=", 1)[1].strip().strip('"').strip("'")
                        return value[:4096] or default
    except Exception:
        pass
    return default


# ---- internals ----
def _rollout_path(session_id: str) -> Optional[str]:
    try:
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            return None
        safe_id = glob.escape(session_id)
        matches = glob.iglob(
            os.path.join(_ROOT, "**", f"*{safe_id}*.jsonl"), recursive=True)
        root = os.path.realpath(_ROOT)
        for index, match in enumerate(matches):
            if index >= 1000:
                break
            resolved = os.path.realpath(match)
            if os.path.commonpath((root, resolved)) == root:
                return match
        return None
    except Exception:
        return None


def _read_meta(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            line = f.readline(MAX_META_RECORD_BYTES + 1)
            if len(line.encode("utf-8", "surrogatepass")) > MAX_META_RECORD_BYTES:
                return None
            d = json.loads(line)
        if d.get("type") == "session_meta" and isinstance(d.get("payload"), dict):
            return d["payload"]
    except Exception:
        pass
    return None


def _bounded_lines(file, max_record_bytes: int):
    """Yield complete JSONL records without ever allocating one unbounded line."""
    while True:
        line = file.readline(max_record_bytes + 1)
        if not line:
            return
        if len(line.encode("utf-8", "surrogatepass")) <= max_record_bytes \
                and (line.endswith("\n") or len(line) < max_record_bytes + 1):
            yield line
            continue
        # Oversized record: consume bounded chunks through its newline and skip it.
        while line and not line.endswith("\n"):
            line = file.readline(max_record_bytes + 1)
