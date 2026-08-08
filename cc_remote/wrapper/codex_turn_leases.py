"""Durable ownership claims for turns started by cc-remote.

The official shared daemon can outlive the wrapper process.  A lease is only an
attribution hint: recovery still requires the same turn to be the rollout tail
and the official thread status to be active.  No Codex credentials or prompts
are stored here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import string
import tempfile
import time
from typing import Callable, Optional


_SCHEMA_VERSION = 1
_FILENAME = "codex-turn-leases.json"
_MAX_BYTES = 64 * 1024
_MAX_LEASES = 64
_MAX_VALUE_LENGTH = 512


@dataclass(frozen=True)
class CodexTurnLease:
    session_id: str
    turn_id: str
    msg_id: str
    daemon_epoch: Optional[str]
    automatic: bool
    updated_at: float


class CodexTurnLeaseStore:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir).expanduser() / _FILENAME

    @staticmethod
    def _valid_text(value: object) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= _MAX_VALUE_LENGTH
            and "\x00" not in value
        )

    @staticmethod
    def _valid_epoch(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 32
            and all(char in string.hexdigits for char in value)
        )

    def _read_state(self) -> tuple[dict[str, CodexTurnLease], int]:
        try:
            if self.path.stat().st_size > _MAX_BYTES:
                return {}, 0
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}, 0
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            return {}, 0
        profile_revision = raw.get("profile_revision", 0)
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 0
        ):
            return {}, 0
        records = raw.get("leases")
        if not isinstance(records, dict) or len(records) > _MAX_LEASES:
            return {}, 0
        leases: dict[str, CodexTurnLease] = {}
        for session_id, record in records.items():
            if (
                not self._valid_text(session_id)
                or not isinstance(record, dict)
                or not self._valid_text(record.get("turn_id"))
                or not self._valid_text(record.get("msg_id"))
                or (
                    record.get("daemon_epoch") is not None
                    and not self._valid_epoch(record.get("daemon_epoch"))
                )
                or not isinstance(record.get("automatic", False), bool)
                or not isinstance(record.get("updated_at"), (int, float))
                or isinstance(record.get("updated_at"), bool)
            ):
                continue
            leases[session_id] = CodexTurnLease(
                session_id=session_id,
                turn_id=record["turn_id"],
                msg_id=record["msg_id"],
                daemon_epoch=record.get("daemon_epoch"),
                automatic=record.get("automatic", False),
                updated_at=float(record["updated_at"]),
            )
        return leases, profile_revision

    def _read(self) -> dict[str, CodexTurnLease]:
        return self._read_state()[0]

    def _write(
        self,
        leases: dict[str, CodexTurnLease],
        *,
        profile_revision: int | None = None,
    ) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if profile_revision is None:
            profile_revision = self._read_state()[1]
        payload = json.dumps({
            "version": _SCHEMA_VERSION,
            "profile_revision": profile_revision,
            "leases": {
                session_id: {
                    "turn_id": lease.turn_id,
                    "msg_id": lease.msg_id,
                    "daemon_epoch": lease.daemon_epoch,
                    "automatic": lease.automatic,
                    "updated_at": lease.updated_at,
                }
                for session_id, lease in leases.items()
            },
        }, separators=(",", ":")) + "\n"
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def get(self, session_id: str) -> Optional[CodexTurnLease]:
        return self._read().get(session_id)

    def list(self) -> tuple[CodexTurnLease, ...]:
        return tuple(sorted(
            self._read().values(),
            key=lambda lease: lease.updated_at,
            reverse=True,
        ))

    def namespace_legacy_sessions(self, profile_id: str) -> int:
        """Move pre-multi-account leases into the default wire namespace."""
        if not self._valid_text(profile_id) or "@" in profile_id:
            raise ValueError("invalid Codex profile id")
        leases = self._read()
        updated = dict(leases)
        migrated = 0
        for session_id, lease in tuple(leases.items()):
            if "@" in session_id:
                continue
            target = f"{profile_id}@{session_id}"
            if not self._valid_text(target):
                continue
            if target not in updated:
                updated[target] = CodexTurnLease(
                    session_id=target,
                    turn_id=lease.turn_id,
                    msg_id=lease.msg_id,
                    daemon_epoch=lease.daemon_epoch,
                    automatic=lease.automatic,
                    updated_at=lease.updated_at,
                )
            updated.pop(session_id, None)
            migrated += 1
        if migrated:
            self._write(updated)
        return migrated

    def denamespace_profile_sessions(self, profile_id: str) -> int:
        """Activate one profile in single-account mode without dropping siblings."""
        if not self._valid_text(profile_id) or "@" in profile_id:
            raise ValueError("invalid Codex profile id")
        prefix = f"{profile_id}@"
        leases = self._read()
        updated = dict(leases)
        migrated = 0
        for session_id, lease in tuple(leases.items()):
            if not session_id.startswith(prefix):
                continue
            native_id = session_id[len(prefix):]
            if not self._valid_text(native_id) or "@" in native_id:
                continue
            updated[native_id] = CodexTurnLease(
                session_id=native_id,
                turn_id=lease.turn_id,
                msg_id=lease.msg_id,
                daemon_epoch=lease.daemon_epoch,
                automatic=lease.automatic,
                updated_at=lease.updated_at,
            )
            updated.pop(session_id, None)
            migrated += 1
        if migrated:
            self._write(updated)
        return migrated

    def remap_profile_sessions(self, remaps: dict[str, str]) -> int:
        leases = self._read()
        updated = dict(leases)
        moves: list[tuple[str, str, CodexTurnLease]] = []
        for session_id, lease in tuple(leases.items()):
            if "@" not in session_id:
                continue
            old_id, native_id = session_id.split("@", 1)
            new_id = remaps.get(old_id)
            if not new_id or new_id == old_id:
                continue
            target = f"{new_id}@{native_id}"
            if not self._valid_text(target):
                continue
            moves.append((session_id, target, CodexTurnLease(
                session_id=target,
                turn_id=lease.turn_id,
                msg_id=lease.msg_id,
                daemon_epoch=lease.daemon_epoch,
                automatic=lease.automatic,
                updated_at=lease.updated_at,
            )))
        sources = {source for source, _target, _lease in moves}
        for source in sources:
            updated.pop(source, None)
        for _source, target, lease in moves:
            if target not in updated or target in sources:
                updated[target] = lease
        migrated = len(moves)
        if migrated:
            self._write(updated)
        return migrated

    def migrate_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Atomically translate lease identities once per topology revision."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise ValueError("invalid Codex profile revision")
        leases, durable_revision = self._read_state()
        if durable_revision >= profile_revision:
            return 0
        updated: dict[str, CodexTurnLease] = {}
        migrated = 0
        for session_id, lease in leases.items():
            target = transform(session_id)
            if not self._valid_text(target):
                raise ValueError("invalid migrated Codex session id")
            candidate = CodexTurnLease(
                session_id=target,
                turn_id=lease.turn_id,
                msg_id=lease.msg_id,
                daemon_epoch=lease.daemon_epoch,
                automatic=lease.automatic,
                updated_at=lease.updated_at,
            )
            existing = updated.get(target)
            if existing is not None and existing != candidate:
                raise ValueError("Codex profile migration collides")
            updated[target] = candidate
            migrated += target != session_id
        self._write(updated, profile_revision=profile_revision)
        return migrated

    def claim(
        self,
        session_id: str,
        turn_id: str,
        msg_id: str,
        *,
        daemon_epoch: Optional[str] = None,
        automatic: bool = False,
    ) -> None:
        if not all(self._valid_text(value)
                   for value in (session_id, turn_id, msg_id)):
            raise ValueError("invalid Codex turn lease")
        if daemon_epoch is not None and not self._valid_epoch(daemon_epoch):
            raise ValueError("invalid Codex daemon epoch")
        if not isinstance(automatic, bool):
            raise ValueError("invalid Codex automatic-turn flag")
        leases = self._read()
        leases.pop(session_id, None)
        leases[session_id] = CodexTurnLease(
            session_id=session_id,
            turn_id=turn_id,
            msg_id=msg_id,
            daemon_epoch=daemon_epoch,
            automatic=automatic,
            updated_at=time.time(),
        )
        while len(leases) > _MAX_LEASES:
            leases.pop(next(iter(leases)))
        self._write(leases)

    def rebind(
        self,
        session_id: str,
        turn_id: str,
        msg_id: str,
        *,
        expected_msg_id: Optional[str] = None,
        daemon_epoch: Optional[str] = None,
    ) -> bool:
        """Move one live lease to a newer logical message boundary.

        A native Codex turn may contain several accepted user items.  Recovery
        needs the latest visible segment, but a stale wrapper must never replace
        a newer segment merely because both messages share the same native turn.
        This compare-and-swap therefore preserves every non-message lease field
        and changes nothing unless the durable owner still matches the caller's
        exact turn, daemon generation, and (when supplied) previous message.
        """
        if not all(self._valid_text(value)
                   for value in (session_id, turn_id, msg_id)):
            raise ValueError("invalid Codex turn lease rebind")
        if (
            expected_msg_id is not None
            and not self._valid_text(expected_msg_id)
        ):
            raise ValueError("invalid expected Codex message id")
        if daemon_epoch is not None and not self._valid_epoch(daemon_epoch):
            raise ValueError("invalid Codex daemon epoch")

        leases, profile_revision = self._read_state()
        current = leases.get(session_id)
        if (
            current is None
            or current.turn_id != turn_id
            or current.daemon_epoch != daemon_epoch
            or (
                expected_msg_id is not None
                and current.msg_id not in {expected_msg_id, msg_id}
            )
        ):
            return False
        if current.msg_id == msg_id:
            return True
        leases[session_id] = CodexTurnLease(
            session_id=session_id,
            turn_id=current.turn_id,
            msg_id=msg_id,
            daemon_epoch=current.daemon_epoch,
            automatic=current.automatic,
            updated_at=time.time(),
        )
        self._write(leases, profile_revision=profile_revision)
        return True

    def release(
        self, session_id: str, *, turn_id: Optional[str] = None,
    ) -> bool:
        leases = self._read()
        current = leases.get(session_id)
        if current is None or (
            turn_id is not None and current.turn_id != turn_id
        ):
            return False
        leases.pop(session_id, None)
        self._write(leases)
        return True
