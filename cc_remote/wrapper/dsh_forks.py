"""Durable at-most-once correlation for DSH ``session.fork``.

DSH accepts no caller-provided idempotency key.  A carrier failure can therefore
arrive after the child was created but before cc-remote received its id.  The
wrapper records the exact pre-fork catalog before crossing that mutation
boundary; retries reconcile only children added after that cut and never issue a
second fork for the same reliable browser command.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any
from uuid import uuid4

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_STATUSES = {"intent", "submitted", "uncertain", "complete", "rejected"}
_TERMINAL = {"complete", "rejected"}
_MAX_ENTRIES = 256
_MAX_BASELINE = 1000
_MAX_FILE_BYTES = 16 * 1024 * 1024


class DshForkJournalError(RuntimeError):
    """The DSH fork journal cannot safely prove an at-most-once outcome."""


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise DshForkJournalError(f"invalid {label}")
    return value


def _cwd(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8", "surrogatepass")) > 4096
        or not os.path.isabs(value)
    ):
        raise DshForkJournalError("invalid DSH fork cwd")
    canonical = os.path.realpath(value)
    if canonical != value:
        raise DshForkJournalError("DSH fork cwd is not canonical")
    return canonical


class DshForkJournal:
    """Small atomic request → child ledger stored below wrapper state_dir."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / "dsh-forks.json"
        self._lock = threading.RLock()
        self.entries = self._load()

    @staticmethod
    def _validate_entry(request_id: object, raw: object) -> dict[str, Any]:
        _safe_id(request_id, "DSH fork request id")
        if not isinstance(raw, dict) or set(raw) - {
            "parent_session_id",
            "native_parent_session_id",
            "last_turn_id",
            "at_seq",
            "cwd",
            "baseline_session_ids",
            "status",
            "session_id",
            "error_message",
            "created_at",
        }:
            raise DshForkJournalError("invalid DSH fork journal entry")
        parent = _safe_id(raw.get("parent_session_id"), "DSH parent session id")
        native_parent = _safe_id(
            raw.get("native_parent_session_id"), "DSH native parent session id"
        )
        last_turn = _safe_id(raw.get("last_turn_id"), "DSH fork point")
        at_seq = raw.get("at_seq")
        if (
            isinstance(at_seq, bool)
            or not isinstance(at_seq, int)
            # Fork submission sends ``beforeSeq = at_seq + 1``.
            or not 0 <= at_seq < MAX_SAFE_WIRE_INTEGER
        ):
            raise DshForkJournalError("invalid DSH fork sequence")
        cwd = _cwd(raw.get("cwd"))
        baseline = raw.get("baseline_session_ids")
        if (
            not isinstance(baseline, list)
            or len(baseline) > _MAX_BASELINE
            or baseline != sorted(set(baseline))
        ):
            raise DshForkJournalError("invalid DSH fork baseline")
        baseline_ids = [
            _safe_id(item, "DSH baseline session id") for item in baseline
        ]
        status_value = raw.get("status")
        if status_value not in _STATUSES:
            raise DshForkJournalError("invalid DSH fork status")
        child = raw.get("session_id")
        if status_value == "complete":
            child = _safe_id(child, "DSH child session id")
        elif child is not None:
            raise DshForkJournalError("unresolved DSH fork has a child id")
        error = raw.get("error_message")
        if status_value == "rejected":
            if not isinstance(error, str) or not error or len(error) > 512:
                raise DshForkJournalError("invalid DSH fork rejection")
        elif error is not None:
            raise DshForkJournalError("non-rejected DSH fork has an error")
        created_at = raw.get("created_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
            or created_at < 0
        ):
            raise DshForkJournalError("invalid DSH fork timestamp")
        return {
            "parent_session_id": parent,
            "native_parent_session_id": native_parent,
            "last_turn_id": last_turn,
            "at_seq": at_seq,
            "cwd": cwd,
            "baseline_session_ids": baseline_ids,
            "status": status_value,
            **({"session_id": child} if child is not None else {}),
            **({"error_message": error} if error is not None else {}),
            "created_at": float(created_at),
        }

    def _load(self) -> OrderedDict[str, dict[str, Any]]:
        result: OrderedDict[str, dict[str, Any]] = OrderedDict()
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise DshForkJournalError("DSH fork journal is not bounded")
            with self.path.open("rb") as stream:
                payload = stream.read(_MAX_FILE_BYTES + 1)
            if len(payload) > _MAX_FILE_BYTES:
                raise DshForkJournalError("DSH fork journal exceeds size limit")
            raw = json.loads(payload.decode("utf-8"))
            if not isinstance(raw, dict) or len(raw) > _MAX_ENTRIES:
                raise DshForkJournalError("invalid DSH fork journal shape")
            for request_id, entry in raw.items():
                result[_safe_id(request_id, "DSH fork request id")] = (
                    self._validate_entry(request_id, entry)
                )
        except FileNotFoundError:
            pass
        except DshForkJournalError:
            raise
        except Exception as exc:
            raise DshForkJournalError("DSH fork journal is unreadable") from exc
        return result

    def get(self, request_id: str) -> dict[str, Any] | None:
        request_id = _safe_id(request_id, "DSH fork request id")
        with self._lock:
            entry = self.entries.get(request_id)
            return dict(entry) if entry is not None else None

    def begin(
        self,
        request_id: str,
        parent_session_id: str,
        native_parent_session_id: str,
        last_turn_id: str,
        at_seq: int,
        cwd: str,
        baseline_session_ids: set[str],
    ) -> dict[str, Any]:
        request_id = _safe_id(request_id, "DSH fork request id")
        identity = {
            "parent_session_id": _safe_id(
                parent_session_id, "DSH parent session id"
            ),
            "native_parent_session_id": _safe_id(
                native_parent_session_id, "DSH native parent session id"
            ),
            "last_turn_id": _safe_id(last_turn_id, "DSH fork point"),
            "at_seq": at_seq,
            "cwd": os.path.realpath(cwd),
            "baseline_session_ids": sorted(baseline_session_ids),
        }
        candidate = self._validate_entry(request_id, {
            **identity,
            "status": "intent",
            "created_at": time.time(),
        })
        with self._lock:
            existing = self.entries.get(request_id)
            if existing is not None:
                if any(
                    existing.get(key) != value for key, value in identity.items()
                ):
                    raise DshForkJournalError(
                        "DSH fork request id was reused for another source"
                    )
                return dict(existing)
            updated = OrderedDict(self.entries)
            while len(updated) >= _MAX_ENTRIES:
                removable = next((
                    key for key, value in updated.items()
                    if value.get("status") in _TERMINAL
                ), None)
                if removable is None:
                    raise DshForkJournalError("DSH fork journal capacity exhausted")
                updated.pop(removable)
            updated[request_id] = candidate
            self._persist(updated)
            self.entries = updated
            return dict(candidate)

    def claim_submission(self, request_id: str) -> bool:
        request_id = _safe_id(request_id, "DSH fork request id")
        with self._lock:
            entry = self.entries.get(request_id)
            if entry is None:
                raise DshForkJournalError("DSH fork intent is missing")
            if entry["status"] != "intent":
                return False
            updated = OrderedDict(self.entries)
            submitted = dict(entry)
            submitted["status"] = "submitted"
            updated[request_id] = submitted
            self._persist(updated)
            self.entries = updated
            return True

    def mark_uncertain(self, request_id: str) -> None:
        self._set_status(request_id, "uncertain")

    def complete(self, request_id: str, session_id: str) -> dict[str, Any]:
        session_id = _safe_id(session_id, "DSH child session id")
        return self._set_status(request_id, "complete", session_id=session_id)

    def reject(self, request_id: str, message: str) -> dict[str, Any]:
        if not isinstance(message, str) or not message:
            raise DshForkJournalError("invalid DSH fork rejection")
        return self._set_status(
            request_id, "rejected", error_message=message[:512]
        )

    def _set_status(
        self,
        request_id: str,
        status_value: str,
        *,
        session_id: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        request_id = _safe_id(request_id, "DSH fork request id")
        with self._lock:
            entry = self.entries.get(request_id)
            if entry is None:
                raise DshForkJournalError("DSH fork intent is missing")
            if entry["status"] == "complete":
                if status_value != "complete" or entry.get("session_id") != session_id:
                    raise DshForkJournalError("DSH fork already has another child")
                return dict(entry)
            if entry["status"] == "rejected":
                if status_value != "rejected":
                    raise DshForkJournalError("DSH fork was already rejected")
                return dict(entry)
            updated_entry = dict(entry)
            updated_entry["status"] = status_value
            updated_entry.pop("session_id", None)
            updated_entry.pop("error_message", None)
            if session_id is not None:
                updated_entry["session_id"] = session_id
            if error_message is not None:
                updated_entry["error_message"] = error_message
            updated_entry = self._validate_entry(request_id, updated_entry)
            updated = OrderedDict(self.entries)
            updated[request_id] = updated_entry
            self._persist(updated)
            self.entries = updated
            return dict(updated_entry)

    def _persist(self, entries: OrderedDict[str, dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(
            f".{os.getpid()}.{uuid4().hex}.tmp"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            payload = json.dumps(
                entries, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(payload) > _MAX_FILE_BYTES:
                raise DshForkJournalError("DSH fork journal exceeds size limit")
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            if isinstance(exc, DshForkJournalError):
                raise
            raise DshForkJournalError(
                "DSH fork journal could not be persisted"
            ) from exc
