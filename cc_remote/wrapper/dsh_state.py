"""Private presentation state owned by the optional DSH integration.

Keep these files separate from the long-lived Claude/Codex stores.  Older
cc-remote releases reject unknown engines in those stores, so writing DSH
records there would make an otherwise safe release rollback lose access to
all existing pin and completion state.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from cc_remote.wrapper.dsh_client import dsh_native_session_id
from cc_remote.wrapper.session_pins import SessionPinStoreError
from cc_remote.wrapper.session_presentation import (
    SessionPresentationSnapshot,
    SessionPresentationStoreError,
)


_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_PIN_MAX_ENTRIES = 4096
_PIN_MAX_FILE_BYTES = 1024 * 1024
_PRESENTATION_MAX_ENTRIES = 4096
_PRESENTATION_MAX_FILE_BYTES = 16 * 1024 * 1024


def _dsh_session_id(value: object) -> str:
    if not isinstance(value, str) or not _WIRE_ID.fullmatch(value):
        raise ValueError("invalid DSH session id")
    dsh_native_session_id(value)
    return value


def _wire_id(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _WIRE_ID.fullmatch(value):
        raise ValueError("invalid DSH presentation id")
    return value


def _read_regular_file(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("DSH state is not a bounded regular file")
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("DSH state exceeds its size limit")
    return raw


def _atomic_write(path: Path, payload: bytes, limit: int) -> None:
    if len(payload) > limit:
        raise ValueError("DSH state exceeds its size limit")
    tmp = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
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
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class DshSessionPinStore:
    """Atomic, bounded sidebar pins for namespaced DSH sessions."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "dsh-session-pins.json"
        self._lock = threading.RLock()
        self._pins = self._load()

    def ids(self, engine: str) -> frozenset[str]:
        self._validate_engine(engine)
        with self._lock:
            return frozenset(self._pins)

    def set_pinned(
        self,
        engine: str,
        session_id: str,
        pinned: bool,
    ) -> None:
        self._validate_engine(engine)
        try:
            session_id = _dsh_session_id(session_id)
        except ValueError as exc:
            raise SessionPinStoreError("invalid DSH pinned session id") from exc
        with self._lock:
            updated = set(self._pins)
            if pinned:
                updated.add(session_id)
            else:
                updated.discard(session_id)
            if updated == self._pins:
                return
            if len(updated) > _PIN_MAX_ENTRIES:
                raise SessionPinStoreError("DSH session pin limit reached")
            payload = json.dumps(
                {"version": 1, "sessions": sorted(updated)},
                separators=(",", ":"),
            ).encode("utf-8")
            try:
                _atomic_write(self.path, payload, _PIN_MAX_FILE_BYTES)
            except Exception as exc:
                raise SessionPinStoreError(
                    "DSH session pin store could not be persisted"
                ) from exc
            self._pins = updated

    @staticmethod
    def _validate_engine(engine: object) -> None:
        if engine != "dsh":
            raise SessionPinStoreError("invalid DSH session pin engine")

    def _load(self) -> set[str]:
        try:
            raw = json.loads(
                _read_regular_file(
                    self.path, _PIN_MAX_FILE_BYTES
                ).decode("utf-8")
            )
            if (
                not isinstance(raw, dict)
                or set(raw) != {"version", "sessions"}
                or raw.get("version") != 1
                or not isinstance(raw.get("sessions"), list)
                or len(raw["sessions"]) > _PIN_MAX_ENTRIES
            ):
                raise ValueError("DSH session pin store has an invalid shape")
            pins: set[str] = set()
            for value in raw["sessions"]:
                pins.add(_dsh_session_id(value))
            if len(pins) != len(raw["sessions"]):
                raise ValueError("DSH session pin store contains duplicates")
            return pins
        except FileNotFoundError:
            return set()
        except Exception as exc:
            raise SessionPinStoreError(
                "DSH session pin store is unreadable"
            ) from exc


