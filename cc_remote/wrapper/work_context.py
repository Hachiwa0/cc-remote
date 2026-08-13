"""Work-only context growth layered over authoritative engine totals."""
from __future__ import annotations

import json
import os
from typing import Any

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER
from cc_remote.wrapper.codex_sessions import codex_rollout_path
from cc_remote.wrapper.stream import _bounded_jsonl_lines, transcript_path


_BASELINE_HISTORY_RECORD_LIMIT = 256
_CONTEXT_TAIL_SCAN_BYTES = 4 * 1024 * 1024
_CONTEXT_RECORD_MAX_BYTES = 1024 * 1024


def _nonnegative_int(value: object) -> int | None:
    if (isinstance(value, bool) or not isinstance(value, int) or value < 0
            or value > MAX_SAFE_WIRE_INTEGER):
        return None
    return value


def recover_codex_context_usage(
    session_id: str,
    *,
    codex_home: str | None = None,
) -> dict[str, Any] | None:
    """Recover the newest persisted Codex context sample from a bounded tail.

    Lightweight ``thread/resume`` does not replay historical tokenUsage
    notifications.  Resolve the rollout inside the selected account namespace
    and inspect only its tail, so even multi-gigabyte sessions remain cheap.
    """
    path = (
        codex_rollout_path(session_id)
        if codex_home is None
        else codex_rollout_path(session_id, codex_home=codex_home)
    )
    if not path:
        return None
    try:
        with open(path, "rb") as history:
            before = os.fstat(history.fileno())
            size = before.st_size
            start = max(0, size - _CONTEXT_TAIL_SCAN_BYTES)
            # Inspect the byte immediately before the bounded window. Without
            # it, a window which happens to start exactly after ``\n`` is
            # indistinguishable from one starting halfway through a record and
            # we would discard a complete first line.
            read_start = max(0, start - 1)
            history.seek(read_start)
            data = history.read(size - read_start)
            after = os.fstat(history.fileno())
        current = os.stat(path)
    except OSError:
        return None

    # A rollout can append while this bounded read is in flight. That makes the
    # captured sample merely older, not corrupt, because we read exactly the
    # pre-open snapshot length. Replacement or truncation is different: bytes
    # may now belong to another source/offset, so fail closed and retry after
    # the next process generation instead of painting a fabricated context.
    if (before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or after.st_size < size
            or current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
            or current.st_size < size):
        return None

    # A partial first record is never trustworthy. The final record may be
    # complete without a trailing newline, which is normal for a closed file.
    starts_at_record_boundary = start == 0
    if start > 0:
        starts_at_record_boundary = data[:1] == b"\n"
        data = data[1:]
    lines = data.splitlines()
    if not starts_at_record_boundary and lines:
        lines = lines[1:]
    for raw in reversed(lines):
        if not raw or len(raw) > _CONTEXT_RECORD_MAX_BYTES:
            continue
        try:
            record = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if (record.get("type") != "event_msg"
                or not isinstance(payload, dict)
                or payload.get("type") != "token_count"):
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        source = info.get("last_token_usage")
        if not isinstance(source, dict):
            source = info.get("last")
        if not isinstance(source, dict):
            continue
        total = _nonnegative_int(source.get("total_tokens"))
        if total is None:
            total = _nonnegative_int(source.get("totalTokens"))
        window = _nonnegative_int(info.get("model_context_window"))
        if window is None:
            window = _nonnegative_int(info.get("modelContextWindow"))
        if total is None or window is None or window <= 0:
            continue
        last: dict[str, int] = {"totalTokens": total}
        for snake, camel in (
            ("input_tokens", "inputTokens"),
            ("cached_input_tokens", "cachedInputTokens"),
            ("output_tokens", "outputTokens"),
            ("reasoning_output_tokens", "reasoningOutputTokens"),
        ):
            value = _nonnegative_int(source.get(snake))
            if value is None:
                value = _nonnegative_int(source.get(camel))
            if value is not None:
                last[camel] = value
        return {
            "last": last,
            "modelContextWindow": window,
        }
    return None


def initial_work_context_baseline(engine: str, usage: dict[str, Any]) -> int:
    """Return the fresh Work session's startup zero point.

    Claude is normally sampled before its first query by ``SdkHandle.connect``.
    The fallback is still useful for migrated sessions. Codex app-server emits
    token usage only after a turn, so its first input depth is the closest
    authoritative startup measurement; output stays user context.
    """
    if engine == "codex":
        raw = usage.get("raw") if isinstance(usage.get("raw"), dict) else {}
        last = raw.get("last") if isinstance(raw.get("last"), dict) else {}
        value = _nonnegative_int(last.get("inputTokens"))
        if value is not None:
            return value
        value = _nonnegative_int(usage.get("used_tokens"))
        return value or 0
    value = _nonnegative_int(usage.get("totalTokens"))
    return value or 0


def recover_work_context_baseline(
    engine: str,
    session_id: str,
    *,
    codex_home: str | None = None,
) -> int | None:
    """Recover a migrated Work session's first authoritative input depth.

    Both native histories record input usage after the first turn. That is the
    same startup zero point used for new Codex Work sessions and avoids treating
    an old conversation's *current* depth as engine overhead after an upgrade.
    """
    if engine == "codex":
        path = (
            codex_rollout_path(session_id)
            if codex_home is None
            else codex_rollout_path(session_id, codex_home=codex_home)
        )
    else:
        path = transcript_path(session_id)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as history:
            for index, line in enumerate(_bounded_jsonl_lines(history)):
                if index >= _BASELINE_HISTORY_RECORD_LIMIT:
                    break
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if engine == "codex":
                    payload = (record.get("payload")
                               if record.get("type") == "event_msg" else None)
                    if (not isinstance(payload, dict)
                            or payload.get("type") != "token_count"):
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    last = info.get("last_token_usage")
                    if not isinstance(last, dict):
                        last = info.get("last")
                    value = (_nonnegative_int(last.get("input_tokens"))
                             if isinstance(last, dict) else None)
                    if value is None and isinstance(last, dict):
                        value = _nonnegative_int(last.get("inputTokens"))
                    if value is not None and value > 0:
                        return value
                    continue

                message = (record.get("message")
                           if record.get("type") == "assistant" else None)
                usage = message.get("usage") if isinstance(message, dict) else None
                if not isinstance(usage, dict):
                    continue
                total = sum(
                    _nonnegative_int(usage.get(key)) or 0
                    for key in (
                        "input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
                if total > 0:
                    return total
    except (OSError, UnicodeError):
        return None
    return None


def work_context_metrics(
    engine: str,
    usage: dict[str, Any],
    baseline_tokens: int | None,
) -> tuple[int, int, float, int]:
    """Split Work's raw total into startup baseline and later growth.

    The returned tuple is ``session, fixed, session_percentage, baseline``.
    Raw totals are deliberately not changed: callers still use them for the
    actual remaining context capacity and compaction threshold.
    """
    raw_key = "used_tokens" if engine == "codex" else "totalTokens"
    max_key = "context_window" if engine == "codex" else "maxTokens"
    raw_total = _nonnegative_int(usage.get(raw_key)) or 0
    max_tokens = _nonnegative_int(usage.get(max_key)) or 0
    baseline = _nonnegative_int(baseline_tokens)
    if baseline is None:
        baseline = initial_work_context_baseline(engine, usage)
    fixed = min(raw_total, baseline)
    session = max(0, raw_total - fixed)
    percentage = session / max_tokens * 100.0 if max_tokens else 0.0
    return session, fixed, percentage, baseline
