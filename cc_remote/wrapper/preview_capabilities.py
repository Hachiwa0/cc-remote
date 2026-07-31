"""Durable, exact-file capabilities for previews outside a session cwd."""
from __future__ import annotations

import math
import os
import sqlite3
import stat
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cc_remote.log import logger

log = logger("cc_remote.wrapper.preview_capabilities")

PreviewMode = Literal["read", "read_write"]
PreviewCapabilitySource = Literal[
    "engine_observed", "structured_write", "user_approved",
]

_GLOBAL_CAP = 4096
_SESSION_CAP = 256
PREVIEW_PATH_MAX_BYTES = 4096


class PreviewCapabilityError(ValueError):
    """A requested external file cannot safely become a preview capability."""


@dataclass(frozen=True)
class PreviewCapability:
    engine: str
    space: str
    session_id: str
    path: str
    device: int
    inode: int
    uid: int
    mode: PreviewMode
    source: PreviewCapabilitySource
    granted_at: float

    def matches(
        self,
        file_stat: os.stat_result,
        *,
        require_write: bool = False,
    ) -> bool:
        return (
            stat.S_ISREG(file_stat.st_mode)
            and file_stat.st_dev == self.device
            and file_stat.st_ino == self.inode
            and getattr(file_stat, "st_uid", -1) == self.uid
            and self.uid == os.geteuid()
            and (not require_write or self.mode == "read_write")
        )


CapabilityKey = tuple[str, str, str, str]


