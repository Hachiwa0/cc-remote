"""Durable ownership claims for turns started by cc-remote.

The official shared daemon can outlive the wrapper process.  A lease is only an
attribution hint: recovery still requires the same turn to be the rollout tail
and the official thread status to be active.  No Codex credentials or prompts
are stored here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import json
import os
from pathlib import Path
import string
import tempfile
import threading
import time
from typing import Callable, Optional


_SCHEMA_VERSION = 3
_FILENAME = "codex-turn-leases.json"
_MAX_BYTES = 64 * 1024
_MAX_LEASES = 64
_MAX_VALUE_LENGTH = 512
_MAX_STREAM_BINDINGS = 8
_MAX_STREAM_ID_LENGTH = 128


def _serialized(method):
    """Keep each durable lease operation one in-process transaction."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


@dataclass(frozen=True)
class CodexTurnStreamBinding:
    """Exact rollout task identity for one official control turn.

    Codex may expose a control/API turn id while writing the same work beneath
    a different rollout task id.  A binding is accepted only when an official
    native user item proves that relationship against this exact rollout
    inode.  No prompt text or model output is persisted here.
    """

    task_id: str
    native_message_id: str
    source_device: int
    source_inode: int


@dataclass(frozen=True)
class CodexTurnLease:
    session_id: str
    turn_id: str
    msg_id: str
    # Immutable browser owner of segment zero. ``msg_id`` moves to the latest
    # accepted steer for crash recovery, but completed History still needs the
    # original owner after the active lease is released.
    initial_msg_id: Optional[str]
    daemon_epoch: Optional[str]
    automatic: bool
    stream_bindings: tuple[CodexTurnStreamBinding, ...]
    updated_at: float

    def stream_task_ids(
        self,
        *,
        source_device: int,
        source_inode: int,
    ) -> frozenset[str]:
        """Return only bindings proven for the currently opened rollout."""
        return frozenset(
            binding.task_id
            for binding in self.stream_bindings
            if (
                binding.source_device == source_device
                and binding.source_inode == source_inode
            )
        )


