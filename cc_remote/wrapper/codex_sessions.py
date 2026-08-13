"""Codex session metadata and rollout helpers.

The app-server state DB is authoritative for sidebar metadata such as names and
archive state. Rollout files remain the source for history, cwd fallback, and
per-turn settings.
"""
from __future__ import annotations

import glob
from importlib import import_module
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Optional

from cc_remote.log import logger
from cc_remote.wrapper.codex_rpc import codex_rpc


def _load_tomllib():
    """Load TOML support on every advertised Python version."""
    try:
        return import_module("tomllib")
    except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib.
        return import_module("tomli")


tomllib = _load_tomllib()

log = logger("cc_remote.wrapper.codex_sessions")

_CONFIG = os.path.expanduser("~/.codex/config.toml")
_CONFIG_MAX_BYTES = 4 * 1024 * 1024
_ROOT = os.path.expanduser("~/.codex/sessions")
_ARCHIVE_ROOT = os.path.expanduser("~/.codex/archived_sessions")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
MAX_JSONL_RECORD_BYTES = 16 * 1024 * 1024
MAX_META_RECORD_BYTES = 1024 * 1024
_LIST_PAGE_SIZE = 100
_LIST_MAX_PER_ARCHIVE_STATE = 200
_LIST_MAX_PAGES = 20
_THREAD_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})
_STATE_DB = re.compile(r"^state_(\d+)\.sqlite$")


def _codex_home(codex_home: str | os.PathLike[str] | None = None) -> str:
    raw = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.realpath(os.path.expanduser(os.fspath(raw)))


def _config_path(codex_home: str | os.PathLike[str] | None = None) -> str:
    if codex_home is None:
        return _CONFIG
    return os.path.join(_codex_home(codex_home), "config.toml")


