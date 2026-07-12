"""Translate ClaudeSDKClient messages into wire-protocol events.

Stateful per turn: tracks the current assistant message_id so streamed
content_block_delta text attaches to the right block; the assembled
AssistantMessage finalizes it. tool_use is emitted ONCE from the assembled
AssistantMessage (full input), never as JSON-fragment deltas — text deltas
still stream live via StreamEvent.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import uuid
from datetime import datetime

from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, UserMessage, SystemMessage,
    StreamEvent, ToolUseBlock, ToolResultBlock,
)

from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    TurnEnd, TurnResult, UserMsg,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_CLAUDE_MESSAGE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_TRANSCRIPT_MATCHES = 1000
_MAX_TRANSCRIPT_RECORD_CHARS = 16 * 1024 * 1024
_MAX_TIMESTAMP_ENTRIES = 200_000


class StreamTranslator:
    def __init__(self, tool_result_max: int):
        self.tool_result_max = tool_result_max
        self._cur_msg_id: str | None = None
        # Claude can emit several AssistantMessage records in one user turn
        # (thinking/text/tool_use, then more assistant records after tool results).
        # fork_session(up_to_message_id=...) accepts the transcript UUID, not the
        # API message_id used to assemble streamed blocks, so retain the last one
        # until the terminal ResultMessage arrives.
        self._last_assistant_uuid: str | None = None

    def feed(self, msg) -> list:
        events: list = []
        if isinstance(msg, StreamEvent):
            ev = msg.event or {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        if self._cur_msg_id is None:
                            self._cur_msg_id = uuid.uuid4().hex
                            events.append(AssistantMsgStart(message_id=self._cur_msg_id))
                        events.append(Delta(message_id=self._cur_msg_id, text=text))
        elif isinstance(msg, AssistantMessage):
            if (isinstance(msg.uuid, str)
                    and _CLAUDE_MESSAGE_UUID.fullmatch(msg.uuid)):
                self._last_assistant_uuid = msg.uuid
            if self._cur_msg_id is None:
                # tool-only message with no text deltas — start a block now
                self._cur_msg_id = msg.message_id or uuid.uuid4().hex
                events.append(AssistantMsgStart(message_id=self._cur_msg_id))
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    events.append(ToolUse(
                        message_id=self._cur_msg_id,
                        tool_use_id=block.id, tool=block.name,
                        input=bounded_tool_input(block.input, self.tool_result_max),
                    ))
            events.append(AssistantMsgEnd(message_id=self._cur_msg_id))
            self._cur_msg_id = None
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    text, was_truncated = bounded_text(
                        block.content, self.tool_result_max)
                    truncated = True if was_truncated else None
                    events.append(ToolResult(
                        tool_use_id=block.tool_use_id,
                        content=text,
                        is_error=bool(block.is_error),
                        truncated=truncated,
                    ))
        elif isinstance(msg, ResultMessage):
            events.append(TurnEnd(result=TurnResult(
                subtype=msg.subtype,
                duration_ms=msg.duration_ms,
                is_error=msg.is_error,
                total_cost_usd=msg.total_cost_usd,
                num_turns=msg.num_turns,
            ), turn_id=self._last_assistant_uuid))
            self._last_assistant_uuid = None
        # SystemMessage (init, etc.) and RateLimitEvent: ignored for MVP
        return events


def _cc_img_block(b: dict) -> dict | None:
    """A cc transcript image block {type:image, source:{type:base64, media_type,
    data}} -> {media_type, data} (the web's QueryImg shape). None if not base64."""
    src = b.get("source")
    if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
        return {"media_type": src.get("media_type") or "image/png", "data": src["data"]}
    return None


def extract_session_id(msg) -> str | None:
    """Pull the cc session id out of any SDK message that carries it."""
    if isinstance(msg, ResultMessage):
        return msg.session_id
    if isinstance(msg, SystemMessage):
        data = msg.data
        if isinstance(data, dict):
            return data.get("session_id")
    return None


def extract_model(msg) -> str | None:
    """Pull the current model out of the init SystemMessage."""
    if isinstance(msg, SystemMessage) and msg.subtype == "init":
        data = msg.data
        if isinstance(data, dict):
            return data.get("model")
    return None


# ---- on-disk history -> wire events (for session switch) ----

def transcript_path(session_id: str) -> str | None:
    """Absolute path of a cc session's transcript .jsonl, or None. session_id is
    globally unique, so a glob across all project dirs finds it regardless of cwd.

    Used by the transcript watcher to spot writes made by an EXTERNAL process (a
    native `claude` in the user's terminal). Watch st_size, NOT st_mtime: merely
    spawning `claude --resume <id>` touches mtime without changing a byte, so mtime
    would false-positive on every session the wrapper opens."""
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    try:
        safe_id = glob.escape(session_id)
        root = os.path.realpath(os.path.expanduser("~/.claude/projects"))
        matches = glob.iglob(os.path.join(root, "*", f"{safe_id}.jsonl"))
        for index, match in enumerate(matches):
            if index >= _MAX_TRANSCRIPT_MATCHES:
                break
            resolved = os.path.realpath(match)
            if os.path.commonpath((root, resolved)) == root:
                return resolved
        return None
    except Exception:
        return None


def _bounded_jsonl_lines(file):
    """Yield complete records while skipping a single pathological long line."""
    while True:
        line = file.readline(_MAX_TRANSCRIPT_RECORD_CHARS + 1)
        if not line:
            return
        complete = line.endswith("\n") or len(line) < _MAX_TRANSCRIPT_RECORD_CHARS + 1
        if complete:
            yield line
            continue
        while line and not line.endswith("\n"):
            line = file.readline(_MAX_TRANSCRIPT_RECORD_CHARS + 1)


def transcript_timestamps(session_id: str) -> dict[str, float]:
    """Map each transcript entry's uuid -> epoch seconds, read straight from the
    .jsonl. The SDK's SessionMessage drops the per-message timestamp, so without
    this, history events default their `ts` to now (making every past message show
    the current time — "like a clock"). Best-effort: {} if not found/readable.
    session_id is globally unique, so a glob across all project dirs locates it."""
    out: dict[str, float] = {}
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return out
    try:
        path = transcript_path(session_id)
        if not path:
            return out
        with open(path) as f:
            for line in _bounded_jsonl_lines(f):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                uid, ts = d.get("uuid"), d.get("timestamp")
                if not uid or not isinstance(ts, str):
                    continue
                try:
                    out[uid] = datetime.fromisoformat(
                        ts.replace("Z", "+00:00") if ts.endswith("Z") else ts).timestamp()
                    if len(out) >= _MAX_TIMESTAMP_ENTRIES:
                        break
                except Exception:
                    continue
    except Exception:
        pass
    return out


def translate_history(messages, tool_result_max: int, timestamps: dict | None = None) -> list:
    """Translate a session's on-disk transcript (list[SessionMessage]) into wire
    events the client reducer renders as past turns.

    The transcript carries no ResultMessage, so synthetic TurnEnd frames delimit
    turns. `timestamps` (uuid -> epoch seconds, from transcript_timestamps) stamps
    each UserMsg with its real ask-time and each TurnEnd with the turn's last
    message time (answer-done time) — otherwise history shows "now". Thinking
    blocks and non-conversational user turns (compact summaries, slash-command
    envelopes, local-command stdout) are skipped so the history reads like a chat.
    """
    events: list = []
    turn_open = False
    last_ts = None  # transcript ts of the most-recent message in the open turn
    last_assistant_uuid = None

    def _history_id(value, kind: str, position: str) -> str:
        """Keep valid engine ids; deterministically repair malformed legacy rows.

        Old hand-edited/corrupt transcripts can omit a message/tool id.  WireId is
        intentionally strict, but one bad block must not make the entire otherwise
        readable conversation disappear.  Transcript positions are append-stable,
        so the fallback also remains a valid pagination/dedup key across reparses.
        """
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
            return value
        raw = value[:1024] if isinstance(value, str) else type(value).__name__
        digest = hashlib.sha256(
            f"{kind}\0{position}\0{raw}".encode("utf-8", "surrogatepass")
        ).hexdigest()[:24]
        return f"hist-{kind}-{digest}"

    def _ts(uid):
        return timestamps.get(uid) if timestamps else None

    def _um(uid, prompt):
        um = UserMsg(msg_id=uid, prompt=prompt)
        t = _ts(uid)
        if t is not None:
            um.ts = t   # question time, not load time
        return um

    def close_turn():
        nonlocal turn_open, last_assistant_uuid
        if turn_open:
            te = TurnEnd(
                result=TurnResult(
                    subtype="success", duration_ms=0, is_error=False),
                turn_id=last_assistant_uuid,
            )
            if last_ts is not None:
                te.ts = last_ts   # answer-done time = last message of the turn
            events.append(te)
            turn_open = False
            last_assistant_uuid = None

    for message_index, m in enumerate(messages):
        msg = m.message
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or m.type
        content = msg.get("content")
        source_uid = m.uuid if isinstance(m.uuid, str) else ""
        message_uid = _history_id(source_uid, "msg", str(message_index))

        if role == "user":
            if isinstance(content, str):
                if _is_meta_user_text(content):
                    continue
                close_turn()
                events.append(_um(message_uid, content))
                turn_open = True
            elif isinstance(content, list):
                # collect any uploaded images up front so they attach to this turn's
                # UserMsg (replay on reload — the transcript stores the base64).
                imgs = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "image":
                        img = _cc_img_block(b)
                        if img:
                            imgs.append(img)
                made = False
                for block_index, b in enumerate(content):
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "tool_result":
                        text, was_truncated = bounded_text(
                            b.get("content"), tool_result_max)
                        truncated = True if was_truncated else None
                        events.append(ToolResult(
                            tool_use_id=_history_id(
                                b.get("tool_use_id"), "tool",
                                f"{message_index}-{block_index}-result"),
                            content=text,
                            is_error=bool(b.get("is_error")),
                            truncated=truncated,
                        ))
                    elif bt == "text":
                        txt = b.get("text", "")
                        if txt and not _is_meta_user_text(txt):
                            close_turn()
                            um = _um(message_uid, txt)
                            if imgs and not made:
                                um.images = imgs
                                made = True
                            events.append(um)
                            turn_open = True
                if imgs and not made:   # image-only user turn
                    close_turn()
                    um = _um(message_uid, "")
                    um.images = imgs
                    events.append(um)
                    turn_open = True
        elif role == "assistant":
            if not isinstance(content, list):
                continue
            if _CLAUDE_MESSAGE_UUID.fullmatch(source_uid):
                last_assistant_uuid = source_uid
            mid = message_uid
            started = False
            for block_index, b in enumerate(content):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    txt = b.get("text", "")
                    if not started:
                        events.append(AssistantMsgStart(message_id=mid))
                        started = True
                    if txt:
                        events.append(Delta(message_id=mid, text=txt))
                elif bt == "tool_use":
                    if not started:
                        events.append(AssistantMsgStart(message_id=mid))
                        started = True
                    # a stored tool_use input SHOULD be a dict, but old/odd history
                    # can carry a scalar (e.g. 3); coerce so ToolUse validation
                    # doesn't crash the whole history load (and thus the resume).
                    _inp = b.get("input")
                    events.append(ToolUse(
                        message_id=mid,
                        tool_use_id=_history_id(
                            b.get("id"), "tool",
                            f"{message_index}-{block_index}-use"),
                        tool=b.get("name") or "",
                        input=bounded_tool_input(
                            _inp if isinstance(_inp, dict)
                            else ({} if _inp is None else {"value": _inp}),
                            tool_result_max,
                        ),
                    ))
                # thinking / unknown blocks: skipped (MVP)
            if started:
                events.append(AssistantMsgEnd(message_id=mid))
                turn_open = True
        # advance last_ts AFTER handling m: a leading close_turn (for the next user
        # msg) stamps the PRIOR turn's tail; the final close_turn stamps this turn's
        # last (assistant) message = answer-done time.
        mts = _ts(source_uid)
        if mts is not None:
            last_ts = mts
    close_turn()
    return events


def _is_meta_user_text(text: str) -> bool:
    """Skip non-conversational user turns that would just clutter the history:
    compact summaries, slash-command envelopes, and local-command stdout/stderr."""
    t = text.lstrip()
    return (
        t.startswith("This session is being continued from a previous conversation")
        or t.startswith("<command-name>")
        or t.startswith("<command-message>")
        or t.startswith("<command-args>")
        or t.startswith("<local-command-stdout>")
        or t.startswith("<local-command-stderr>")
    )


def last_assistant_model(messages) -> str | None:
    """Most recent assistant message's model id, for restoring the model readout
    when loading a switched session's history."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "assistant" and isinstance(m.message, dict):
            mdl = m.message.get("model")
            if mdl:
                return mdl
    return None
