"""Translate ClaudeSDKClient messages into wire-protocol events.

Stateful per turn: tracks the current assistant message_id so streamed
content_block_delta text attaches to the right block; the assembled
AssistantMessage finalizes it. tool_use is emitted ONCE from the assembled
AssistantMessage (full input), never as JSON-fragment deltas — text deltas
still stream live via StreamEvent.
"""
from __future__ import annotations

import glob
import json
import os
import uuid
from datetime import datetime
from typing import Any

from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, UserMessage, SystemMessage,
    StreamEvent, ToolUseBlock, ToolResultBlock,
)

from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    TurnEnd, TurnResult, UserMsg,
)


class StreamTranslator:
    def __init__(self, tool_result_max: int):
        self.tool_result_max = tool_result_max
        self._cur_msg_id: str | None = None

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
            if self._cur_msg_id is None:
                # tool-only message with no text deltas — start a block now
                self._cur_msg_id = msg.message_id or uuid.uuid4().hex
                events.append(AssistantMsgStart(message_id=self._cur_msg_id))
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    events.append(ToolUse(
                        message_id=self._cur_msg_id,
                        tool_use_id=block.id, tool=block.name, input=block.input,
                    ))
            events.append(AssistantMsgEnd(message_id=self._cur_msg_id))
            self._cur_msg_id = None
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    text = _stringify(block.content)
                    truncated = None
                    if len(text) > self.tool_result_max:
                        text = text[:self.tool_result_max]
                        truncated = True
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
            )))
        # SystemMessage (init, etc.) and RateLimitEvent: ignored for MVP
        return events


def _stringify(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or str(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


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

def transcript_timestamps(session_id: str) -> dict[str, float]:
    """Map each transcript entry's uuid -> epoch seconds, read straight from the
    .jsonl. The SDK's SessionMessage drops the per-message timestamp, so without
    this, history events default their `ts` to now (making every past message show
    the current time — "like a clock"). Best-effort: {} if not found/readable.
    session_id is globally unique, so a glob across all project dirs locates it."""
    out: dict[str, float] = {}
    try:
        matches = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
        if not matches:
            return out
        with open(matches[0]) as f:
            for line in f:
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

    def _ts(uid):
        return timestamps.get(uid) if timestamps else None

    def _um(uid, prompt):
        um = UserMsg(msg_id=uid, prompt=prompt)
        t = _ts(uid)
        if t is not None:
            um.ts = t   # question time, not load time
        return um

    def close_turn():
        nonlocal turn_open
        if turn_open:
            te = TurnEnd(result=TurnResult(subtype="success", duration_ms=0, is_error=False))
            if last_ts is not None:
                te.ts = last_ts   # answer-done time = last message of the turn
            events.append(te)
            turn_open = False

    for m in messages:
        msg = m.message
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or m.type
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                if _is_meta_user_text(content):
                    continue
                close_turn()
                events.append(_um(m.uuid, content))
                turn_open = True
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "tool_result":
                        text = _stringify(b.get("content"))
                        truncated = None
                        if len(text) > tool_result_max:
                            text = text[:tool_result_max]
                            truncated = True
                        events.append(ToolResult(
                            tool_use_id=b.get("tool_use_id") or "",
                            content=text,
                            is_error=bool(b.get("is_error")),
                            truncated=truncated,
                        ))
                    elif bt == "text":
                        txt = b.get("text", "")
                        if txt and not _is_meta_user_text(txt):
                            close_turn()
                            events.append(_um(m.uuid, txt))
                            turn_open = True
        elif role == "assistant":
            if not isinstance(content, list):
                continue
            mid = m.uuid
            started = False
            for b in content:
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
                        tool_use_id=b.get("id") or "",
                        tool=b.get("name") or "",
                        input=_inp if isinstance(_inp, dict) else ({} if _inp is None else {"value": _inp}),
                    ))
                # thinking / unknown blocks: skipped (MVP)
            if started:
                events.append(AssistantMsgEnd(message_id=mid))
                turn_open = True
        # advance last_ts AFTER handling m: a leading close_turn (for the next user
        # msg) stamps the PRIOR turn's tail; the final close_turn stamps this turn's
        # last (assistant) message = answer-done time.
        mts = _ts(m.uuid)
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