class CodexTurnLeaseStore:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir).expanduser() / _FILENAME
        # Atomic replace prevents torn files, while this lock prevents two
        # wrapper tasks from losing each other's whole-file read/modify/write.
        self._lock = threading.RLock()

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

    @staticmethod
    def _valid_stream_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= _MAX_STREAM_ID_LENGTH
            and value[0] in string.ascii_letters + string.digits
            and all(
                char in string.ascii_letters + string.digits + "._:@-"
                for char in value
            )
        )

    @classmethod
    def _read_stream_bindings(
        cls,
        value: object,
    ) -> tuple[CodexTurnStreamBinding, ...] | None:
        if value is None:
            return ()
        if not isinstance(value, list) or len(value) > _MAX_STREAM_BINDINGS:
            return None
        bindings: list[CodexTurnStreamBinding] = []
        seen: set[tuple[str, str, int, int]] = set()
        for raw in value:
            if not isinstance(raw, dict):
                return None
            task_id = raw.get("task_id")
            native_message_id = raw.get("native_message_id")
            source_device = raw.get("source_device")
            source_inode = raw.get("source_inode")
            if (
                not cls._valid_stream_id(task_id)
                or not cls._valid_stream_id(native_message_id)
                or isinstance(source_device, bool)
                or not isinstance(source_device, int)
                or source_device < 0
                or isinstance(source_inode, bool)
                or not isinstance(source_inode, int)
                or source_inode < 0
            ):
                return None
            key = (
                task_id,
                native_message_id,
                source_device,
                source_inode,
            )
            if key in seen:
                continue
            seen.add(key)
            bindings.append(CodexTurnStreamBinding(
                task_id=task_id,
                native_message_id=native_message_id,
                source_device=source_device,
                source_inode=source_inode,
            ))
        return tuple(bindings)

    def _read_state(self) -> tuple[dict[str, CodexTurnLease], int]:
        try:
            if self.path.stat().st_size > _MAX_BYTES:
                return {}, 0
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}, 0
        version = raw.get("version") if isinstance(raw, dict) else None
        if version not in {1, 2, _SCHEMA_VERSION}:
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
            stream_bindings = self._read_stream_bindings(
                record.get("stream_bindings")
                if isinstance(record, dict) and version >= 3
                else None
            )
            if (
                not self._valid_text(session_id)
                or not isinstance(record, dict)
                or stream_bindings is None
                or not self._valid_text(record.get("turn_id"))
                or not self._valid_text(record.get("msg_id"))
                or (
                    record.get("initial_msg_id") is not None
                    and not self._valid_text(record.get("initial_msg_id"))
                )
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
                # A v1 record may already point at a later steer. Never guess
                # that it owns segment zero merely to migrate the file.
                initial_msg_id=(
                    record.get("initial_msg_id")
                    if version >= 2 else None
                ),
                daemon_epoch=record.get("daemon_epoch"),
                automatic=record.get("automatic", False),
                stream_bindings=stream_bindings,
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
        retained = dict(leases)

        def encode() -> str:
            return json.dumps({
                "version": _SCHEMA_VERSION,
                "profile_revision": profile_revision,
                "leases": {
                    session_id: {
                        "turn_id": lease.turn_id,
                        "msg_id": lease.msg_id,
                        "initial_msg_id": lease.initial_msg_id,
                        "daemon_epoch": lease.daemon_epoch,
                        "automatic": lease.automatic,
                        "stream_bindings": [
                            {
                                "task_id": binding.task_id,
                                "native_message_id": binding.native_message_id,
                                "source_device": binding.source_device,
                                "source_inode": binding.source_inode,
                            }
                            for binding in lease.stream_bindings
                        ],
                        "updated_at": lease.updated_at,
                    }
                    for session_id, lease in retained.items()
                },
            }, separators=(",", ":")) + "\n"

        payload = encode()
        while (
            len(payload.encode("utf-8")) > _MAX_BYTES
            and len(retained) > 1
        ):
            oldest = min(
                retained,
                key=lambda session_id: retained[session_id].updated_at,
            )
            retained.pop(oldest)
            payload = encode()
        if len(payload.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("Codex turn lease state exceeds limit")
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

    @_serialized
    def get(self, session_id: str) -> Optional[CodexTurnLease]:
        return self._read().get(session_id)

    @_serialized
    def list(self) -> tuple[CodexTurnLease, ...]:
        return tuple(sorted(
            self._read().values(),
            key=lambda lease: lease.updated_at,
            reverse=True,
        ))

    @_serialized
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
                    initial_msg_id=lease.initial_msg_id,
                    daemon_epoch=lease.daemon_epoch,
                    automatic=lease.automatic,
                    stream_bindings=lease.stream_bindings,
                    updated_at=lease.updated_at,
                )
            updated.pop(session_id, None)
            migrated += 1
        if migrated:
            self._write(updated)
        return migrated

    @_serialized
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
                initial_msg_id=lease.initial_msg_id,
                daemon_epoch=lease.daemon_epoch,
                automatic=lease.automatic,
                stream_bindings=lease.stream_bindings,
                updated_at=lease.updated_at,
            )
            updated.pop(session_id, None)
            migrated += 1
        if migrated:
            self._write(updated)
        return migrated

    @_serialized
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
                initial_msg_id=lease.initial_msg_id,
                daemon_epoch=lease.daemon_epoch,
                automatic=lease.automatic,
                stream_bindings=lease.stream_bindings,
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

    @_serialized
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
                initial_msg_id=lease.initial_msg_id,
                daemon_epoch=lease.daemon_epoch,
                automatic=lease.automatic,
                stream_bindings=lease.stream_bindings,
                updated_at=lease.updated_at,
            )
            existing = updated.get(target)
            if existing is not None and existing != candidate:
                raise ValueError("Codex profile migration collides")
            updated[target] = candidate
            migrated += target != session_id
        self._write(updated, profile_revision=profile_revision)
        return migrated

    @_serialized
    def claim(
        self,
        session_id: str,
        turn_id: str,
        msg_id: str,
        *,
        daemon_epoch: Optional[str] = None,
        automatic: bool = False,
    ) -> Optional[str]:
        if not all(self._valid_text(value)
                   for value in (session_id, turn_id, msg_id)):
            raise ValueError("invalid Codex turn lease")
        if daemon_epoch is not None and not self._valid_epoch(daemon_epoch):
            raise ValueError("invalid Codex daemon epoch")
        if not isinstance(automatic, bool):
            raise ValueError("invalid Codex automatic-turn flag")
        leases = self._read()
        current = leases.get(session_id)
        initial_msg_id = (
            current.initial_msg_id
            if current is not None and current.turn_id == turn_id
            else msg_id
        )
        stream_bindings = (
            current.stream_bindings
            if current is not None and current.turn_id == turn_id
            else ()
        )
        leases.pop(session_id, None)
        leases[session_id] = CodexTurnLease(
            session_id=session_id,
            turn_id=turn_id,
            msg_id=msg_id,
            initial_msg_id=initial_msg_id,
            daemon_epoch=daemon_epoch,
            automatic=automatic,
            stream_bindings=stream_bindings,
            updated_at=time.time(),
        )
        while len(leases) > _MAX_LEASES:
            leases.pop(next(iter(leases)))
        self._write(leases)
        return initial_msg_id

    @_serialized
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
            initial_msg_id=current.initial_msg_id,
            daemon_epoch=current.daemon_epoch,
            automatic=current.automatic,
            stream_bindings=current.stream_bindings,
            updated_at=time.time(),
        )
        self._write(leases, profile_revision=profile_revision)
        return True

    @_serialized
    def bind_stream(
        self,
        session_id: str,
        turn_id: str,
        task_id: str,
        native_message_id: str,
        *,
        source_device: int,
        source_inode: int,
        expected_msg_id: Optional[str] = None,
        daemon_epoch: Optional[str] = None,
    ) -> bool:
        """CAS one source-bound rollout task onto an existing live lease.

        Empty ``stream_bindings`` is the pending state.  Only an exact native
        user-item witness may move it to bound; a stale generation/message or a
        conflicting task for the same witness changes nothing.
        """
        if (
            not all(self._valid_text(value) for value in (session_id, turn_id))
            or not self._valid_stream_id(task_id)
            or not self._valid_stream_id(native_message_id)
            or isinstance(source_device, bool)
            or not isinstance(source_device, int)
            or source_device < 0
            or isinstance(source_inode, bool)
            or not isinstance(source_inode, int)
            or source_inode < 0
        ):
            raise ValueError("invalid Codex stream binding")
        if expected_msg_id is not None and not self._valid_text(expected_msg_id):
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
                and current.msg_id != expected_msg_id
            )
        ):
            return False
        candidate = CodexTurnStreamBinding(
            task_id=task_id,
            native_message_id=native_message_id,
            source_device=source_device,
            source_inode=source_inode,
        )
        for binding in current.stream_bindings:
            if binding == candidate:
                return True
            if (
                binding.source_device == source_device
                and binding.source_inode == source_inode
                and binding.native_message_id == native_message_id
                and binding.task_id != task_id
            ):
                return False
        bindings = (*current.stream_bindings, candidate)
        if len(bindings) > _MAX_STREAM_BINDINGS:
            bindings = bindings[-_MAX_STREAM_BINDINGS:]
        leases[session_id] = CodexTurnLease(
            session_id=current.session_id,
            turn_id=current.turn_id,
            msg_id=current.msg_id,
            initial_msg_id=current.initial_msg_id,
            daemon_epoch=current.daemon_epoch,
            automatic=current.automatic,
            stream_bindings=bindings,
            updated_at=time.time(),
        )
        self._write(leases, profile_revision=profile_revision)
        return True

    @_serialized
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
