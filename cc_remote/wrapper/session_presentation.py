"""Durable cross-client presentation receipts for native sessions.

Completion badges and Goal dismissal are user-visible session state, not
engine transcript metadata.  Keep their latest bounded projection in the
wrapper so acknowledging either surface on one browser immediately applies to
every other browser and survives a wrapper restart.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4


_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 16 * 1024 * 1024


class SessionPresentationStoreError(RuntimeError):
    """The Remote-owned presentation receipt store is unsafe or malformed."""


@dataclass(frozen=True)
class SessionPresentationSnapshot:
    completion_id: str | None = None
    completion_unread: bool = False
    completion_revision: int = 0
    dismissed_goal_id: str | None = None
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "completion_id": self.completion_id,
            "completion_unread": self.completion_unread,
            "completion_revision": self.completion_revision,
            "dismissed_goal_id": self.dismissed_goal_id,
            "updated_at": self.updated_at,
        }


def _wire_id(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _WIRE_ID.fullmatch(value):
        raise SessionPresentationStoreError("session presentation id is invalid")
    return value


def _snapshot(value: object) -> SessionPresentationSnapshot:
    if not isinstance(value, dict) or set(value) != {
        "completion_id",
        "completion_unread",
        "completion_revision",
        "dismissed_goal_id",
        "updated_at",
    }:
        raise SessionPresentationStoreError(
            "session presentation snapshot has an invalid shape"
        )
    revision = value.get("completion_revision")
    updated_at = value.get("updated_at")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise SessionPresentationStoreError(
            "session presentation revision is invalid"
        )
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or updated_at < 0
    ):
        raise SessionPresentationStoreError(
            "session presentation timestamp is invalid"
        )
    unread = value.get("completion_unread")
    if not isinstance(unread, bool):
        raise SessionPresentationStoreError(
            "session completion receipt is invalid"
        )
    completion_id = _wire_id(value.get("completion_id"), optional=True)
    dismissed_goal_id = _wire_id(
        value.get("dismissed_goal_id"), optional=True
    )
    if unread and completion_id is None:
        raise SessionPresentationStoreError(
            "an unread completion requires an identity"
        )
    return SessionPresentationSnapshot(
        completion_id=completion_id,
        completion_unread=unread,
        completion_revision=revision,
        dismissed_goal_id=dismissed_goal_id,
        updated_at=float(updated_at),
    )


class SessionPresentationStore:
    """Atomic, bounded completion and Goal presentation receipts."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "session-presentation.json"
        self._lock = threading.RLock()
        self._sessions = self._load()

    def get(self, session_id: str) -> SessionPresentationSnapshot:
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        assert session_id is not None
        with self._lock:
            snapshot = self._sessions.get(session_id)
            if snapshot is None:
                return SessionPresentationSnapshot()
            self._sessions.move_to_end(session_id)
            return snapshot

    def mark_completion(
        self,
        session_id: str,
        completion_id: str | None = None,
    ) -> SessionPresentationSnapshot:
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        completion_id = _wire_id(
            completion_id or f"completion-{uuid4().hex}"
        )
        assert session_id is not None and completion_id is not None
        with self._lock:
            current = self._sessions.get(
                session_id, SessionPresentationSnapshot()
            )
            if (
                current.completion_id == completion_id
                and current.completion_unread
            ):
                return current
            return self._replace_locked(
                session_id,
                replace(
                    current,
                    completion_id=completion_id,
                    completion_unread=True,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def acknowledge_completion(
        self,
        session_id: str,
        completion_id: str,
    ) -> SessionPresentationSnapshot:
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        completion_id = _wire_id(completion_id)  # type: ignore[assignment]
        assert session_id is not None and completion_id is not None
        with self._lock:
            current = self._sessions.get(
                session_id, SessionPresentationSnapshot()
            )
            # A delayed acknowledgement for turn N must never clear the unread
            # receipt for a newer turn N+1.
            if (
                current.completion_id != completion_id
                or not current.completion_unread
            ):
                return current
            return self._replace_locked(
                session_id,
                replace(
                    current,
                    completion_unread=False,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def clear_completion(
        self,
        session_id: str,
    ) -> SessionPresentationSnapshot:
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        assert session_id is not None
        with self._lock:
            current = self._sessions.get(
                session_id, SessionPresentationSnapshot()
            )
            if current.completion_id is None and not current.completion_unread:
                return current
            return self._replace_locked(
                session_id,
                replace(
                    current,
                    completion_id=None,
                    completion_unread=False,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def dismiss_goal(
        self,
        session_id: str,
        goal_id: str,
    ) -> SessionPresentationSnapshot:
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        goal_id = _wire_id(goal_id)  # type: ignore[assignment]
        assert session_id is not None and goal_id is not None
        with self._lock:
            current = self._sessions.get(
                session_id, SessionPresentationSnapshot()
            )
            if current.dismissed_goal_id == goal_id:
                return current
            return self._replace_locked(
                session_id,
                replace(
                    current,
                    dismissed_goal_id=goal_id,
                    updated_at=time.time(),
                ),
            )

    def reconcile_goal(self, session_id: str, goal_id: str | None) -> bool:
        """Return whether *goal_id* is hidden, clearing stale generations."""
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        goal_id = _wire_id(goal_id, optional=True)  # type: ignore[assignment]
        assert session_id is not None
        with self._lock:
            current = self._sessions.get(
                session_id, SessionPresentationSnapshot()
            )
            dismissed = current.dismissed_goal_id
            if dismissed is not None and dismissed != goal_id:
                current = self._replace_locked(
                    session_id,
                    replace(
                        current,
                        dismissed_goal_id=None,
                        updated_at=time.time(),
                    ),
                )
            return goal_id is not None and current.dismissed_goal_id == goal_id

    def move(self, old_session_id: str, session_id: str) -> None:
        old_session_id = _wire_id(old_session_id)  # type: ignore[assignment]
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        assert old_session_id is not None and session_id is not None
        if old_session_id == session_id:
            return
        with self._lock:
            snapshot = self._sessions.get(old_session_id)
            if snapshot is None:
                return
            updated = OrderedDict(self._sessions)
            updated.pop(old_session_id, None)
            target = updated.get(session_id)
            if target is None or snapshot.updated_at >= target.updated_at:
                updated.pop(session_id, None)
                updated[session_id] = snapshot
            self._persist_bounded(updated)
            self._sessions = updated

    def delete(self, session_id: str) -> None:
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        assert session_id is not None
        with self._lock:
            if session_id not in self._sessions:
                return
            updated = OrderedDict(self._sessions)
            updated.pop(session_id, None)
            self._persist_bounded(updated)
            self._sessions = updated

    def _replace_locked(
        self,
        session_id: str,
        snapshot: SessionPresentationSnapshot,
    ) -> SessionPresentationSnapshot:
        updated = OrderedDict(self._sessions)
        updated.pop(session_id, None)
        updated[session_id] = snapshot
        while len(updated) > _MAX_ENTRIES:
            updated.popitem(last=False)
        self._persist_bounded(updated)
        self._sessions = updated
        return snapshot

    def _load(self) -> OrderedDict[str, SessionPresentationSnapshot]:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise ValueError(
                    "session presentation store is not a bounded regular file"
                )
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("session presentation store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"version", "sessions"}:
                raise ValueError(
                    "session presentation store has an invalid envelope"
                )
            if raw.get("version") != 1 or not isinstance(
                raw.get("sessions"), dict
            ):
                raise ValueError(
                    "session presentation store version is unsupported"
                )
            loaded: OrderedDict[
                str, SessionPresentationSnapshot
            ] = OrderedDict()
            for session_id, value in raw["sessions"].items():
                clean_id = _wire_id(session_id)
                assert clean_id is not None
                loaded[clean_id] = _snapshot(value)
            if len(loaded) > _MAX_ENTRIES:
                raise ValueError("session presentation store has too many entries")
            return loaded
        except FileNotFoundError:
            return OrderedDict()
        except Exception as exc:
            raise SessionPresentationStoreError(
                "session presentation store is unreadable"
            ) from exc

    @staticmethod
    def _payload(
        sessions: OrderedDict[str, SessionPresentationSnapshot],
    ) -> bytes:
        return json.dumps(
            {
                "version": 1,
                "sessions": {
                    session_id: snapshot.as_dict()
                    for session_id, snapshot in sessions.items()
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _persist_bounded(
        self,
        sessions: OrderedDict[str, SessionPresentationSnapshot],
    ) -> None:
        bounded = OrderedDict(sessions)
        payload = self._payload(bounded)
        while len(payload) > _MAX_FILE_BYTES and len(bounded) > 1:
            bounded.popitem(last=False)
            payload = self._payload(bounded)
        if len(payload) > _MAX_FILE_BYTES:
            raise SessionPresentationStoreError(
                "session presentation store exceeds size limit"
            )
        sessions.clear()
        sessions.update(bounded)
        self._persist(payload)

    def _persist(self, payload: bytes) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(tmp, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise SessionPresentationStoreError(
                "session presentation store could not be persisted"
            ) from exc