class PreviewCapabilityStore:
    """Bounded in-memory lookup with an atomic SQLite durability layer.

    The state is only an allow capability, never source-of-truth file content.
    If the database is unavailable or malformed, the store starts empty and
    therefore fails closed by asking the user again.
    """

    def __init__(self, state_dir: Path):
        self._path = Path(state_dir) / "preview-capabilities.sqlite3"
        self._entries: OrderedDict[
            CapabilityKey, PreviewCapability
        ] = OrderedDict()
        self._persistent = True
        try:
            self._initialize()
            self._load()
        except Exception as exc:
            self._persistent = False
            self._entries.clear()
            log.warning(
                "preview capability store unavailable; using fail-closed memory",
                error_type=type(exc).__name__,
            )

    @staticmethod
    def _key(
        engine: str,
        space: str,
        session_id: str,
        path: str,
    ) -> CapabilityKey:
        return engine, space, session_id, path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        connection = sqlite3.connect(self._path, timeout=1.0)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            connection.close()
            raise
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS preview_capabilities (
                    engine TEXT NOT NULL,
                    space TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    device TEXT NOT NULL,
                    inode TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    granted_at REAL NOT NULL,
                    PRIMARY KEY (engine, space, session_id, path)
                )
                """
            )

    @staticmethod
    def _valid_text(value: object, *, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and "\x00" not in value
            and len(value.encode("utf-8", "surrogatepass")) <= maximum
        )

    @classmethod
    def _from_row(cls, row: tuple[object, ...]) -> PreviewCapability | None:
        (
            engine, space, session_id, path, device, inode, uid,
            mode, source, granted_at,
        ) = row
        if (
            engine not in {"claude", "codex"}
            or space not in {"code", "work"}
            or not cls._valid_text(session_id, maximum=128)
            or not cls._valid_text(path, maximum=PREVIEW_PATH_MAX_BYTES)
            or mode not in {"read", "read_write"}
            or source not in {
                "engine_observed", "structured_write", "user_approved",
            }
            or not isinstance(granted_at, (int, float))
            or not math.isfinite(float(granted_at))
            or granted_at < 0
        ):
            return None
        try:
            numeric = tuple(int(value) for value in (device, inode, uid))
        except (TypeError, ValueError):
            return None
        if any(value < 0 for value in numeric):
            return None
        return PreviewCapability(
            engine=engine,
            space=space,
            session_id=session_id,
            path=path,
            device=numeric[0],
            inode=numeric[1],
            uid=numeric[2],
            mode=mode,
            source=source,
            granted_at=float(granted_at),
        )

    def _load(self) -> None:
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM preview_capabilities"
            ).fetchone()[0]
            if count > _GLOBAL_CAP:
                connection.execute(
                    """
                    DELETE FROM preview_capabilities
                    WHERE rowid IN (
                        SELECT rowid
                        FROM preview_capabilities
                        ORDER BY granted_at ASC
                        LIMIT ?
                    )
                    """,
                    (count - _GLOBAL_CAP,),
                )
            rows = connection.execute(
                """
                SELECT engine, space, session_id, path, device, inode, uid,
                       mode, source, granted_at
                FROM preview_capabilities
                ORDER BY granted_at ASC
                """,
            ).fetchall()
        for row in rows:
            capability = self._from_row(row)
            if capability is None:
                raise ValueError("preview capability store contains invalid data")
            key = self._key(
                capability.engine,
                capability.space,
                capability.session_id,
                capability.path,
            )
            self._entries[key] = capability
        removed = self._trim_entries()
        if removed:
            with self._connect() as connection:
                for key in removed:
                    connection.execute(
                        """
                        DELETE FROM preview_capabilities
                        WHERE engine=? AND space=? AND session_id=? AND path=?
                        """,
                        key,
                    )

    def _trim_entries(self) -> list[CapabilityKey]:
        removed: list[CapabilityKey] = []
        counts: dict[tuple[str, str, str], int] = {}
        for key in self._entries:
            scope = key[:3]
            counts[scope] = counts.get(scope, 0) + 1
        for key in tuple(self._entries):
            scope = key[:3]
            if counts[scope] <= _SESSION_CAP:
                continue
            self._entries.pop(key, None)
            counts[scope] -= 1
            removed.append(key)
        while len(self._entries) > _GLOBAL_CAP:
            dropped, _ = self._entries.popitem(last=False)
            removed.append(dropped)
        return removed

    @staticmethod
    def _canonical_path(path: str) -> str:
        if (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or len(path.encode("utf-8", "surrogatepass"))
            > PREVIEW_PATH_MAX_BYTES
        ):
            raise PreviewCapabilityError("预览路径无效")
        canonical = os.path.realpath(path)
        if (
            not canonical
            or "\x00" in canonical
            or len(canonical.encode("utf-8", "surrogatepass"))
            > PREVIEW_PATH_MAX_BYTES
        ):
            raise PreviewCapabilityError("预览路径过长")
        return canonical

    @staticmethod
    def _inspect_regular_file(path: str) -> os.stat_result:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise PreviewCapabilityError("文件不存在") from exc
        except PermissionError as exc:
            raise PreviewCapabilityError("没有权限读取该文件") from exc
        except OSError as exc:
            raise PreviewCapabilityError("无法安全打开该文件") from exc
        try:
            file_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PreviewCapabilityError("预览目标必须是普通文件")
        if getattr(file_stat, "st_uid", -1) != os.geteuid():
            raise PreviewCapabilityError("只允许预览当前用户拥有的文件")
        return file_stat

    def _persist_grant(
        self,
        capability: PreviewCapability,
        removed: list[CapabilityKey],
    ) -> None:
        if not self._persistent:
            return
        try:
            with self._connect() as connection:
                for key in removed:
                    connection.execute(
                        """
                        DELETE FROM preview_capabilities
                        WHERE engine=? AND space=? AND session_id=? AND path=?
                        """,
                        key,
                    )
                connection.execute(
                    """
                    INSERT INTO preview_capabilities (
                        engine, space, session_id, path, device, inode, uid,
                        mode, source, granted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(engine, space, session_id, path) DO UPDATE SET
                        device=excluded.device,
                        inode=excluded.inode,
                        uid=excluded.uid,
                        mode=excluded.mode,
                        source=excluded.source,
                        granted_at=excluded.granted_at
                    """,
                    (
                        capability.engine,
                        capability.space,
                        capability.session_id,
                        capability.path,
                        str(capability.device),
                        str(capability.inode),
                        str(capability.uid),
                        capability.mode,
                        capability.source,
                        capability.granted_at,
                    ),
                )
        except Exception as exc:
            self._persistent = False
            log.warning(
                "preview capability grant not persisted",
                error_type=type(exc).__name__,
            )

    def grant_path(
        self,
        engine: str,
        space: str,
        session_id: str,
        path: str,
        *,
        mode: PreviewMode,
        source: PreviewCapabilitySource,
        persist: bool = True,
    ) -> PreviewCapability:
        capability = self.inspect_path(
            engine,
            space,
            session_id,
            path,
            mode=mode,
            source=source,
        )
        canonical = capability.path
        key = self._key(engine, space, session_id, canonical)
        previous = self._entries.get(key)
        same_identity = (
            previous is not None
            and previous.device == capability.device
            and previous.inode == capability.inode
            and previous.uid == capability.uid
        )
        if same_identity and previous.mode == "read_write":
            capability = PreviewCapability(
                engine=capability.engine,
                space=capability.space,
                session_id=capability.session_id,
                path=capability.path,
                device=capability.device,
                inode=capability.inode,
                uid=capability.uid,
                mode="read_write",
                source=previous.source,
                granted_at=capability.granted_at,
            )
        self._entries.pop(key, None)
        self._entries[key] = capability

        removed = self._trim_entries()
        if persist:
            self._persist_grant(capability, removed)
        return capability

    def inspect_path(
        self,
        engine: str,
        space: str,
        session_id: str,
        path: str,
        *,
        mode: PreviewMode,
        source: PreviewCapabilitySource,
    ) -> PreviewCapability:
        """Inspect one exact regular file without retaining a capability.

        Successful engine-side image reads use this to bind a one-shot byte
        snapshot to the file identity observed at that tool boundary. They must
        not silently turn a transient read into durable future path access.
        """
        if engine not in {"claude", "codex"} or space not in {"code", "work"}:
            raise PreviewCapabilityError("会话范围无效")
        if not self._valid_text(session_id, maximum=128):
            raise PreviewCapabilityError("会话标识无效")
        if mode not in {"read", "read_write"}:
            raise PreviewCapabilityError("预览权限无效")
        if source not in {
            "engine_observed", "structured_write", "user_approved",
        }:
            raise PreviewCapabilityError("预览权限来源无效")
        canonical = self._canonical_path(path)
        file_stat = self._inspect_regular_file(canonical)
        return PreviewCapability(
            engine=engine,
            space=space,
            session_id=session_id,
            path=canonical,
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            uid=file_stat.st_uid,
            mode=mode,
            source=source,
            granted_at=time.time(),
        )

    def snapshot(
        self,
        engine: str,
        space: str,
        session_id: str,
        *,
        require_write: bool = False,
    ) -> dict[str, PreviewCapability]:
        return {
            path: capability
            for (entry_engine, entry_space, entry_sid, path), capability
            in self._entries.items()
            if (
                entry_engine == engine
                and entry_space == space
                and entry_sid == session_id
                and (not require_write or capability.mode == "read_write")
            )
        }

    def revoke(
        self,
        engine: str,
        space: str,
        session_id: str,
        path: str,
    ) -> None:
        canonical = self._canonical_path(path)
        key = self._key(engine, space, session_id, canonical)
        self._entries.pop(key, None)
        if not self._persistent:
            return
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    DELETE FROM preview_capabilities
                    WHERE engine=? AND space=? AND session_id=? AND path=?
                    """,
                    key,
                )
        except Exception as exc:
            self._persistent = False
            log.warning(
                "preview capability revoke not persisted",
                error_type=type(exc).__name__,
            )

    def rekey(
        self,
        engine: str,
        space: str,
        old_session_id: str,
        new_session_id: str,
        *,
        persist: bool = True,
    ) -> None:
        moving = [
            (key, capability)
            for key, capability in self._entries.items()
            if key[:3] == (engine, space, old_session_id)
        ]
        if not moving or old_session_id == new_session_id:
            return
        for old_key, capability in moving:
            self._entries.pop(old_key, None)
            new_key = self._key(
                engine, space, new_session_id, capability.path)
            existing = self._entries.get(new_key)
            same_identity = (
                existing is not None
                and existing.device == capability.device
                and existing.inode == capability.inode
                and existing.uid == capability.uid
            )
            selected = (
                existing
                if existing is not None
                and not same_identity
                and existing.granted_at >= capability.granted_at
                else capability
            )
            mode: PreviewMode = selected.mode
            source = selected.source
            if same_identity and existing is not None:
                mode = (
                    "read_write"
                    if capability.mode == "read_write"
                    or existing.mode == "read_write"
                    else "read"
                )
                source = (
                    capability.source if mode == capability.mode
                    else existing.source
                )
            self._entries[new_key] = PreviewCapability(
                engine=engine,
                space=space,
                session_id=new_session_id,
                path=selected.path,
                device=selected.device,
                inode=selected.inode,
                uid=selected.uid,
                mode=mode,
                source=source,
                granted_at=(
                    max(capability.granted_at, existing.granted_at)
                    if same_identity and existing is not None
                    else selected.granted_at
                ),
            )
        self._entries = OrderedDict(sorted(
            self._entries.items(),
            key=lambda item: item[1].granted_at,
        ))
        removed = self._trim_entries()
        if not persist or not self._persistent:
            return
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    DELETE FROM preview_capabilities
                    WHERE engine=? AND space=? AND session_id=?
                    """,
                    (engine, space, old_session_id),
                )
                for key in removed:
                    connection.execute(
                        """
                        DELETE FROM preview_capabilities
                        WHERE engine=? AND space=? AND session_id=? AND path=?
                        """,
                        key,
                    )
                for key, capability in self._entries.items():
                    if key[:3] != (engine, space, new_session_id):
                        continue
                    connection.execute(
                        """
                        INSERT INTO preview_capabilities (
                            engine, space, session_id, path, device, inode, uid,
                            mode, source, granted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(engine, space, session_id, path) DO UPDATE SET
                            device=excluded.device,
                            inode=excluded.inode,
                            uid=excluded.uid,
                            mode=excluded.mode,
                            source=excluded.source,
                            granted_at=excluded.granted_at
                        """,
                        (
                            capability.engine,
                            capability.space,
                            capability.session_id,
                            capability.path,
                            str(capability.device),
                            str(capability.inode),
                            str(capability.uid),
                            capability.mode,
                            capability.source,
                            capability.granted_at,
                        ),
                    )
        except Exception as exc:
            self._persistent = False
            log.warning(
                "preview capability rekey not persisted",
                error_type=type(exc).__name__,
            )

    def remove_session(self, engine: str, session_id: str) -> None:
        removed = [
            key for key in self._entries
            if key[0] == engine and key[2] == session_id
        ]
        for key in removed:
            self._entries.pop(key, None)
        if not self._persistent:
            return
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    DELETE FROM preview_capabilities
                    WHERE engine=? AND session_id=?
                    """,
                    (engine, session_id),
                )
        except Exception as exc:
            self._persistent = False
            log.warning(
                "preview capability session cleanup not persisted",
                error_type=type(exc).__name__,
            )
