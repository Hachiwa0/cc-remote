"""Persist the cc session id so a wrapper restart can --resume it.

Keyed by cwd: different projects have different sessions, and resume requires
the cwd to match (the session jsonl lives under
~/.claude/projects/<cwd-with-/-as->/).
"""
from __future__ import annotations

import json
from pathlib import Path

from cc_remote.log import logger

log = logger("cc_remote.wrapper.session")


def _session_file(state_dir: Path, cc_cwd: str) -> Path:
    safe = cc_cwd.replace("/", "_").strip("_") or "root"
    return state_dir / "sessions" / f"{safe}.json"


def load_session_id(state_dir: Path, cc_cwd: str) -> str | None:
    f = _session_file(state_dir, cc_cwd)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
        sid = data.get("cc_session_id")
        log.debug("loaded session id", path=str(f), has_id=bool(sid))
        return sid
    except Exception as e:
        log.warning("failed to read session file", path=str(f), error=str(e))
        return None


def save_session_id(state_dir: Path, cc_cwd: str, session_id: str) -> None:
    f = _session_file(state_dir, cc_cwd)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"cc_session_id": session_id, "cc_cwd": cc_cwd}))
    log.debug("saved session id", path=str(f), session_id=session_id)
