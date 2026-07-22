"""Persistent, rebuildable materialized history pages.

The engine transcript/rollout remains the source of truth.  This store keeps
only derived conversation pages so opening a session does not repeatedly scan
and translate the same JSONL bytes.  Every row is bound to a strong-enough
source fingerprint and can therefore be discarded without affecting engine
state or recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SCHEMA_VERSION = 3
_FINGERPRINT_SAMPLE_BYTES = 64 * 1024
_DEFAULT_MAX_ENTRIES = 128
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_SUMMARY_PROMPT_MAX_CHARS = 128 * 1024
_SUMMARY_TEXT_MAX_CHARS = 256 * 1024
_VOLATILE_EVENT_FIELDS = frozenset({"ts", "seq", "to", "route_id"})


@dataclass(frozen=True)
class HistorySourceFingerprint:
    """Identity of the exact transcript snapshot used to build a page."""

    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    sample_sha256: str

    @classmethod
    def capture(cls, path: str | os.PathLike[str]) -> "HistorySourceFingerprint":
        resolved = os.path.realpath(os.fspath(path))
        with open(resolved, "rb") as source:
            stat = os.fstat(source.fileno())
            digest = hashlib.sha256()
            head = source.read(_FINGERPRINT_SAMPLE_BYTES)
            digest.update(head)
            if stat.st_size > _FINGERPRINT_SAMPLE_BYTES:
                source.seek(max(0, stat.st_size - _FINGERPRINT_SAMPLE_BYTES))
                digest.update(source.read(_FINGERPRINT_SAMPLE_BYTES))
        return cls(
            path=resolved,
            device=int(stat.st_dev),
            inode=int(stat.st_ino),
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            sample_sha256=digest.hexdigest(),
        )

    @property
    def token(self) -> str:
        payload = "\0".join((
            self.path,
            str(self.device),
            str(self.inode),
            str(self.size),
            str(self.mtime_ns),
            self.sample_sha256,
        ))
        return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class MaterializedHistoryPage:
    """Immutable narrative projection stored independently of live control."""

    events: tuple[dict[str, Any], ...]
    has_more: bool
    oldest_id: str | None
    newest_id: str | None
    turns: tuple[dict[str, Any], ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "events": list(self.events),
            "has_more": self.has_more,
            "oldest_id": self.oldest_id,
            "newest_id": self.newest_id,
            "turns": list(self.turns),
        }

    def semantic_token(self) -> str:
        """Hash narrative identity while ignoring transport-time metadata.

        A few legacy translators construct synthetic start/end envelopes after
        reading the transcript, so their default ``ts`` can vary across equal
        parses.  That timestamp is useful for the first materialization but is
        not evidence that the underlying conversation changed.
        """
        events = [
            {key: value for key, value in event.items()
             if key not in _VOLATILE_EVENT_FIELDS}
            for event in self.events
        ]
        payload = {
            "events": events,
            "has_more": self.has_more,
            "oldest_id": self.oldest_id,
            "newest_id": self.newest_id,
            "turns": list(self.turns),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def semantically_equals(self, other: "MaterializedHistoryPage") -> bool:
        return self.semantic_token() == other.semantic_token()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MaterializedHistoryPage":
        events = payload.get("events")
        if not isinstance(events, list) or not all(
                isinstance(event, dict) for event in events):
            raise ValueError("invalid materialized history events")
        turns = payload.get("turns")
        if turns is None:
            turns = materialize_history_turns(events)
        if not isinstance(turns, (list, tuple)) or not all(
                isinstance(turn, dict) for turn in turns):
            raise ValueError("invalid materialized history turns")
        return cls(
            events=tuple(events),
            has_more=bool(payload.get("has_more")),
            oldest_id=(payload.get("oldest_id")
                       if isinstance(payload.get("oldest_id"), str) else None),
            newest_id=(payload.get("newest_id")
                       if isinstance(payload.get("newest_id"), str) else None),
            turns=tuple(turns),
        )


def _event_ms(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) * 1000)


def group_history_events(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[list[dict[str, Any]]]:
    """Split translated wire events into stable completed/in-flight turns."""
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("type")
        if event_type in {"model", "effort", "perm", "fast",
                          "collaboration_mode", "session_control"}:
            continue
        if event_type == "user_msg" and current:
            groups.append(current)
            current = []
        current.append(event)
        if event_type == "turn_end":
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _turn_id(group: list[dict[str, Any]]) -> str | None:
    for event in group:
        if event.get("type") == "user_msg" and isinstance(event.get("msg_id"), str):
            return event["msg_id"]
    for event in reversed(group):
        if event.get("type") == "turn_end" and isinstance(event.get("turn_id"), str):
            return event["turn_id"]
    for field in ("message_id", "item_id", "tool_use_id"):
        for event in group:
            if isinstance(event.get(field), str):
                return event[field]
    return None


def materialize_history_turns(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Build the lightweight UI projection persisted beside full detail.

    Tool/process/commentary bodies deliberately stay in ``events`` for an
    explicit detail request.  The initial projection contains only the prompt,
    final answer, terminal state and a count advertising expandable detail.
    """
    turns: list[dict[str, Any]] = []
    for group in group_history_events(events):
        turn_id = _turn_id(group)
        if turn_id is None:
            continue
        prompt = ""
        prompt_truncated = False
        images = None
        deferred_image_count = 0
        files = None
        started_ms = None
        done_ms = None
        duration_ms = None
        fork_point = None
        checkpoint_id = None
        done = False
        interrupted = False
        error = None
        channels: dict[str, str] = {}
        texts: dict[str, list[str]] = {}
        text_order: list[str] = []
        detail_items: set[str] = set()
        for event in group:
            event_type = event.get("type")
            if started_ms is None:
                started_ms = _event_ms(event.get("ts"))
            if event_type == "user_msg":
                if isinstance(event.get("prompt"), str):
                    prompt = event["prompt"]
                    if len(prompt) > _SUMMARY_PROMPT_MAX_CHARS:
                        suffix = "\n\n…（完整问题请展开本轮过程）"
                        keep = _SUMMARY_PROMPT_MAX_CHARS - len(suffix)
                        prompt = prompt[:keep] + suffix
                        prompt_truncated = True
                raw_images = event.get("images")
                if isinstance(raw_images, list):
                    deferred_image_count = len(raw_images)
                files = event.get("files")
            elif event_type == "assistant_msg_start":
                message_id = event.get("message_id")
                if isinstance(message_id, str):
                    channels[message_id] = str(event.get("channel") or "unknown")
                    if message_id not in texts:
                        texts[message_id] = []
                        text_order.append(message_id)
            elif event_type == "delta":
                message_id = event.get("message_id")
                if isinstance(message_id, str) and isinstance(event.get("text"), str):
                    channels[message_id] = str(
                        event.get("channel") or channels.get(message_id) or "unknown")
                    if message_id not in texts:
                        texts[message_id] = []
                        text_order.append(message_id)
                    texts[message_id].append(event["text"])
            elif event_type == "turn_binding":
                if isinstance(event.get("turn_id"), str):
                    fork_point = event["turn_id"]
            elif event_type == "turn_end":
                done = True
                done_ms = _event_ms(event.get("ts"))
                if isinstance(event.get("turn_id"), str):
                    fork_point = event["turn_id"]
                if isinstance(event.get("checkpoint_id"), str):
                    checkpoint_id = event["checkpoint_id"]
                result = event.get("result")
                if isinstance(result, dict):
                    if isinstance(result.get("duration_ms"), int):
                        duration_ms = result["duration_ms"]
                    subtype = str(result.get("subtype") or "")
                    interrupted = subtype in {
                        "interrupted", "error_during_execution", "aborted",
                    }
                    if bool(result.get("is_error")) and not interrupted:
                        error = subtype or "error"
            elif event_type == "error":
                if isinstance(event.get("message"), str):
                    error = event["message"]
            if event_type in {"tool_use", "process", "turn_plan", "turn_diff"}:
                detail_id = event.get("tool_use_id") or event.get("item_id")
                if isinstance(detail_id, str):
                    detail_items.add(detail_id)

        final_ids = [
            message_id for message_id in text_order
            if channels.get(message_id) == "final"
        ]
        if not final_ids:
            final_ids = [
                message_id for message_id in text_order
                if channels.get(message_id) in {None, "unknown"}
            ]
        final_id_set = set(final_ids)
        for message_id in text_order:
            if message_id not in final_id_set and any(texts.get(message_id, ())):
                detail_items.add(message_id)
        blocks = []
        remaining_summary_chars = _SUMMARY_TEXT_MAX_CHARS
        summary_truncated = False
        for message_id in final_ids:
            text = "".join(texts.get(message_id, ()))
            if text:
                if remaining_summary_chars <= 0:
                    summary_truncated = True
                    continue
                if len(text) > remaining_summary_chars:
                    suffix = "\n\n…（完整内容请展开本轮过程）"
                    if remaining_summary_chars <= len(suffix):
                        text = suffix[:remaining_summary_chars]
                    else:
                        keep = remaining_summary_chars - len(suffix)
                        text = text[:keep] + suffix
                    summary_truncated = True
                remaining_summary_chars -= len(text)
                blocks.append({
                    "kind": "text",
                    "message_id": message_id,
                    "text": text,
                    "done": done,
                    "channel": "final",
                })
        turn: dict[str, Any] = {
            "id": turn_id,
            "prompt": prompt,
            "blocks": blocks,
            "done": done,
            "detailEventCount": (
                len(detail_items)
                + deferred_image_count
                + int(prompt_truncated)
                + int(summary_truncated)
            ),
            "detailLoaded": False,
        }
        optional = {
            "forkPointId": fork_point,
            "checkpointId": checkpoint_id,
            "images": images,
            "files": files,
            "ts": started_ms,
            "doneTs": done_ms,
            "durationMs": duration_ms,
            "error": error,
        }
        turn.update({key: value for key, value in optional.items() if value is not None})
        if interrupted:
            turn["interrupted"] = True
        turns.append(turn)
    return tuple(turns)


