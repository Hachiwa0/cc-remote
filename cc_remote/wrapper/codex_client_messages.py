"""Durable browser-message aliases for Codex persisted history.

Codex app-server owns the native turn and user-item ids.  A browser query has a
different logical ``msg_id`` which is announced live through ``TurnBinding``.
That live binding is not guaranteed to remain in the replay ring after a
wrapper restart, so keep only the proven identity relation needed to rebuild
``ConversationTurn.clientMsgId`` later.

Records are tied to the exact rollout path/device/inode.  A replaced source
must never inherit aliases from an older file which happened to reuse native
ids.  No prompt text, model output, credentials, or account tokens are stored.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_STORE_VERSION = 1
_FILENAME = "codex-client-message-ids.json"
_MAX_FILE_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_ALIASES = 8192
_MAX_SOURCES = 256
_MAX_SEGMENT_INDEX = 4095


class CodexClientMessageStoreError(RuntimeError):
    """A Codex client-message identity record is unsafe or malformed."""


@dataclass(frozen=True)
class CodexClientMessageAliases:
    """Exact aliases usable by both official and rollout projections."""

    native_messages: dict[str, str]
    segments: dict[tuple[str, int], str]
    source_path: str | None = None
    source_device: int | None = None
    source_inode: int | None = None

    @property
    def has_aliases(self) -> bool:
        return bool(self.native_messages or self.segments)

    def matches_source(
        self,
        path: str,
        device: int,
        inode: int,
    ) -> bool:
        return (
            self.source_path == path
            and self.source_device == device
            and self.source_inode == inode
        )

    def resolve(
        self,
        native_turn_id: str,
        segment_index: int,
        native_message_id: str | None = None,
    ) -> str | None:
        if native_message_id is not None:
            client_id = self.native_messages.get(native_message_id)
            if client_id is not None:
                return client_id
        return self.segments.get((native_turn_id, segment_index))


@dataclass(frozen=True)
class _SourceIdentity:
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class _Alias:
    native_turn_id: str
    segment_index: int | None
    native_message_id: str | None
    client_message_id: str
    updated_at: float


@dataclass
class _SourceAliases:
    source: _SourceIdentity
    aliases: list[_Alias]
    updated_at: float


def _valid_id(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise CodexClientMessageStoreError(f"invalid {name}")
    return value


def _valid_segment(value: object, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_SEGMENT_INDEX
    ):
        raise CodexClientMessageStoreError("invalid Codex segment index")
    return value


def _source_identity(source_path: str | os.PathLike[str]) -> _SourceIdentity:
    try:
        path = os.path.realpath(os.fspath(source_path))
    except (TypeError, ValueError, OSError) as exc:
        raise CodexClientMessageStoreError("invalid Codex rollout path") from exc
    if not os.path.isabs(path) or "\x00" in path or len(path) > 4096:
        raise CodexClientMessageStoreError("invalid Codex rollout path")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise CodexClientMessageStoreError(
            "Codex rollout identity is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CodexClientMessageStoreError(
            "Codex rollout is not a regular file")
    return _SourceIdentity(path=path, device=info.st_dev, inode=info.st_ino)


class CodexClientMessageStore:
    """Private bounded journal of native Codex identities to browser ids."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        max_aliases: int = _DEFAULT_MAX_ALIASES,
    ) -> None:
        self.path = Path(state_dir) / _FILENAME
        self.max_aliases = max(1, int(max_aliases))
        self._lock = threading.RLock()

    def _load(self) -> list[_SourceAliases]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return []
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _MAX_FILE_BYTES
        ):
            raise CodexClientMessageStoreError(
                "Codex client-message store is not a private bounded file")
        try:
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("identity store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CodexClientMessageStoreError(
                "Codex client-message store is unreadable") from exc
        records = raw.get("sources") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != _STORE_VERSION
            or not isinstance(records, list)
            or len(records) > _MAX_SOURCES
        ):
            raise CodexClientMessageStoreError(
                "Codex client-message store has invalid shape")

        loaded: list[_SourceAliases] = []
        total_aliases = 0
        seen_sources: set[tuple[str, int, int]] = set()
        for record in records:
            source = record.get("source") if isinstance(record, dict) else None
            aliases = record.get("aliases") if isinstance(record, dict) else None
            updated_at = record.get("updated_at") if isinstance(record, dict) else None
            if (
                not isinstance(record, dict)
                or not isinstance(source, dict)
                or not isinstance(source.get("path"), str)
                or not os.path.isabs(source["path"])
                or "\x00" in source["path"]
                or len(source["path"]) > 4096
                or isinstance(source.get("device"), bool)
                or not isinstance(source.get("device"), int)
                or source["device"] < 0
                or isinstance(source.get("inode"), bool)
                or not isinstance(source.get("inode"), int)
                or source["inode"] < 0
                or not isinstance(aliases, list)
                or not isinstance(updated_at, (int, float))
                or isinstance(updated_at, bool)
            ):
                raise CodexClientMessageStoreError(
                    "Codex client-message source has invalid shape")
            identity = _SourceIdentity(
                path=source["path"],
                device=source["device"],
                inode=source["inode"],
            )
            source_key = (identity.path, identity.device, identity.inode)
            if source_key in seen_sources:
                raise CodexClientMessageStoreError(
                    "Codex client-message source is duplicated")
            seen_sources.add(source_key)
            parsed: list[_Alias] = []
            seen_segments: dict[tuple[str, int], str] = {}
            seen_messages: dict[str, str] = {}
            for alias in aliases:
                if not isinstance(alias, dict):
                    raise CodexClientMessageStoreError(
                        "Codex client-message alias has invalid shape")
                native_turn_id = _valid_id(
                    alias.get("native_turn_id"), name="Codex native turn id")
                segment_index = _valid_segment(
                    alias.get("segment_index"), optional=True)
                native_message_id = alias.get("native_message_id")
                if native_message_id is not None:
                    native_message_id = _valid_id(
                        native_message_id, name="Codex native message id")
                if segment_index is None and native_message_id is None:
                    raise CodexClientMessageStoreError(
                        "Codex alias has no exact native identity")
                client_message_id = _valid_id(
                    alias.get("client_message_id"), name="browser message id")
                alias_updated_at = alias.get("updated_at")
                if (
                    not isinstance(alias_updated_at, (int, float))
                    or isinstance(alias_updated_at, bool)
                ):
                    raise CodexClientMessageStoreError(
                        "Codex alias timestamp is invalid")
                if segment_index is not None:
                    key = (native_turn_id, segment_index)
                    owner = seen_segments.get(key)
                    if owner is not None:
                        raise CodexClientMessageStoreError(
                            "Codex segment alias is duplicated")
                    seen_segments[key] = client_message_id
                if native_message_id is not None:
                    owner = seen_messages.get(native_message_id)
                    if owner is not None:
                        raise CodexClientMessageStoreError(
                            "Codex native-message alias is duplicated")
                    seen_messages[native_message_id] = client_message_id
                parsed.append(_Alias(
                    native_turn_id=native_turn_id,
                    segment_index=segment_index,
                    native_message_id=native_message_id,
                    client_message_id=client_message_id,
                    updated_at=float(alias_updated_at),
                ))
            total_aliases += len(parsed)
            if total_aliases > self.max_aliases:
                raise CodexClientMessageStoreError(
                    "Codex client-message store exceeds alias limit")
            loaded.append(_SourceAliases(
                source=identity,
                aliases=parsed,
                updated_at=float(updated_at),
            ))
        return loaded

    def get(
        self,
        source_path: str | os.PathLike[str],
    ) -> CodexClientMessageAliases:
        source = _source_identity(source_path)
        with self._lock:
            records = self._load()
            selected = next(
                (record for record in records if record.source == source), None)
            if selected is None:
                return CodexClientMessageAliases(
                    {}, {}, source.path, source.device, source.inode)
            native_messages: dict[str, str] = {}
            segments: dict[tuple[str, int], str] = {}
            for alias in selected.aliases:
                if alias.native_message_id is not None:
                    native_messages[alias.native_message_id] = (
                        alias.client_message_id)
                if alias.segment_index is not None:
                    segments[(
                        alias.native_turn_id,
                        alias.segment_index,
                    )] = alias.client_message_id
            return CodexClientMessageAliases(
                native_messages,
                segments,
                source.path,
                source.device,
                source.inode,
            )

    def put(
        self,
        source_path: str | os.PathLike[str],
        native_turn_id: str,
        client_message_id: str,
        *,
        segment_index: int | None = None,
        native_message_id: str | None = None,
    ) -> bool:
        source = _source_identity(source_path)
        native_turn_id = _valid_id(
            native_turn_id, name="Codex native turn id")
        client_message_id = _valid_id(
            client_message_id, name="browser message id")
        segment_index = _valid_segment(segment_index, optional=True)
        if native_message_id is not None:
            native_message_id = _valid_id(
                native_message_id, name="Codex native message id")
        if segment_index is None and native_message_id is None:
            raise CodexClientMessageStoreError(
                "Codex alias has no exact native identity")

        with self._lock:
            records = self._load()
            selected = next(
                (record for record in records if record.source == source), None)
            if selected is None:
                selected = _SourceAliases(source=source, aliases=[], updated_at=0)
                records.append(selected)

            exact_segment: _Alias | None = None
            exact_message: _Alias | None = None
            client_turn_segments: list[_Alias] = []
            for alias in selected.aliases:
                is_exact_segment = bool(
                    segment_index is not None
                    and alias.segment_index == segment_index
                    and alias.native_turn_id == native_turn_id
                )
                is_exact_message = bool(
                    native_message_id is not None
                    and alias.native_message_id == native_message_id
                )
                if is_exact_segment:
                    exact_segment = alias
                    if alias.client_message_id != client_message_id:
                        raise CodexClientMessageStoreError(
                            "Codex segment alias conflicts with durable identity")
                if is_exact_message:
                    exact_message = alias
                    if (
                        alias.client_message_id != client_message_id
                        or alias.native_turn_id != native_turn_id
                    ):
                        raise CodexClientMessageStoreError(
                            "Codex native-message alias conflicts with "
                            "durable identity")
                if (
                    alias.native_turn_id == native_turn_id
                    and alias.client_message_id == client_message_id
                    and alias.segment_index is not None
                ):
                    client_turn_segments.append(alias)

            if segment_index is not None:
                if any(
                    alias.segment_index != segment_index
                    for alias in client_turn_segments
                ):
                    raise CodexClientMessageStoreError(
                        "Codex client-message segment upgrade conflicts")
            if (
                (segment_index is None or exact_segment is not None)
                and (native_message_id is None or exact_message is not None)
            ):
                return False

            # Segment positions and app-server user-item ids are independent
            # exact keys.  Codex can expose the same logical user input once
            # through the live rollout stream and again through official
            # History with a different native message id.  Preserve both
            # aliases instead of treating the second id as an unsafe upgrade.
            new_segment = segment_index if exact_segment is None else None
            new_message = native_message_id if exact_message is None else None

            now = time.time()
            selected.aliases.append(_Alias(
                native_turn_id=native_turn_id,
                segment_index=new_segment,
                native_message_id=new_message,
                client_message_id=client_message_id,
                updated_at=now,
            ))
            selected.updated_at = now
            self._prune(records)
            self._persist(records)
            return True

    def delete_path(self, source_path: str | os.PathLike[str]) -> bool:
        try:
            path = os.path.realpath(os.fspath(source_path))
        except (TypeError, ValueError, OSError) as exc:
            raise CodexClientMessageStoreError(
                "invalid Codex rollout path") from exc
        if not os.path.isabs(path) or "\x00" in path or len(path) > 4096:
            raise CodexClientMessageStoreError("invalid Codex rollout path")
        with self._lock:
            records = self._load()
            retained = [record for record in records if record.source.path != path]
            if len(retained) == len(records):
                return False
            if retained:
                self._persist(retained)
            else:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            return True

    def _prune(self, records: list[_SourceAliases]) -> None:
        records.sort(key=lambda record: record.updated_at)
        while len(records) > _MAX_SOURCES:
            records.pop(0)
        while sum(len(record.aliases) for record in records) > self.max_aliases:
            oldest = min(
                (
                    (alias.updated_at, source_index, alias_index)
                    for source_index, record in enumerate(records)
                    for alias_index, alias in enumerate(record.aliases)
                ),
                default=None,
            )
            if oldest is None:
                break
            _updated_at, source_index, alias_index = oldest
            records[source_index].aliases.pop(alias_index)
            if not records[source_index].aliases:
                records.pop(source_index)

    @staticmethod
    def _payload(records: list[_SourceAliases]) -> bytes:
        return json.dumps(
            {
                "version": _STORE_VERSION,
                "sources": [{
                    "source": {
                        "path": record.source.path,
                        "device": record.source.device,
                        "inode": record.source.inode,
                    },
                    "updated_at": record.updated_at,
                    "aliases": [{
                        "native_turn_id": alias.native_turn_id,
                        "segment_index": alias.segment_index,
                        "native_message_id": alias.native_message_id,
                        "client_message_id": alias.client_message_id,
                        "updated_at": alias.updated_at,
                    } for alias in record.aliases],
                } for record in records],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _persist(self, records: list[_SourceAliases]) -> None:
        payload = self._payload(records)
        while len(payload) > _MAX_FILE_BYTES:
            before = sum(len(record.aliases) for record in records)
            self._prune_to_count(records, max(0, before - 1))
            after = sum(len(record.aliases) for record in records)
            if after >= before:
                raise CodexClientMessageStoreError(
                    "Codex client-message store exceeds size limit")
            payload = self._payload(records)

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            if isinstance(exc, CodexClientMessageStoreError):
                raise
            raise CodexClientMessageStoreError(
                "Codex client-message store could not be persisted") from exc

    @staticmethod
    def _prune_to_count(
        records: list[_SourceAliases], target: int,
    ) -> None:
        while sum(len(record.aliases) for record in records) > target:
            oldest = min(
                (
                    (alias.updated_at, source_index, alias_index)
                    for source_index, record in enumerate(records)
                    for alias_index, alias in enumerate(record.aliases)
                ),
                default=None,
            )
            if oldest is None:
                break
            _updated_at, source_index, alias_index = oldest
            records[source_index].aliases.pop(alias_index)
            if not records[source_index].aliases:
                records.pop(source_index)
