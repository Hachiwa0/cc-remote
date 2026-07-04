"""Translate ClaudeSDKClient messages into wire-protocol events.

Stateful per turn: tracks the current assistant message_id so streamed
content_block_delta text attaches to the right block; the assembled
AssistantMessage finalizes it. tool_use is emitted ONCE from the assembled
AssistantMessage (full input), never as JSON-fragment deltas — text deltas
still stream live via StreamEvent.
"""
from __future__ import annotations

import uuid
from typing import Any

from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, UserMessage, SystemMessage,
    StreamEvent, RateLimitEvent, ToolUseBlock, ToolResultBlock,
)

from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    TurnEnd, TurnResult,
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