def _session_roots(
    codex_home: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    if codex_home is None:
        return _ROOT, _ARCHIVE_ROOT
    home = _codex_home(codex_home)
    return os.path.join(home, "sessions"), os.path.join(home, "archived_sessions")


async def list_codex_sessions(
    limit: int = 60,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """List active and archived app-server threads, newest first.

    ``limit`` is applied independently to active and archived threads so a busy
    active list cannot make the archived group disappear. Both result sets are
    bounded and paginated with opaque app-server cursors.
    """
    per_state_limit = max(1, min(limit, _LIST_MAX_PER_ARCHIVE_STATE))
    provider = (
        codex_current_provider()
        if codex_home is None
        else codex_current_provider(codex_home=codex_home)
    ).strip()
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

            response = (
                await codex_rpc("thread/list", params)
                if codex_home is None
                else await codex_rpc(
                    "thread/list", params,
                    codex_home=os.fspath(codex_home),
                )
            )
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


def codex_session_cwd(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[str]:
    """The cwd a Codex thread was started in (for resume). None if not found."""
    path = (
        _rollout_path(session_id)
        if codex_home is None
        else _rollout_path(session_id, codex_home=codex_home)
    )
    if not path:
        return None
    meta = _read_meta(path)
    return meta.get("cwd") if meta else None


def codex_rollout_path(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[str]:
    """Public: the rollout .jsonl for a Codex thread (for history replay)."""
    return (
        _rollout_path(session_id)
        if codex_home is None
        else _rollout_path(session_id, codex_home=codex_home)
    )


def codex_session_presence(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> bool | None:
    """Read one exact native thread id without collapsing I/O failure.

    The app-server SQLite catalog is authoritative for active and archived
    threads. ``None`` means ownership is unknown and callers must not infer a
    different engine from absence.
    """
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(
        session_id
    ):
        return None
    home = _codex_home(codex_home)
    config_path = os.path.join(home, "config.toml")
    try:
        if os.path.getsize(config_path) > _CONFIG_MAX_BYTES:
            return None
        with open(config_path, "rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        config = {}
    except Exception:
        return None
    sqlite_home = config.get("sqlite_home")
    if sqlite_home is None:
        sqlite_root = home
    elif isinstance(sqlite_home, str) and sqlite_home.strip():
        sqlite_root = os.path.expanduser(sqlite_home)
        if not os.path.isabs(sqlite_root):
            sqlite_root = os.path.join(home, sqlite_root)
        sqlite_root = os.path.realpath(sqlite_root)
    else:
        return None
    try:
        candidates = [
            (int(match.group(1)), os.path.join(sqlite_root, entry.name))
            for entry in os.scandir(sqlite_root)
            if entry.is_file(follow_symlinks=False)
            and (match := _STATE_DB.fullmatch(entry.name)) is not None
        ]
    except OSError:
        return None
    if not candidates:
        return None
    db_path = max(candidates, key=lambda item: item[0])[1]
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            row = connection.execute(
                "SELECT 1 FROM threads WHERE id=? LIMIT 1", (session_id,)
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return row is not None


def codex_model(
    default: str = "gpt-5-codex",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The model Codex is configured to use (from ~/.codex/config.toml). Used to
    show the right model readout for live Codex sessions (not a Claude model)."""
    return _config_value("model", default, codex_home=codex_home)[:256]


def codex_effort(
    default: str = "high",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The default reasoning effort from ~/.codex/config.toml (model_reasoning_effort)."""
    return _config_value(
        "model_reasoning_effort", default, codex_home=codex_home)[:64]


def codex_current_provider(
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The provider Codex is configured for right now (config.toml model_provider).
    A codex rollout carries provider-encrypted reasoning, so a session from a
    DIFFERENT provider can't be resumed here — the list is filtered to this one."""
    return _config_value("model_provider", "", codex_home=codex_home)[:256]


def codex_context_window(
    default: int = 256000,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> int:
    """Fallback context window (tokens) for a fresh session before any turn has
    reported one. The AUTHORITATIVE value comes from the live server's
    thread/tokenUsage/updated (tokenUsage.modelContextWindow) and overrides this;
    ~/.codex/config.toml's model_context_window is only a user-declared estimate
    (it can disagree with the server, e.g. 400000 in config vs 258400 live)."""
    try:
        return int(_config_value(
            "model_context_window", str(default), codex_home=codex_home))
    except (ValueError, TypeError):
        return default


def codex_fast_enabled(
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> bool:
    """True for either accepted/reported top-level Codex Fast tier name."""
    return (_config_value(
        "service_tier", "", codex_home=codex_home) or "").lower() in {
        "fast", "priority",
    }


def codex_approval(
    default: str = "never",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The top-level Codex approval policy used for a new thread."""
    value = _config_value(
        "approval_policy", default, codex_home=codex_home)
    return value if value in {"untrusted", "on-request", "never"} else default


def codex_web_search(
    default: str = "cached",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The top-level search mode inherited by a new no-override thread."""
    value = _config_value("web_search", default, codex_home=codex_home)
    return value if value in {"cached", "live"} else default


def codex_session_settings(
    session_id: str, max_bytes: int = 64 * 1024 * 1024,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> dict:
    """The per-thread settings carried by the latest bounded rollout tail.

    Codex appends a `turn_context` record per turn carrying `model`, `effort`,
    and the nested `collaboration_mode` selected for that turn. A live
    `thread/settings/update` is persisted immediately as a
    `thread_settings_applied` event, before another turn necessarily exists.
    Both records are consumed in file order so a wrapper restart cannot restore
    the preceding turn's stale controls over that newer applied snapshot.
    The official thread/resume response is authoritative for settings it exposes;
    this bounded tail is the fallback and remains necessary for collaboration mode,
    which 0.144.1 does not include in that response. Config.toml is never a valid
    resume source because it holds only fresh-thread global defaults.

    Returns {} when the rollout is missing/unreadable; the caller falls back to the
    config defaults (correct for a brand-new session).
    """
    path = (
        _rollout_path(session_id)
        if codex_home is None
        else _rollout_path(session_id, codex_home=codex_home)
    )
    if not path:
        return {}
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    out: dict = {}
    try:
        # A long-running thread can easily exceed 64 MiB. Only its newest
        # settings records matter, so seek to a bounded tail and discard the
        # first partial JSONL record instead of rejecting the entire rollout.
        tail_bytes = max(1, int(max_bytes))
        start = max(0, size - tail_bytes)
        with open(path, "rb") as f:
            if start:
                f.seek(start - 1)
                starts_at_record = f.read(1) == b"\n"
                f.seek(start)
                if not starts_at_record:
                    discarded = f.readline(MAX_JSONL_RECORD_BYTES + 1)
                    if not discarded.endswith(b"\n"):
                        return {}
            while True:
                raw = f.readline(MAX_JSONL_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_JSONL_RECORD_BYTES:
                    if not raw.endswith(b"\n"):
                        # The remainder is still the same oversized record. Stop:
                        # a boundary cannot be recovered without exceeding our cap.
                        break
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                # Cheap prefilter: most lines are messages, not settings.
                if ('"turn_context"' not in line
                        and '"thread_settings_applied"' not in line):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                record_type = rec.get("type")
                payload = rec.get("payload")
                effort_key = "effort"
                if record_type == "event_msg" and isinstance(payload, dict):
                    if payload.get("type") != "thread_settings_applied":
                        continue
                    payload = payload.get("thread_settings")
                    effort_key = "reasoning_effort"
                elif record_type != "turn_context":
                    continue
                if not isinstance(payload, dict):
                    continue

                model = payload.get("model")
                if isinstance(model, str) and model:
                    out["model"] = model[:256]
                if effort_key in payload:
                    effort = payload.get(effort_key)
                    if isinstance(effort, str) and effort:
                        out["effort"] = effort[:64]
                    elif effort is None:
                        out.pop("effort", None)

                approval = payload.get("approval_policy")
                if (isinstance(approval, str)
                        and approval in {"untrusted", "on-request", "never"}):
                    out["approval_policy"] = approval
                    out.pop("approval_policy_granular", None)
                elif (isinstance(approval, dict)
                        and isinstance(approval.get("granular"), dict)):
                    # The bounded history reader need not duplicate the complete
                    # policy object.  It must, however, distinguish native
                    # granular approval from a missing value so callers do not
                    # replace it with a stale named/UI projection.
                    out.pop("approval_policy", None)
                    out["approval_policy_granular"] = True
                if "service_tier" in payload:
                    tier = payload.get("service_tier")
                    if tier is None or tier == "default":
                        out["service_tier"] = None
                    elif isinstance(tier, str) and tier:
                        out["service_tier"] = tier[:64]
                collaboration = payload.get("collaboration_mode")
                if isinstance(collaboration, dict):
                    mode = collaboration.get("mode")
                    if mode in ("default", "plan"):
                        out["collaboration_mode"] = mode
                # Only thread_settings_applied contains the selected profile id.
                # turn_context.permission_profile is the expanded policy object
                # and intentionally has no stable profile provenance.
                if effort_key == "reasoning_effort":
                    active_profile = payload.get("active_permission_profile")
                    if isinstance(active_profile, dict):
                        profile_id = active_profile.get("id")
                        if (isinstance(profile_id, str) and profile_id
                                and len(profile_id) <= 256):
                            out["permission_profile"] = profile_id
                    elif "active_permission_profile" in payload:
                        out["permission_profile"] = None
    except Exception as e:
        log.warning("read codex session settings failed", session_id=session_id, error=str(e))
    return out


def _config_value(
    key: str,
    default: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    try:
        target = os.path.realpath(_config_path(codex_home))
        if os.path.getsize(target) > _CONFIG_MAX_BYTES:
            return default
        with open(target, "rb") as f:
            config = tomllib.load(f)
        # tomllib preserves table boundaries. Looking only at the root prevents
        # a profile/provider's nested `model`, effort or service tier from being
        # mistaken for the user's default.
        value = config.get(key)
        if isinstance(value, str):
            return value[:4096] or default
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)[:4096]
    except Exception:
        pass
    return default


# ---- internals ----
def _rollout_path(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[str]:
    try:
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            return None
        safe_id = glob.escape(session_id)
        scanned = 0
        # thread/archive moves the rollout out of ``sessions``. Archived rows
        # still need history, cwd lookup, and engine detection so unarchive never
        # falls through to the Claude SDK.
        for source_root in _session_roots(codex_home):
            matches = glob.iglob(
                os.path.join(source_root, "**", f"*{safe_id}*.jsonl"),
                recursive=True,
            )
            root = os.path.realpath(source_root)
            for match in matches:
                if scanned >= 1000:
                    return None
                scanned += 1
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
