"""Durable identity aliases for browser-originated Claude user messages.

Claude Code assigns both the transcript ``uuid`` and per-turn ``promptId``
inside its own process.  Neither value is the browser's Query.msg_id.  The
wrapper learns the first real SDK-authored user UUID after a frozen transcript
append boundary; ``--replay-user-messages`` remains the exact fallback while
the matching browser turn is active. Only that identity pair is stored here.

Records are tied to the exact transcript inode.  A replaced/recreated source
must never inherit aliases from an older file that happened to reuse a session
id.  The mapping contains no prompt text or model output.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from uuid import uuid4


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_STORE_VERSION = 1
_MAX_FILE_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_ALIASES = 8192


class ClaudeClientMessageStoreError(RuntimeError):
    """A Claude client-message identity record is unsafe or malformed."""


@dataclass(frozen=True)
class _SourceIdentity:
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class _CachedRecord:
    file_signature: tuple[int, int, int]
    source: _SourceIdentity
    aliases: OrderedDict[str, str]


def _valid_id(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ClaudeClientMessageStoreError(f"invalid {name}")
    return value


def _source_identity(source_path: str | os.PathLike[str]) -> _SourceIdentity:
    try:
        path = os.path.realpath(os.fspath(source_path))
    except (TypeError, ValueError, OSError) as exc:
        raise ClaudeClientMessageStoreError("invalid Claude transcript path") from exc
    if not os.path.isabs(path) or "\x00" in path or len(path) > 4096:
        raise ClaudeClientMessageStoreError("invalid Claude transcript path")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ClaudeClientMessageStoreError(
            "Claude transcript identity is unavailable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ClaudeClientMessageStoreError(
            "Claude transcript is not a regular file")
    return _SourceIdentity(path=path, device=info.st_dev, inode=info.st_ino)


class ClaudeClientMessageStore:
    """Private, bounded per-session native UUID -> browser id journal."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        max_aliases: int = _DEFAULT_MAX_ALIASES,
    ) -> None:
        self.directory = Path(state_dir) / "claude-client-message-ids"
        self.max_aliases = max(1, int(max_aliases))
        self._lock = threading.RLock()
        self._cache: dict[str, _CachedRecord] = {}

    @staticmethod
    def _session_key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _path(self, session_id: str) -> Path:
        session_id = _valid_id(session_id, name="Claude session id")
        return self.directory / f"{self._session_key(session_id)}.json"

    @staticmethod
    def _file_signature(info: os.stat_result) -> tuple[int, int, int]:
        return info.st_ino, info.st_size, info.st_mtime_ns

    def _load(
        self,
        session_id: str,
    ) -> tuple[_SourceIdentity, OrderedDict[str, str]] | None:
        path = self._path(session_id)
        try:
            info = path.lstat()
        except FileNotFoundError:
            self._cache.pop(session_id, None)
            return None
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _MAX_FILE_BYTES
        ):
            raise ClaudeClientMessageStoreError(
                "Claude client-message store is not a private bounded file")
        signature = self._file_signature(info)
        cached = self._cache.get(session_id)
        if cached is not None and cached.file_signature == signature:
            return cached.source, OrderedDict(cached.aliases)
        try:
            raw_bytes = path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("identity store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ClaudeClientMessageStoreError(
                "Claude client-message store is unreadable") from exc
        source = raw.get("source") if isinstance(raw, dict) else None
        aliases = raw.get("aliases") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != _STORE_VERSION
            or raw.get("session_id") != session_id
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
            or len(aliases) > self.max_aliases
        ):
            raise ClaudeClientMessageStoreError(
                "Claude client-message store has invalid shape")
        loaded: OrderedDict[str, str] = OrderedDict()
        for pair in aliases:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ClaudeClientMessageStoreError(
                    "Claude client-message alias has invalid shape")
            native_id = _valid_id(pair[0], name="Claude native message id")
            client_id = _valid_id(pair[1], name="browser message id")
            if native_id in loaded:
                raise ClaudeClientMessageStoreError(
                    "Claude client-message alias is duplicated")
            loaded[native_id] = client_id
        identity = _SourceIdentity(
            path=source["path"],
            device=source["device"],
            inode=source["inode"],
        )
        self._cache[session_id] = _CachedRecord(
            file_signature=signature,
            source=identity,
            aliases=OrderedDict(loaded),
        )
        return identity, loaded

    def get(
        self,
        session_id: str,
        source_path: str | os.PathLike[str],
    ) -> dict[str, str]:
        session_id = _valid_id(session_id, name="Claude session id")
        current_source = _source_identity(source_path)
        with self._lock:
            loaded = self._load(session_id)
            if loaded is None or loaded[0] != current_source:
                return {}
            return dict(loaded[1])

    def put(
        self,
        session_id: str,
        source_path: str | os.PathLike[str],
        native_message_id: str,
        client_message_id: str,
    ) -> bool:
        session_id = _valid_id(session_id, name="Claude session id")
        native_message_id = _valid_id(
            native_message_id, name="Claude native message id")
        client_message_id = _valid_id(
            client_message_id, name="browser message id")
        source = _source_identity(source_path)
        with self._lock:
            loaded = self._load(session_id)
            aliases = (
                loaded[1] if loaded is not None and loaded[0] == source
                else OrderedDict()
            )
            if aliases.get(native_message_id) == client_message_id:
                return False
            aliases.pop(native_message_id, None)
            aliases[native_message_id] = client_message_id
            while len(aliases) > self.max_aliases:
                aliases.popitem(last=False)
            self._persist(session_id, source, aliases)
            return True

    def delete(self, session_id: str) -> None:
        session_id = _valid_id(session_id, name="Claude session id")
        path = self._path(session_id)
        with self._lock:
            self._cache.pop(session_id, None)
            try:
                path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ClaudeClientMessageStoreError(
                    "Claude client-message store could not be deleted") from exc

    def _persist(
        self,
        session_id: str,
        source: _SourceIdentity,
        aliases: OrderedDict[str, str],
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        payload = json.dumps(
            {
                "version": _STORE_VERSION,
                "session_id": session_id,
                "source": {
                    "path": source.path,
                    "device": source.device,
                    "inode": source.inode,
                },
                "aliases": list(aliases.items()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise ClaudeClientMessageStoreError(
                "Claude client-message store exceeds size limit")
        path = self._path(session_id)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
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
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            info = path.lstat()
            self._cache[session_id] = _CachedRecord(
                file_signature=self._file_signature(info),
                source=source,
                aliases=OrderedDict(aliases),
            )
        except Exception as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            if isinstance(exc, ClaudeClientMessageStoreError):
                raise
            raise ClaudeClientMessageStoreError(
                "Claude client-message store could not be persisted") from exc