class DshSessionPresentationStore:
    """Atomic, bounded completion receipts for DSH sessions."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "dsh-session-presentation.json"
        self._lock = threading.RLock()
        self._sessions = self._load()

    def get(
        self,
        engine: str,
        session_id: str,
    ) -> SessionPresentationSnapshot:
        self._validate_engine(engine)
        try:
            session_id = _dsh_session_id(session_id)
        except ValueError as exc:
            raise SessionPresentationStoreError(
                "invalid DSH presentation session id"
            ) from exc
        with self._lock:
            snapshot = self._sessions.get(session_id)
            if snapshot is None:
                return SessionPresentationSnapshot()
            self._sessions.move_to_end(session_id)
            return snapshot

    def completion_engine(
        self,
        session_id: str,
        completion_id: str,
    ) -> str | None:
        try:
            completion_id = str(_wire_id(completion_id))
            snapshot = self.get("dsh", session_id)
        except (ValueError, SessionPresentationStoreError):
            return None
        return "dsh" if snapshot.completion_id == completion_id else None

    def mark_completion(
        self,
        engine: str,
        session_id: str,
        completion_id: str | None = None,
    ) -> SessionPresentationSnapshot:
        with self._lock:
            current = self.get(engine, session_id)
            try:
                clean_completion_id = str(_wire_id(
                    completion_id or f"completion-{uuid4().hex}"
                ))
            except ValueError as exc:
                raise SessionPresentationStoreError(
                    "invalid DSH completion id"
                ) from exc
            if (
                current.completion_id == clean_completion_id
                and current.completion_unread
            ):
                return current
            return self._replace(
                session_id,
                replace(
                    current,
                    completion_id=clean_completion_id,
                    completion_unread=True,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def acknowledge_completion(
        self,
        engine: str,
        session_id: str,
        completion_id: str,
    ) -> SessionPresentationSnapshot:
        with self._lock:
            current = self.get(engine, session_id)
            try:
                clean_completion_id = str(_wire_id(completion_id))
            except ValueError as exc:
                raise SessionPresentationStoreError(
                    "invalid DSH completion id"
                ) from exc
            if (
                current.completion_id != clean_completion_id
                or not current.completion_unread
            ):
                return current
            return self._replace(
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
        engine: str,
        session_id: str,
    ) -> SessionPresentationSnapshot:
        with self._lock:
            current = self.get(engine, session_id)
            if current.completion_id is None and not current.completion_unread:
                return current
            return self._replace(
                session_id,
                replace(
                    current,
                    completion_id=None,
                    completion_unread=False,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def delete(self, engine: str, session_id: str) -> None:
        self._validate_engine(engine)
        try:
            session_id = _dsh_session_id(session_id)
        except ValueError as exc:
            raise SessionPresentationStoreError(
                "invalid DSH presentation session id"
            ) from exc
        with self._lock:
            if session_id not in self._sessions:
                return
            updated = OrderedDict(self._sessions)
            updated.pop(session_id, None)
            self._persist_bounded(updated)
            self._sessions = updated

    @staticmethod
    def _validate_engine(engine: object) -> None:
        if engine != "dsh":
            raise SessionPresentationStoreError(
                "invalid DSH presentation engine"
            )

    def _replace(
        self,
        session_id: str,
        snapshot: SessionPresentationSnapshot,
    ) -> SessionPresentationSnapshot:
        try:
            session_id = _dsh_session_id(session_id)
        except ValueError as exc:
            raise SessionPresentationStoreError(
                "invalid DSH presentation session id"
            ) from exc
        with self._lock:
            updated = OrderedDict(self._sessions)
            updated.pop(session_id, None)
            updated[session_id] = snapshot
            while len(updated) > _PRESENTATION_MAX_ENTRIES:
                updated.popitem(last=False)
            self._persist_bounded(updated)
            self._sessions = updated
            return snapshot

    @staticmethod
    def _snapshot(value: object) -> SessionPresentationSnapshot:
        if not isinstance(value, dict) or set(value) != {
            "completion_id",
            "completion_unread",
            "completion_revision",
            "dismissed_goal_id",
            "updated_at",
        }:
            raise ValueError("DSH presentation snapshot has an invalid shape")
        completion_id = _wire_id(value.get("completion_id"), optional=True)
        dismissed_goal_id = _wire_id(
            value.get("dismissed_goal_id"), optional=True
        )
        unread = value.get("completion_unread")
        revision = value.get("completion_revision")
        updated_at = value.get("updated_at")
        if (
            not isinstance(unread, bool)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))
            or updated_at < 0
            or (unread and completion_id is None)
            or dismissed_goal_id is not None
        ):
            raise ValueError("DSH presentation snapshot is invalid")
        return SessionPresentationSnapshot(
            completion_id=completion_id,
            completion_unread=unread,
            completion_revision=revision,
            dismissed_goal_id=None,
            updated_at=float(updated_at),
        )

    def _load(self) -> OrderedDict[str, SessionPresentationSnapshot]:
        try:
            raw = json.loads(
                _read_regular_file(
                    self.path, _PRESENTATION_MAX_FILE_BYTES
                ).decode("utf-8")
            )
            if (
                not isinstance(raw, dict)
                or set(raw) != {"version", "sessions"}
                or raw.get("version") != 1
                or not isinstance(raw.get("sessions"), dict)
                or len(raw["sessions"]) > _PRESENTATION_MAX_ENTRIES
            ):
                raise ValueError("DSH presentation store has an invalid shape")
            result: OrderedDict[str, SessionPresentationSnapshot] = OrderedDict()
            for session_id, value in raw["sessions"].items():
                result[_dsh_session_id(session_id)] = self._snapshot(value)
            return result
        except FileNotFoundError:
            return OrderedDict()
        except Exception as exc:
            raise SessionPresentationStoreError(
                "DSH presentation store is unreadable"
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
        while (
            len(payload) > _PRESENTATION_MAX_FILE_BYTES
            and len(bounded) > 1
        ):
            bounded.popitem(last=False)
            payload = self._payload(bounded)
        try:
            _atomic_write(
                self.path,
                payload,
                _PRESENTATION_MAX_FILE_BYTES,
            )
        except Exception as exc:
            raise SessionPresentationStoreError(
                "DSH presentation store could not be persisted"
            ) from exc
        sessions.clear()
        sessions.update(bounded)
