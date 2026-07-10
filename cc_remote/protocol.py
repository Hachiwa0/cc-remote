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

PROTOCOL_VERSION = 3

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

    Client role sends `client_id` (for routed frames). `last_seq`/`cursors` are
    LEGACY — hello no longer replays history; it only prompts the wrapper to send
    a lightweight Snapshot per resident session (state dot). History is fetched
    on demand via GetHistory (one bulk frame read from the transcript), like a
    web chat's GET /conversation. Wrapper role announces its cc session id,
    current state, and ring-buffer bounds.
    """
    type: Literal["hello"] = "hello"
    role: Literal["client", "wrapper"]
    client_id: Optional[str] = None  # client
    last_seq: Optional[int] = None  # client (legacy: focused session only)
    cursors: Optional[dict[str, int]] = None  # client: per-session last_seq for multi-session catch-up
    cc_session_id: Optional[str] = None  # wrapper
    state: Optional[State] = None  # wrapper
    buffer_head_seq: Optional[int] = None  # wrapper
    buffer_tail_seq: Optional[int] = None  # wrapper


class Query(_Base):
    type: Literal["query"] = "query"
    prompt: str
    msg_id: str
    images: Optional[list[dict[str, str]]] = None  # [{media_type, data(base64)}] — multimodal image blocks
    files: Optional[list[dict[str, str]]] = None   # [{filename, data(base64)}] — written to /tmp, prompt gets @path


class Interrupt(_Base):
    type: Literal["interrupt"] = "interrupt"


class SetModel(_Base):
    type: Literal["set_model"] = "set_model"
    model: str


class SetEffort(_Base):
    """client -> wrapper: set the session's reasoning effort (thinking strength).
    Unlike set_model, effort is a spawn-time CLI flag (--effort), so applying it
    respawns the cc subprocess with resume — done lazily at the next turn."""
    type: Literal["set_effort"] = "set_effort"
    effort: str  # low | medium | high | xhigh | max


class SetServiceTier(_Base):
    """client -> wrapper: set the Codex service tier (codex only). "fast" maps to
    codex's Fast mode via turn/start's `serviceTier` param; "" / "default" turns it
    off. Applied on the NEXT turn (a per-turn turn/start override, like model)."""
    type: Literal["set_service_tier"] = "set_service_tier"
    service_tier: str  # fast | default (or "" = off)


class OpenBtw(_Base):
    """client -> wrapper: open a /btw ephemeral side-fork of `sid` (the parent
    session). The wrapper forks it (inherits context) and replies BtwOpened
    routed to=<client_id> (only the requester opens the panel)."""
    type: Literal["open_btw"] = "open_btw"
    client_id: Optional[str] = None  # requester, for routing BtwOpened back
    # `sid` (inherited) = the parent session to fork.


class CloseBtw(_Base):
    """client -> wrapper: discard the /btw fork `sid` (tear down, never persisted)."""
    type: Literal["close_btw"] = "close_btw"
    # `sid` (inherited) = the btw fork to close.


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
    # rebuild=True: the client's last_seq was from a previous wrapper lifetime
    # (seq reset on restart), so it must discard its IndexedDB cache and rebuild
    # from this full-buffer replay. Distinct from `truncated` (which means the
    # buffer evicted events the client wanted -> data may be lost -> show banner).
    rebuild: bool = False


class ReplayEnd(_Base):
    type: Literal["replay_end"] = "replay_end"
    to_seq: int
    truncated: bool


class Snapshot(_Base):
    type: Literal["snapshot"] = "snapshot"
    cc_session_id: Optional[str] = None
    state: State
    tail_text: str = ""
    cwd: Optional[str] = None  # active cc cwd, so the client knows the current project


class StateEvent(_Base):
    type: Literal["state"] = "state"
    state: State


class Model(_Base):
    """The cc session's current model (from SystemMessage init / after set_model).
    Downstream so a reconnecting client restores the model readout."""
    type: Literal["model"] = "model"
    model: str


class Effort(_Base):
    """The session's current reasoning effort. Downstream so a reconnecting
    client restores the effort readout."""
    type: Literal["effort"] = "effort"
    effort: str


class Fast(_Base):
    """Codex Fast-mode (service_tier) state, downstream. Emitted after a /fast
    toggle and on each codex turn so the client shows whether the next reply is on
    the fast tier or standard — not just that it was 'toggled'."""
    type: Literal["fast"] = "fast"
    on: bool


class BtwOpened(_Base):
    """wrapper -> client: a /btw fork is ready. `btw_sid` is the stable routing key
    for the side panel (send Query{sid=btw_sid} for its turns, CloseBtw to end)."""
    type: Literal["btw_opened"] = "btw_opened"
    btw_sid: str
    parent_sid: str
    engine: str


class UserMsg(_Base):
    """A user's query, broadcast to all clients so every device sees the full
    conversation (prompt + response). The originating client dedups by msg_id
    (it already created the turn optimistically on send). Carries images so
    other devices / fresh replays render the attachment."""
    type: Literal["user_msg"] = "user_msg"
    msg_id: str
    prompt: str
    images: Optional[list[dict[str, str]]] = None  # [{media_type, data(base64)}]


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


# ---- sessions (list / switch / new) ----

class SessionInfo(BaseModel):
    """A row in the sessions sidebar (subset of SDK SDKSessionInfo)."""
    model_config = ConfigDict(extra="forbid")
    session_id: str
    summary: Optional[str] = None
    last_modified: Optional[str] = None
    first_prompt: Optional[str] = None
    git_branch: Optional[str] = None
    cwd: Optional[str] = None
    tag: Optional[str] = None  # SDK session tag; "archived" hides the card in the sidebar
    state: Optional[State] = None  # if resident: this session's idle/running/... (sidebar status dot)
    engine: Optional[str] = None  # "claude" | "codex"; None = claude (legacy sidebar badge)


class ListSessions(_Base):
    """client -> wrapper: request the session list. `engine` picks the backend's
    session store (Claude ~/.claude/projects vs Codex ~/.codex/sessions);
    optional, default claude."""
    type: Literal["list_sessions"] = "list_sessions"
    engine: Literal["claude", "codex"] = "claude"


class SessionList(_Base):
    """wrapper -> client: the sessions (downstream so a reconnect restores it)."""
    type: Literal["session_list"] = "session_list"
    sessions: list[SessionInfo]


class SwitchSession(_Base):
    """client -> wrapper: resume a different existing session. `engine` tells the
    wrapper which backend to resume it with (codex threads resume differently)."""
    type: Literal["switch_session"] = "switch_session"
    session_id: str
    engine: Optional[str] = None


class NewSession(_Base):
    """client -> wrapper: start a fresh session (no resume). Optional `cwd`
    spawns it in that directory (default = the wrapper's current cc_cwd).
    `engine` selects the backend — Claude Code (default) or Codex. Optional
    `model`/`effort` pre-select the model and reasoning strength AT SPAWN — so
    the very first turn already uses them (effort especially: applying it at
    spawn avoids the respawn-with-resume that a post-spawn set_effort forces).
    All fields optional and backward-compatible (no PROTOCOL_VERSION bump): an
    old client omits them and gets the wrapper's defaults, exactly as before."""
    type: Literal["new_session"] = "new_session"
    cwd: Optional[str] = None
    engine: Literal["claude", "codex"] = "claude"
    model: Optional[str] = None    # None -> engine default (settings.json / codex config)
    effort: Optional[str] = None   # low|medium|high|xhigh|max; None -> engine default


class SessionFocus(_Base):
    """wrapper -> client: NON-destructive view change — the user switched which
    resident session they're viewing. The client just swaps its view to this
    session; turns are NOT cleared (they're already in memory).

    Focus-moving ONLY: sending this switches the client's view. A brand-new
    session captures its real cc id mid-turn — that is a re-key, NOT a focus
    change, so it uses SessionRekey (below), else a background session capturing
    its id would yank the user's view (focus-steal)."""
    type: Literal["session_focus"] = "session_focus"
    session_id: str
    cwd: Optional[str] = None


class SessionRekey(_Base):
    """wrapper -> client: a resident session's pool key changed from a temp key
    (`tmp-<uuid>`, assigned to a brand-new session before its id is known) to
    its real cc session id, captured from the first ResultMessage/init. The
    client renames its runtime entry old_key -> session_id and migrates that
    session's replay cursor. This does NOT move focus — focus only follows if
    the client was already viewing old_key. Splitting this out from
    SessionFocus is what prevents focus-steal when a *background* new session
    captures its id."""
    type: Literal["session_rekey"] = "session_rekey"
    old_key: str
    session_id: str
    cwd: Optional[str] = None


class RenameSession(_Base):
    """client -> wrapper: set a session's custom title (appended to its jsonl)."""
    type: Literal["rename_session"] = "rename_session"
    session_id: str
    title: str


class ArchiveSession(_Base):
    """client -> wrapper: toggle the "archived" tag on a session."""
    type: Literal["archive_session"] = "archive_session"
    session_id: str
    archived: bool


# ---- directory picker (for creating a session in an arbitrary cwd) ----

class ListDir(_Base):
    """client -> wrapper: list subdirectories of a path on the wrapper host.
    Used by the directory picker when creating a session in an arbitrary cwd.
    path=None starts at $HOME."""
    type: Literal["list_dir"] = "list_dir"
    path: Optional[str] = None


class DirList(_Base):
    """wrapper -> client: subdirectories of the requested path. One-shot like
    SessionList (not buffered/replayed). `parent` is the parent dir for the
    "go up" button; null at filesystem root. Hidden dirs (dotfiles) are omitted."""
    type: Literal["dir_list"] = "dir_list"
    path: str
    parent: Optional[str] = None
    dirs: list[dict[str, str]] = []  # each: {name, path}


# ---- model catalog (the engine is the source of truth, not the client) ----

class GetModels(_Base):
    """client -> wrapper: what models does this engine actually offer?

    Only `codex` answers with real data: its app-server's `model/list` reports each
    model's `supportedReasoningEfforts` + `defaultReasoningEffort`. cc has no such
    RPC, so the client keeps its static table for engine="cc"."""
    type: Literal["get_models"] = "get_models"
    engine: Optional[str] = None
    client_id: Optional[str] = None  # requester, so the wrapper routes Models back to=<client_id>


class Models(_Base):
    """wrapper -> client: the engine's model catalog (one-shot, like DirList — not
    seq'd/buffered). Each entry: {id, display_name, description, efforts,
    default_effort, is_default}. `efforts` is authoritative: turn/start does NOT
    validate the level (it accepts `bogus-zzz`), so offering one the model lacks
    only fails later inside the model API. Empty list = we couldn't read it; the
    client falls back to its static table rather than rendering nothing.

    `default_model` is what a NEW session starts on — codex's ~/.codex/config.toml
    `model`, i.e. the same default the user's terminal codex inherits. It is NOT the
    focused session's model (that's per-session, carried in its own rollout)."""
    type: Literal["models"] = "models"
    engine: str
    models: list[dict[str, Any]] = []
    default_model: Optional[str] = None


class SetPerm(_Base):
    """client -> wrapper: switch the cc session's permission mode (runtime, no reconnect)."""
    type: Literal["set_perm"] = "set_perm"
    mode: str


class Perm(_Base):
    """The cc session's current permission mode. Downstream so a reconnecting
    client restores the readout."""
    type: Literal["perm"] = "perm"
    mode: str


class GetContext(_Base):
    """client -> wrapper: request current context window usage."""
    type: Literal["get_context"] = "get_context"


class ContextReport(_Base):
    """wrapper -> client: context window usage (one-shot response to GetContext,
    like SessionList — not buffered)."""
    type: Literal["context_report"] = "context_report"
    total_tokens: int
    max_tokens: int
    percentage: float
    model: Optional[str] = None
    is_auto_compact_enabled: Optional[bool] = None
    categories: list[dict[str, Any]] = []


class GetDiff(_Base):
    """client -> wrapper: request a git diff (context + line numbers) for a file.
    `theme` picks delta's light/dark rendering so the panel matches the app."""
    type: Literal["get_diff"] = "get_diff"
    file: str
    theme: str = "light"


class DiffReport(_Base):
    """wrapper -> client: git diff text (one-shot, like ContextReport)."""
    type: Literal["diff_report"] = "diff_report"
    file: str
    diff: str


class GetHistory(_Base):
    """client -> wrapper: request a session's history, read ON-DEMAND from its
    transcript (NOT the ring buffer, NOT requiring the session to be resident).
    This replaces per-session buffer replay on hello — history is fetched once
    when a session is opened, like a web chat's GET /conversation. `before`/
    `limit` page older turns (load-more); omit both for the whole history."""
    type: Literal["get_history"] = "get_history"
    session_id: str
    client_id: Optional[str] = None  # requester, so the wrapper routes History back to=<client_id>
    cwd: Optional[str] = None
    before: Optional[str] = None  # oldest already-loaded turn's msg_id — page strictly older than this
    limit: Optional[int] = None   # max turns to return (null = all)


class History(_Base):
    """wrapper -> client: a session's history as ONE bulk frame (one-shot, like
    ContextReport/SessionList — NOT seq'd/buffered). `events` are already-
    serialized narrative event dicts (same shape as the live stream) that the
    client applies in a single reducer pass into runtimes[session_id], deduped
    by msg_id/message_id against any live tail. Routed to=<client_id>, EXCEPT the
    mirror push below, which is broadcast."""
    type: Literal["history"] = "history"
    session_id: str
    events: list[dict[str, Any]] = []
    has_more: bool = False            # older turns exist beyond what's returned (pagination)
    oldest_id: Optional[str] = None   # first returned turn's msg_id — cursor for load-more
    newest_id: Optional[str] = None   # last returned turn's msg_id
    before: Optional[str] = None      # echoes the request's `before`: set => this is an OLDER page (client prepends)
    # True => this session's transcript is being appended to by an EXTERNAL process
    # (a native `claude`/`codex` in the user's terminal), not by us. The wrapper
    # mirrors those appends by broadcasting a fresh History; the client renders the
    # session READ-ONLY, since a cc session has a single owner and typing here would
    # fork the conversation. Additive + defaulted, so no PROTOCOL_VERSION bump.
    external: bool = False


class AskUser(_Base):
    """wrapper -> client: the agent called the `ask_user` MCP tool and is
    blocked awaiting the user's choice. The client renders a question card;
    the user's pick is returned via AnswerQuestion. ask_id correlates the two.
    The wrapper's MCP handler awaits a Future keyed by ask_id."""
    type: Literal["ask_user"] = "ask_user"
    ask_id: str
    question: str
    options: list[dict[str, str]] = []  # each: {label, ds?}


class AnswerQuestion(_Base):
    """client -> wrapper: the user's answer to an AskUser prompt. answer is the
    selected option's label (or free text if the agent allowed it)."""
    type: Literal["answer_question"] = "answer_question"
    ask_id: str
    answer: str


AnyMessage = Union[
    Hello, Query, Interrupt, SetModel, SetEffort, SetServiceTier, SetPerm, Fast, OpenBtw, CloseBtw, BtwOpened, GetContext, GetDiff, GetHistory, GetModels, ListSessions, SwitchSession, NewSession, ListDir, Ping, Pong,
    ReplayStart, ReplayEnd, Snapshot, StateEvent, Model, Effort, Perm, ContextReport, DiffReport, History, Models, AskUser, AnswerQuestion,
    SessionList, SessionFocus, SessionRekey, RenameSession, ArchiveSession, DirList,
    UserMsg, AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    TurnEnd, Error, WrapperDisconnected, WrapperReconnected,
]

# Session-narrative events the wrapper seqs and buffers. Replay/snapshot/
# control frames (replay_start, replay_end, snapshot, wrapper_disconnected,
# wrapper_reconnected) are synthesized per-reconnect and are NOT seq'd/buffered.
DOWNSTREAM_TYPES = frozenset({
    "user_msg", "state", "model", "effort", "perm", "fast", "btw_opened",
    "assistant_msg_start", "delta", "tool_use", "tool_result",
    "assistant_msg_end", "turn_end", "error", "ask_user",
})

_TYPE_MAP: dict[str, type[BaseModel]] = {
    "hello": Hello,
    "query": Query,
    "interrupt": Interrupt,
    "set_model": SetModel,
    "set_effort": SetEffort,
    "set_service_tier": SetServiceTier,
    "open_btw": OpenBtw,
    "close_btw": CloseBtw,
    "btw_opened": BtwOpened,
    "set_perm": SetPerm,
    "get_context": GetContext,
    "get_diff": GetDiff,
    "get_history": GetHistory,
    "get_models": GetModels,
    "models": Models,
    "list_sessions": ListSessions,
    "switch_session": SwitchSession,
    "new_session": NewSession,
    "rename_session": RenameSession,
    "archive_session": ArchiveSession,
    "list_dir": ListDir,
    "dir_list": DirList,
    "ping": Ping,
    "pong": Pong,
    "replay_start": ReplayStart,
    "replay_end": ReplayEnd,
    "snapshot": Snapshot,
    "state": StateEvent,
    "model": Model,
    "effort": Effort,
    "fast": Fast,
    "perm": Perm,
    "context_report": ContextReport,
    "diff_report": DiffReport,
    "history": History,
    "ask_user": AskUser,
    "answer_question": AnswerQuestion,
    "session_list": SessionList,
    "session_focus": SessionFocus,
    "session_rekey": SessionRekey,
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
