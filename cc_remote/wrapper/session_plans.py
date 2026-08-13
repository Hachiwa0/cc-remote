"""Durable latest-plan snapshots for Codex sessions.

Codex emits ``turn/plan/updated`` as a live notification, but its persisted
``thread/turns/list`` projection does not currently retain that notification.
Keep the latest display-safe snapshot in cc-remote's private state so a fresh
browser can recover the same plan affordance without replaying a model turn.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from cc_remote.protocol import TurnPlan


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 16 * 1024 * 1024


class SessionPlanStoreError(RuntimeError):
    """The Remote-owned plan snapshot store is unsafe or malformed."""


@dataclass(frozen=True)
class SessionPlanSnapshot:
    item_id: str
    turn_id: str | None
    explanation: str | None
    plan: tuple[dict[str, str], ...]
    updated_at: float

    @property
    def complete(self) -> bool:
        return bool(self.plan) and all(
            entry["status"] == "completed" for entry in self.plan)

    @classmethod
    def from_event(
        cls,
        event: TurnPlan,
        *,
        updated_at: float | None = None,
    ) -> "SessionPlanSnapshot":
        # TurnPlan is the public bounded schema. Re-validating a copied payload
        # keeps this private store aligned if a caller supplies a subclass or a
        # test double instead of the exact model instance.
        clean = TurnPlan.model_validate(event.model_dump(mode="python"))
        return cls(
            item_id=clean.item_id,
            turn_id=clean.turn_id,
            explanation=clean.explanation,
            plan=tuple(dict(entry) for entry in clean.plan),
            updated_at=time.time() if updated_at is None else updated_at,
        )

    def as_event(self) -> TurnPlan:
        return TurnPlan(
            item_id=self.item_id,
            turn_id=self.turn_id,
            explanation=self.explanation,
            plan=[dict(entry) for entry in self.plan],
        )

    def as_process_block(self) -> dict[str, Any]:
        return {
            "kind": "process",
            "item_id": self.item_id,
            "processKind": "plan",
            "phase": "snapshot",
            "status": "succeeded" if self.complete else "running",
            "turn_id": self.turn_id,
            "parent_id": None,
            "title": "计划",
            "done": self.complete,
            "explanation": self.explanation,
            "plan": [dict(entry) for entry in self.plan],
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "turn_id": self.turn_id,
            "explanation": self.explanation,
            "plan": [dict(entry) for entry in self.plan],
            "updated_at": self.updated_at,
        }


def _session_id(value: object) -> str:
    if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
        raise SessionPlanStoreError("Codex session id is invalid")
    return value


def _snapshot(value: object) -> SessionPlanSnapshot:
    if not isinstance(value, dict) or set(value) != {
        "item_id", "turn_id", "explanation", "plan", "updated_at",
    }:
        raise SessionPlanStoreError("Codex plan snapshot has an invalid shape")
    updated_at = value.get("updated_at")
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or updated_at < 0
    ):
        raise SessionPlanStoreError("Codex plan snapshot timestamp is invalid")
    try:
        event = TurnPlan(
            item_id=value.get("item_id"),
            turn_id=value.get("turn_id"),
            explanation=value.get("explanation"),
            plan=value.get("plan"),
        )
    except Exception as exc:
        raise SessionPlanStoreError(
            "Codex plan snapshot payload is invalid") from exc
    return SessionPlanSnapshot.from_event(event, updated_at=float(updated_at))


class SessionPlanStore:
    """Atomic, bounded plan snapshots that survive browser/wrapper restarts."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "session-plans.json"
        self._lock = threading.RLock()
        self._plans, self._profile_revision = self._load()

    def migrate_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Atomically translate Codex Plan keys once per topology revision."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise SessionPlanStoreError("invalid Codex profile revision")
        with self._lock:
            if self._profile_revision >= profile_revision:
                return 0
            updated: OrderedDict[str, SessionPlanSnapshot] = OrderedDict()
            migrated = 0
            for session_id, snapshot in self._plans.items():
                target = _session_id(transform(session_id))
                existing = updated.get(target)
                if existing is not None and existing != snapshot:
                    raise SessionPlanStoreError(
                        "Codex Plan profile migration collides")
                updated[target] = snapshot
                migrated += target != session_id
            self._persist_bounded(
                updated, profile_revision=profile_revision)
            self._plans = updated
            self._profile_revision = profile_revision
            return migrated

    def get(self, session_id: str) -> SessionPlanSnapshot | None:
        session_id = _session_id(session_id)
        with self._lock:
            snapshot = self._plans.get(session_id)
            if snapshot is not None:
                self._plans.move_to_end(session_id)
            return snapshot

    def put(self, session_id: str, event: TurnPlan) -> SessionPlanSnapshot:
        session_id = _session_id(session_id)
        snapshot = SessionPlanSnapshot.from_event(event)
        with self._lock:
            updated = OrderedDict(self._plans)
            updated.pop(session_id, None)
            updated[session_id] = snapshot
            while len(updated) > _MAX_ENTRIES:
                updated.popitem(last=False)
            self._persist_bounded(updated)
            self._plans = updated
        return snapshot

    def move(self, old_session_id: str, session_id: str) -> None:
        old_session_id = _session_id(old_session_id)
        session_id = _session_id(session_id)
        if old_session_id == session_id:
            return
        with self._lock:
            snapshot = self._plans.get(old_session_id)
            if snapshot is None:
                return
            updated = OrderedDict(self._plans)
            updated.pop(old_session_id, None)
            updated.pop(session_id, None)
            updated[session_id] = snapshot
            self._persist_bounded(updated)
            self._plans = updated

    def delete(self, session_id: str) -> None:
        session_id = _session_id(session_id)
        with self._lock:
            if session_id not in self._plans:
                return
            updated = OrderedDict(self._plans)
            updated.pop(session_id, None)
            self._persist_bounded(updated)
            self._plans = updated

    def retire_completed(
        self,
        session_id: str,
        *,
        current_turn_ids: frozenset[str] = frozenset(),
    ) -> bool:
        """Delete a completed Plan when a later user message begins.

        A replay of the Plan's own ``UserMsg`` is not a later message, so its
        known native/client identity may protect the snapshot. A steer is
        always a new user boundary and therefore passes no protected ids.
        """
        session_id = _session_id(session_id)
        with self._lock:
            snapshot = self._plans.get(session_id)
            if (
                snapshot is None
                or not snapshot.complete
                or (
                    snapshot.turn_id is not None
                    and snapshot.turn_id in current_turn_ids
                )
            ):
                return False
            updated = OrderedDict(self._plans)
            updated.pop(session_id, None)
            self._persist_bounded(updated)
            self._plans = updated
            return True

    def _load(
        self,
    ) -> tuple[OrderedDict[str, SessionPlanSnapshot], int]:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise ValueError("session plan store is not a bounded regular file")
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("session plan store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) not in (
                {"version", "plans"},
                {"version", "profile_revision", "plans"},
            ):
                raise ValueError("session plan store has an invalid envelope")
            if raw.get("version") not in {1, 2} or not isinstance(
                raw.get("plans"), dict
            ):
                raise ValueError("session plan store version is unsupported")
            profile_revision = raw.get("profile_revision", 0)
            if (
                isinstance(profile_revision, bool)
                or not isinstance(profile_revision, int)
                or profile_revision < 0
                or (raw.get("version") == 1 and profile_revision != 0)
            ):
                raise ValueError("session plan profile revision is invalid")
            loaded: OrderedDict[str, SessionPlanSnapshot] = OrderedDict()
            for session_id, value in raw["plans"].items():
                loaded[_session_id(session_id)] = _snapshot(value)
            if len(loaded) > _MAX_ENTRIES:
                raise ValueError("session plan store has too many entries")
            return loaded, profile_revision
        except FileNotFoundError:
            return OrderedDict(), 0
        except Exception as exc:
            raise SessionPlanStoreError(
                "session plan store is unreadable") from exc

    @staticmethod
    def _payload(
        plans: OrderedDict[str, SessionPlanSnapshot],
        *,
        profile_revision: int,
    ) -> bytes:
        return json.dumps({
            "version": 2,
            "profile_revision": profile_revision,
            "plans": {
                session_id: snapshot.as_dict()
                for session_id, snapshot in plans.items()
            },
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _persist_bounded(
        self,
        plans: OrderedDict[str, SessionPlanSnapshot],
        *,
        profile_revision: int | None = None,
    ) -> None:
        bounded = OrderedDict(plans)
        revision = (
            self._profile_revision
            if profile_revision is None else profile_revision
        )
        payload = self._payload(bounded, profile_revision=revision)
        while len(payload) > _MAX_FILE_BYTES and len(bounded) > 1:
            bounded.popitem(last=False)
            payload = self._payload(
                bounded, profile_revision=revision)
        if len(payload) > _MAX_FILE_BYTES:
            raise SessionPlanStoreError("session plan store exceeds size limit")
        # Propagate LRU evictions back to the caller's replacement map.
        plans.clear()
        plans.update(bounded)
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
            raise SessionPlanStoreError(
                "session plan store could not be persisted") from exc
