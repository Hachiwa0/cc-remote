"""Wire protocol between client <-> relay <-> wrapper.

One JSON object per WebSocket text frame. Common envelope fields on every
message: `v` (protocol version), `type`, `ts`, optional `sid` (cc session id
once known), optional `seq` (monotonic per-session int assigned by the wrapper
to every downstream event; absent on command messages). Auth is NOT in the
envelope — it's a Bearer header at WS upgrade and is never logged.

Discriminated by `type`. `extra="forbid"` so unknown fields fail fast.
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1

State = Literal["idle", "running", "interrupting", "draining"]

# Error codes
ERR_BUSY = "busy"
ERR_NOT_RUNNING = "not_running"
ERR_DRAIN_TIMEOUT = "drain_timeout"
ERR_CC_CRASH = "cc_crash"
ERR_BAD_PROMPT = "bad_prompt"
ERR_PROTOCOL = "protocol"
ERR_INTERNAL = "internal"
ERR_WRAPPER_OFFLINE = "wrapper_offline"
ERR_WRAPPER_ALREADY_CONNECTED = "wrapper_already_connected"
ERR_AUTH = "auth"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: int = PROTOCOL_VERSION
    ts: float = Field(default_factory=time.time)
    sid: Optional[str] = None
    seq: Optional[int] = None
    # `to` routes a frame to a single client (by client_id) instead of
    # broadcasting. Used for per-client replay frames; null = broadcast.
    to: Optional[str] = None


# ---- client -> wrapper (via relay); no seq ----

class Hello(_Base):
    """First frame after upgrade. `role` distinguishes client vs wrapper.

    Client role sends `client_id` (for routed replay) and `last_seq` (request
    replay from last_seq+1; null = fresh catch-up / snapshot only). Wrapper role
    announces its cc session id, current state, and ring-buffer bounds.
    """
    type: Literal["hello"] = "hello"
    role: Literal["client", "wrapper"]
    client_id: Optional[str] = None  # client
    last_seq: Optional[int] = None  # client
    cc_session_id: Optional[str] = None  # wrapper
    state: Optional[State] = None  # wrapper
    buffer_head_seq: Optional[int] = None  # wrapper
    buffer_tail_seq: Optional[int] = None  # wrapper


class Query(_Base):
    type: Literal["query"] = "query"
    prompt: str
    msg_id: str


class Interrupt(_Base):
    type: Literal["interrupt"] = "interrupt"


class Ping(_Base):
    type: Literal["ping"] = "ping"
    n: int


class Pong(_Base):
    type: Literal["pong"] = "pong"
    n: int


# ---- wrapper -> client (via relay); all carry seq ----

class ReplayStart(_Base):
    type: Literal["replay_start"] = "replay_start"
    from_seq: int
    to_seq: int
    truncated: bool


class ReplayEnd(_Base):
    type: Literal["replay_end"] = "replay_end"
    to_seq: int
    truncated: bool


class Snapshot(_Base):
    type: Literal["snapshot"] = "snapshot"
    cc_session_id: Optional[str] = None
    state: State
    tail_text: str = ""


class StateEvent(_Base):
    type: Literal["state"] = "state"
    state: State


class UserMsg(_Base):
    """A user's query, broadcast to all clients so every device sees the full
    conversation (prompt + response). The originating client dedups by msg_id
    (it already created the turn optimistically on send)."""
    type: Literal["user_msg"] = "user_msg"
    msg_id: str
    prompt: str


class AssistantMsgStart(_Base):
    type: Literal["assistant_msg_start"] = "assistant_msg_start"
    message_id: str


class Delta(_Base):
    type: Literal["delta"] = "delta"
    message_id: str
    text: str


class ToolUse(_Base):
    type: Literal["tool_use"] = "tool_use"
    message_id: str
    tool_use_id: str
    tool: str
    input: dict[str, Any]


class ToolResult(_Base):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool
    truncated: Optional[bool] = None


class AssistantMsgEnd(_Base):
    type: Literal["assistant_msg_end"] = "assistant_msg_end"
    message_id: str


class TurnResult(BaseModel):
    """Subset of ResultMessage forwarded to clients."""
    model_config = ConfigDict(extra="allow")
    subtype: str
    duration_ms: int
    is_error: bool
    total_cost_usd: Optional[float] = None
    num_turns: Optional[int] = None


class TurnEnd(_Base):
    type: Literal["turn_end"] = "turn_end"
    result: TurnResult


class Error(_Base):
    type: Literal["error"] = "error"
    code: str
    message: str


# ---- relay -> client (control); no seq ----

class WrapperDisconnected(_Base):
    type: Literal["wrapper_disconnected"] = "wrapper_disconnected"


class WrapperReconnected(_Base):
    type: Literal["wrapper_reconnected"] = "wrapper_reconnected"
    cc_session_id: Optional[str] = None
    state: State


AnyMessage = Union[
    Hello, Query, Interrupt, Ping, Pong,
    ReplayStart, ReplayEnd, Snapshot, StateEvent,
    UserMsg, AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    TurnEnd, Error, WrapperDisconnected, WrapperReconnected,
]

# Session-narrative events the wrapper seqs and buffers. Replay/snapshot/
# control frames (replay_start, replay_end, snapshot, wrapper_disconnected,
# wrapper_reconnected) are synthesized per-reconnect and are NOT seq'd/buffered.
DOWNSTREAM_TYPES = frozenset({
    "user_msg", "state", "assistant_msg_start", "delta", "tool_use",
    "tool_result", "assistant_msg_end", "turn_end", "error",
})

_TYPE_MAP: dict[str, type[BaseModel]] = {
    "hello": Hello,
    "query": Query,
    "interrupt": Interrupt,
    "ping": Ping,
    "pong": Pong,
    "replay_start": ReplayStart,
    "replay_end": ReplayEnd,
    "snapshot": Snapshot,
    "state": StateEvent,
    "user_msg": UserMsg,
    "assistant_msg_start": AssistantMsgStart,
    "delta": Delta,
    "tool_use": ToolUse,
    "tool_result": ToolResult,
    "assistant_msg_end": AssistantMsgEnd,
    "turn_end": TurnEnd,
    "error": Error,
    "wrapper_disconnected": WrapperDisconnected,
    "wrapper_reconnected": WrapperReconnected,
}


class ProtocolError(Exception):
    pass


def deserialize(raw: str | bytes) -> AnyMessage:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ProtocolError("payload must be a JSON object")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version mismatch: got {data.get('v')!r}, want {PROTOCOL_VERSION}")
    t = data.get("type")
    cls = _TYPE_MAP.get(t)
    if cls is None:
        raise ProtocolError(f"unknown message type: {t!r}")
    try:
        return cls.model_validate(data)  # type: ignore[return-value]
    except Exception as e:
        raise ProtocolError(f"invalid {t} message: {e}") from e


def serialize(msg: BaseModel) -> str:
    return msg.model_dump_json()


def is_downstream(msg: BaseModel) -> bool:
    return msg.type in DOWNSTREAM_TYPES