class HistoryIndexStore:
    """Small SQLite LRU for source-bound materialized conversation pages.

    Connections are intentionally short lived.  History parsing runs in worker
    threads, and opening a connection per store operation avoids sharing one
    sqlite handle across the wrapper event loop and those workers.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = Path(state_dir) / "history-index.sqlite3"
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current not in (0, _SCHEMA_VERSION):
                # This database is derived exclusively from engine transcripts.
                # Rebuilding is safer than carrying migrations for stale cached
                # projections across wire-shape changes.
                connection.execute("DROP TABLE IF EXISTS history_pages")
                connection.execute("DROP TABLE IF EXISTS history_turn_details")
                current = 0
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_pages (
                    session_id TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    source_token TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    before_cursor TEXT NOT NULL,
                    page_limit INTEGER NOT NULL,
                    payload_json BLOB NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY (
                        session_id, engine, source_token,
                        before_cursor, page_limit
                    )
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_pages_lru "
                "ON history_pages(accessed_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_turn_details (
                    session_id TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    source_token TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    payload_json BLOB NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY (session_id, engine, source_token, turn_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_turn_details_lookup "
                "ON history_turn_details(session_id, engine, source_path, "
                "turn_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_turn_details_lru "
                "ON history_turn_details(accessed_at)"
            )
            if current == 0:
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _cursor(before: str | None) -> str:
        return before or ""

    def get_page(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        *,
        before: str | None,
        limit: int,
    ) -> MaterializedHistoryPage | None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM history_pages
                WHERE session_id=? AND engine=? AND source_token=?
                  AND before_cursor=? AND page_limit=?
                """,
                (session_id, engine, source.token, self._cursor(before), int(limit)),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE history_pages SET accessed_at=?
                WHERE session_id=? AND engine=? AND source_token=?
                  AND before_cursor=? AND page_limit=?
                """,
                (now, session_id, engine, source.token,
                 self._cursor(before), int(limit)),
            )
        try:
            payload = json.loads(bytes(row["payload_json"]).decode("utf-8"))
            if not isinstance(payload, dict):
                return None
            return MaterializedHistoryPage.from_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            self.invalidate_session(session_id)
            return None

    def put_page(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        *,
        before: str | None,
        limit: int,
        page: MaterializedHistoryPage,
    ) -> bool:
        payload = json.dumps(
            page.as_payload(), ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        now = time.time()
        with self._connect() as connection:
            # Detail is indexed per turn as well as inside the full page.  This
            # keeps expansion O(one turn) and remains available after an append
            # changes the exact page fingerprint.  Destructive rewrites call
            # invalidate_session(), so an old turn can never cross rollback.
            for group in group_history_events(page.events):
                turn_id = _turn_id(group)
                if turn_id is None:
                    continue
                detail_payload = json.dumps(
                    group, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")
                if len(detail_payload) > self.max_bytes:
                    continue
                connection.execute(
                    """
                    INSERT INTO history_turn_details (
                        session_id, engine, source_token, source_path,
                        turn_id, payload_json, payload_bytes,
                        created_at, accessed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (session_id, engine, source_token, turn_id)
                    DO UPDATE SET
                        source_path=excluded.source_path,
                        payload_json=excluded.payload_json,
                        payload_bytes=excluded.payload_bytes,
                        created_at=excluded.created_at,
                        accessed_at=excluded.accessed_at
                    """,
                    (session_id, engine, source.token, source.path, turn_id,
                     detail_payload, len(detail_payload), now, now),
                )
            self._prune_details(connection)
            if len(payload) > self.max_bytes:
                return False
            connection.execute(
                """
                INSERT INTO history_pages (
                    session_id, engine, source_token, source_path,
                    before_cursor, page_limit, payload_json, payload_bytes,
                    created_at, accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    session_id, engine, source_token, before_cursor, page_limit
                ) DO UPDATE SET
                    source_path=excluded.source_path,
                    payload_json=excluded.payload_json,
                    payload_bytes=excluded.payload_bytes,
                    created_at=excluded.created_at,
                    accessed_at=excluded.accessed_at
                """,
                (session_id, engine, source.token, source.path,
                 self._cursor(before), int(limit), payload, len(payload), now, now),
            )
            # A changed source makes every older snapshot for this session
            # unreachable.  Delete it eagerly rather than waiting for global LRU.
            connection.execute(
                """
                DELETE FROM history_pages
                WHERE session_id=? AND engine=? AND source_token<>?
                """,
                (session_id, engine, source.token),
            )
            self._prune(connection)
        return True

    def get_turn_detail(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        turn_id: str,
    ) -> tuple[dict[str, Any], ...] | None:
        """Return one materialized turn without reading a complete page.

        Prefer the exact source snapshot.  If the transcript only appended
        after the page was painted, the newest row for the same source path is
        still a valid immutable completed turn.  Rollback explicitly removes
        every row for the session before its new revision is exposed.
        """
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_token, payload_json FROM history_turn_details
                WHERE session_id=? AND engine=? AND source_path=? AND turn_id=?
                ORDER BY (source_token = ?) DESC, created_at DESC
                LIMIT 1
                """,
                (session_id, engine, source.path, turn_id, source.token),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE history_turn_details SET accessed_at=?
                WHERE session_id=? AND engine=? AND source_token=? AND turn_id=?
                """,
                (now, session_id, engine, row["source_token"], turn_id),
            )
        try:
            payload = json.loads(bytes(row["payload_json"]).decode("utf-8"))
            if not isinstance(payload, list) or not all(
                    isinstance(event, dict) for event in payload):
                return None
            if _turn_id(payload) != turn_id:
                return None
            return tuple(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None

    def _prune(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(payload_bytes), 0) AS bytes "
            "FROM history_pages"
        ).fetchone()
        entries = int(row["entries"])
        total_bytes = int(row["bytes"])
        while entries > self.max_entries or total_bytes > self.max_bytes:
            victim = connection.execute(
                """
                SELECT session_id, engine, source_token, before_cursor,
                       page_limit, payload_bytes
                FROM history_pages ORDER BY accessed_at ASC LIMIT 1
                """
            ).fetchone()
            if victim is None:
                break
            connection.execute(
                """
                DELETE FROM history_pages
                WHERE session_id=? AND engine=? AND source_token=?
                  AND before_cursor=? AND page_limit=?
                """,
                tuple(victim[key] for key in (
                    "session_id", "engine", "source_token",
                    "before_cursor", "page_limit")),
            )
            entries -= 1
            total_bytes -= int(victim["payload_bytes"])

    def _prune_details(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(payload_bytes), 0) AS bytes "
            "FROM history_turn_details"
        ).fetchone()
        entries = int(row["entries"])
        total_bytes = int(row["bytes"])
        max_entries = self.max_entries * 4
        while entries > max_entries or total_bytes > self.max_bytes:
            victim = connection.execute(
                """
                SELECT session_id, engine, source_token, turn_id, payload_bytes
                FROM history_turn_details ORDER BY accessed_at ASC LIMIT 1
                """
            ).fetchone()
            if victim is None:
                break
            connection.execute(
                """
                DELETE FROM history_turn_details
                WHERE session_id=? AND engine=? AND source_token=? AND turn_id=?
                """,
                tuple(victim[key] for key in (
                    "session_id", "engine", "source_token", "turn_id")),
            )
            entries -= 1
            total_bytes -= int(victim["payload_bytes"])

    def invalidate_session(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM history_pages WHERE session_id=?", (session_id,))
            connection.execute(
                "DELETE FROM history_turn_details WHERE session_id=?",
                (session_id,),
            )

    def close(self) -> None:
        """Compatibility hook for WrapperMachine shutdown.

        The store uses operation-scoped connections, so there is no live handle
        to close.  Keeping an explicit hook makes future connection pooling safe.
        """
