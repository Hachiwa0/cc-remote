"""Translate DeepSeek Harness session events into cc-remote wire events."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote

from cc_remote.protocol import (
    AssistantMsgEnd,
    AssistantMsgStart,
    Delta,
    Effort,
    Model,
    ProcessEvent,
    ToolResult,
    ToolUse,
    TurnEnd,
    TurnPlan,
    TurnResult,
    TurnSteered,
    UserMsg,
    MAX_SAFE_WIRE_INTEGER,
)


class DshEventError(ValueError):
    """A DSH event is malformed or cannot be reconstructed safely."""


_SESSION_CONFIGURATION_EVENTS = frozenset({
    "agent-preset/selected",
    "approval/policy",
    "permission/preset",
    "plan/mode",
    "sandbox/mode",
})


def encode_dsh_model(provider: str, model: str) -> str:
    if not provider or not model:
        raise ValueError("DSH provider and model must not be empty")
    value = f"dsh://{quote(provider, safe='')}/{quote(model, safe='')}"
    if len(value) > 256 or "\x00" in value:
        raise ValueError("DSH model id cannot be represented on the wire")
    return value


def decode_dsh_model(value: str) -> tuple[str, str]:
    if not value.startswith("dsh://"):
        raise ValueError("not a DSH model id")
    remainder = value[6:]
    provider, separator, model = remainder.partition("/")
    if not separator or not provider or not model:
        raise ValueError("invalid DSH model id")
    decoded = unquote(provider), unquote(model)
    if (
        not all(decoded)
        or any("\x00" in part for part in decoded)
        or encode_dsh_model(*decoded) != value
    ):
        raise ValueError("invalid DSH model id")
    return decoded


def dsh_message_id(seq: int) -> str:
    return f"dsh-msg-{seq}"


def dsh_fork_point(seq: int) -> str:
    return f"dsh-seq-{seq}"


def parse_dsh_fork_point(value: str) -> int:
    prefix = "dsh-seq-"
    if not value.startswith(prefix):
        raise ValueError("invalid DSH fork point")
    raw = value[len(prefix):]
    if not raw.isdigit():
        raise ValueError("invalid DSH fork point")
    seq = int(raw)
    # ``session.fork`` consumes this sequence as ``beforeSeq = seq + 1``.
    # Keep the derived Typert/JSON value representable by JavaScript too.
    if seq < 0 or seq >= MAX_SAFE_WIRE_INTEGER:
        raise ValueError("invalid DSH fork point")
    return seq


def parse_dsh_history_cursor(value: str) -> int:
    for prefix in ("dsh-msg-", "dsh-auto-"):
        if value.startswith(prefix):
            raw = value[len(prefix):]
            if raw.isdigit():
                seq = int(raw)
                if 0 <= seq <= MAX_SAFE_WIRE_INTEGER:
                    return seq
    raise ValueError("invalid DSH history cursor")


def _wire_id(prefix: str, value: Any) -> str:
    raw = str(value)
    candidate = f"{prefix}-{raw}"
    if (
        len(candidate) <= 128
        and candidate
        and all(ch.isalnum() or ch in "._:@-" for ch in candidate)
    ):
        return candidate
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def dsh_command_item_id(
    command_id: str,
    client_msg_id: str | None = None,
) -> str:
    """Return one stable process identity for a DSH command lifecycle.

    A browser-originated command uses its private message id so the immediate
    receipt projection and the later source-history rebuild fold into the same
    process card.  Other clients' commands stay keyed by DSH's durable id.
    """
    if client_msg_id:
        return _wire_id("dsh-command-client", client_msg_id)
    return _wire_id("dsh-command", command_id)


def _event_time(event: dict[str, Any]) -> float:
    value = event.get("time")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
    ):
        raise DshEventError("DSH event has an invalid timestamp")
    return float(value) / 1000.0


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    values: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"text", "reasoning"} and isinstance(block.get("text"), str):
            values.append(block["text"])
        elif block_type not in {"image", "tool-call", "tool-result"}:
            values.append(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(value for value in values if value)


def _tool_result_text(message: Any) -> tuple[str, bool]:
    if not isinstance(message, dict):
        return "", True
    content = message.get("content")
    if not isinstance(content, list):
        return "", True
    texts: list[str] = []
    is_error = False
    for outer in content:
        if not isinstance(outer, dict) or outer.get("type") != "tool-result":
            continue
        is_error = is_error or bool(outer.get("isError"))
        inner = outer.get("content")
        if isinstance(inner, list):
            rendered = _content_text(inner)
            if rendered:
                texts.append(rendered)
    return "\n".join(texts), is_error


@dataclass
class _AssistantBlock:
    started: bool = False
    ended: bool = False
    text_chars: int = 0
    text_digest: Any = field(default_factory=hashlib.sha256, repr=False)
    channel: str = "unknown"

    def append(self, text: str) -> None:
        self.text_chars += len(text)
        self.text_digest.update(text.encode("utf-8", "surrogatepass"))

    def suffix(self, assembled: str) -> str:
        if self.text_chars == 0:
            return assembled
        if len(assembled) < self.text_chars:
            return ""
        prefix = assembled[:self.text_chars]
        digest = hashlib.sha256(
            prefix.encode("utf-8", "surrogatepass")
        ).digest()
        if digest != self.text_digest.digest():
            return ""
        return assembled[self.text_chars:]


@dataclass(frozen=True)
class _CommandBlock:
    message_id: str
    item_id: str
    title: str
    prompt: str
    started_at: float
    nested_in_turn: bool
    client_msg_id: str | None = None
    has_run: bool = True


@dataclass
class DshStreamTranslator:
    """Stateful translator for one DSH session in source sequence order."""

    strict_history: bool = False
    active_turn: int | None = None
    turn_started_at: float | None = None
    turn_start_seq: int | None = None
    turn_has_visible_user: bool = False
    turn_presentation_id: str | None = None
    # DSH emits plugin-authored ``user/message`` context before the direct
    # human message in some turns.  Those rows are useful in the process
    # timeline, but they cannot own a conversation turn.  Hold them until the
    # exact human owner arrives (or until genuine autonomous output proves the
    # turn has no human owner) so live delivery and history rebuild never
    # synthesize an orphan ``dsh-auto-*`` row.
    pending_contexts: list[ProcessEvent] = field(default_factory=list)
    blocks: dict[tuple[int, int, int], _AssistantBlock] = field(
        default_factory=dict
    )
    next_step_pending: list[str] = field(default_factory=list)
    next_step_claimed: set[str] = field(default_factory=set)
    commands: dict[str, _CommandBlock] = field(default_factory=dict)
    # Permission changes are composer controls in DSH, not conversation turns.
    # Retain their ids only until command/done so both live and rebuilt history
    # omit the native control lifecycle.
    hidden_commands: set[str] = field(default_factory=set)

    MAX_ACTIVE_BLOCKS = 4096
    MAX_INBOX_IDENTITIES = 4096
    MAX_ACTIVE_COMMANDS = 4096
    MAX_PENDING_CONTEXTS = 128

    def feed(
        self,
        event: dict[str, Any],
        *,
        view: Any = None,
        command_client_id: str | None = None,
    ) -> list[Any]:
        self._validate_event(event)
        event_type = event["type"]
        data = event["data"]
        seq = event["seq"]
        ts = _event_time(event)

        if event_type == "turn/start":
            turn = self._required_int(data, "turn")
            # DSH's direct user append and turn/start travel over independent
            # mux paths, so the user identity can arrive first. Preserve that
            # exact presentation owner instead of synthesizing a duplicate row
            # when the model stream begins.
            has_pending_user = bool(
                self.active_turn is None
                and self.turn_start_seq is None
                and self.turn_has_visible_user
                and self.turn_presentation_id is not None
            )
            self.active_turn = turn
            self.turn_started_at = ts
            self.turn_start_seq = seq
            if not has_pending_user:
                self.turn_has_visible_user = False
                self.turn_presentation_id = None
            return []

        if event_type == "turn/end":
            turn = self._required_int(data, "turn")
            reason = data.get("reason")
            if not isinstance(reason, dict) or not isinstance(reason.get("kind"), str):
                raise DshEventError("DSH turn/end omitted its reason")
            kind = reason["kind"]
            is_error = kind in {"aborted", "blocked", "error", "interrupted"}
            duration_ms = 0
            if self.turn_started_at is not None:
                duration_ms = max(0, round((ts - self.turn_started_at) * 1000))
            leading = self._ensure_visible_turn(ts)
            presentation_id = self.turn_presentation_id
            self.blocks = {
                key: block for key, block in self.blocks.items()
                if key[0] != turn
            }
            self.active_turn = None
            self.turn_started_at = None
            self.turn_start_seq = None
            self.turn_has_visible_user = False
            self.turn_presentation_id = None
            self.pending_contexts.clear()
            return [*leading, TurnEnd(
                ts=ts,
                turn_id=dsh_fork_point(seq),
                presentation_id=presentation_id,
                result=TurnResult(
                    subtype="success" if kind == "completed" else kind,
                    duration_ms=duration_ms,
                    is_error=is_error,
                ),
            )]

        if event_type == "user/message":
            if not self._is_append_surface(event):
                return []
            message = data
            source = message.get("source")
            if not isinstance(source, dict):
                raise DshEventError("DSH user/message omitted its source")
            native_message_id = message.get("id")
            steered = (
                isinstance(native_message_id, str)
                and native_message_id in self.next_step_claimed
            )
            if isinstance(native_message_id, str):
                self.next_step_claimed.discard(native_message_id)
            if source.get("kind") == "user":
                self.turn_has_visible_user = True
                presentation_id = dsh_message_id(seq)
                self.turn_presentation_id = presentation_id
                client_id = source.get("rpcId")
                if not self._valid_wire_id(client_id):
                    client_id = None
                prompt = _content_text(message.get("content"))[:2 * 1024 * 1024]
                if steered:
                    visible = TurnSteered(
                        ts=ts,
                        msg_id=presentation_id,
                        client_msg_id=client_id,
                        turn_id=_wire_id(
                            "dsh-turn",
                            self.active_turn
                            if self.active_turn is not None else seq,
                        ),
                        prompt=prompt,
                    )
                else:
                    visible = UserMsg(
                        ts=ts,
                        msg_id=presentation_id,
                        client_msg_id=client_id,
                        prompt=prompt,
                    )
                return [visible, *self._drain_pending_contexts()]
            summary = _content_text(message.get("content"))
            if not summary:
                return []
            context = ProcessEvent(
                ts=ts,
                item_id=_wire_id("dsh-context", seq),
                kind="task",
                phase="snapshot",
                status="succeeded",
                title=self._context_title(source),
                summary=summary[:64 * 1024],
            )
            if not self.turn_has_visible_user:
                if len(self.pending_contexts) >= self.MAX_PENDING_CONTEXTS:
                    raise DshEventError("DSH pending context limit reached")
                self.pending_contexts.append(context)
                return []
            return [context]

        if event_type == "agent/inbox/spliced":
            self._feed_inbox_splice(data)
            return []

        if event_type == "assistant/chunk":
            return [*self._ensure_visible_turn(ts), *self._feed_chunk(data, ts)]

        if event_type == "assistant/message":
            if not self._is_append_surface(event):
                return []
            return [
                *self._ensure_visible_turn(ts),
                *self._feed_assistant_message(data, ts),
            ]

        if event_type == "tool/call":
            turn = self._required_int(data, "turn")
            step = self._required_int(data, "step")
            call_id = data.get("callId")
            name = data.get("name")
            arguments = data.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise DshEventError("DSH tool/call has an invalid identity")
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else {}
            except ValueError:
                parsed = {"raw": arguments}
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            title = self._view_title(view)
            return [*self._ensure_visible_turn(ts), ToolUse(
                ts=ts,
                message_id=_wire_id("dsh-step", f"{turn}-{step}"),
                tool_use_id=_wire_id("dsh-tool", call_id),
                tool=name,
                input=parsed,
                title=title,
            )]

        if event_type == "tool/result":
            if not self._is_append_surface(event):
                return []
            message = data.get("message")
            content, block_error = _tool_result_text(message)
            source = message.get("source") if isinstance(message, dict) else None
            call_id = source.get("callId") if isinstance(source, dict) else None
            if not isinstance(call_id, str):
                raise DshEventError("DSH tool/result omitted call correlation")
            view_summary = self._view_summary(view)
            return [*self._ensure_visible_turn(ts), ToolResult(
                ts=ts,
                tool_use_id=_wire_id("dsh-tool", call_id),
                content=content,
                is_error=block_error or isinstance(data.get("error"), dict),
                status=("failed" if block_error or data.get("error") else "succeeded"),
                summary=view_summary,
            )]

        if event_type == "todo/write":
            todos = data.get("todos")
            if not isinstance(todos, list):
                raise DshEventError("DSH todo/write omitted todos")
            plan = []
            for todo in todos[:128]:
                if not isinstance(todo, dict) or not isinstance(todo.get("content"), str):
                    continue
                status = {
                    "pending": "pending",
                    "in_progress": "inProgress",
                    "completed": "completed",
                }.get(todo.get("status"), "pending")
                plan.append({"step": todo["content"][:16 * 1024], "status": status})
            if not plan:
                return []
            turn_id = (
                _wire_id("dsh-turn", self.active_turn)
                if self.active_turn is not None else None
            )
            return [*self._ensure_visible_turn(ts), TurnPlan(
                ts=ts,
                item_id=_wire_id("dsh-plan", seq),
                turn_id=turn_id,
                plan=plan,
            )]

        if event_type == "request/header":
            header = data.get("header")
            config = header.get("config") if isinstance(header, dict) else None
            if not isinstance(config, dict):
                raise DshEventError("DSH request/header omitted config")
            provider = config.get("provider")
            model = config.get("model")
            if not isinstance(provider, str) or not isinstance(model, str):
                raise DshEventError("DSH request/header omitted model route")
            values: list[Any] = [Model(ts=ts, model=encode_dsh_model(provider, model))]
            effort = config.get("reasoningEffort")
            if isinstance(effort, str) and effort:
                values.append(Effort(ts=ts, effort=effort))
            return values

        if event_type == "command/run":
            command_id = self._command_id(data)
            if command_id in self.commands or command_id in self.hidden_commands:
                raise DshEventError("DSH command/run repeated commandId")
            if (
                len(self.commands) + len(self.hidden_commands)
                >= self.MAX_ACTIVE_COMMANDS
            ):
                raise DshEventError("DSH active command limit reached")
            name = data.get("name")
            args = data.get("args")
            source = data.get("source")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 512
                or (args is not None and not isinstance(args, str))
                or not isinstance(source, dict)
                or source.get("kind") != "user"
            ):
                raise DshEventError("DSH command/run has an invalid payload")
            if name == "permission":
                self.hidden_commands.add(command_id)
                return []
            client_id = (
                command_client_id
                if self._valid_wire_id(command_client_id) else None
            )
            prompt = f"/{name}{args or ''}"[:2 * 1024 * 1024]
            title = f"/{name}"[:1024]
            nested = self.active_turn is not None
            block = _CommandBlock(
                message_id=dsh_message_id(seq),
                item_id=dsh_command_item_id(command_id, client_id),
                title=title,
                prompt=prompt,
                started_at=ts,
                nested_in_turn=nested,
                client_msg_id=client_id,
            )
            self.commands[command_id] = block
            leading: list[Any] = []
            if not nested:
                leading.append(UserMsg(
                    ts=ts,
                    msg_id=block.message_id,
                    client_msg_id=client_id,
                    prompt=prompt,
                ))
            return [*leading, ProcessEvent(
                ts=ts,
                item_id=block.item_id,
                kind="task",
                phase="start",
                status="running",
                title=title,
                command=prompt,
            )]

        if event_type == "command/done":
            command_id = self._command_id(data)
            kind = data.get("kind")
            text = data.get("text")
            source_seq = data.get("sourceEventSeq")
            if (
                kind not in {"success", "error"}
                or (text is not None and not isinstance(text, str))
                or (
                    source_seq is not None
                    and (
                        not isinstance(source_seq, int)
                        or isinstance(source_seq, bool)
                        or source_seq < 0
                        or source_seq >= seq
                    )
                )
            ):
                raise DshEventError("DSH command/done has an invalid payload")
            if command_id in self.hidden_commands:
                self.hidden_commands.discard(command_id)
                return []
            block = self.commands.pop(command_id, None)
            client_id = (
                command_client_id
                if self._valid_wire_id(command_client_id) else None
            )
            if block is None:
                # A history window may begin at the update half of the pair.
                # Preserve the durable outcome as a generic command row rather
                # than dropping it or attaching it to an unrelated model turn.
                block = _CommandBlock(
                    message_id=dsh_message_id(seq),
                    item_id=dsh_command_item_id(command_id, client_id),
                    title="DSH 命令",
                    prompt="",
                    started_at=ts,
                    nested_in_turn=False,
                    client_msg_id=client_id,
                    has_run=False,
                )
            duration_ms = max(0, round((ts - block.started_at) * 1000))
            process = ProcessEvent(
                ts=ts,
                item_id=block.item_id,
                kind="task",
                phase="end",
                status="succeeded" if kind == "success" else "failed",
                title=block.title,
                command=block.prompt or None,
                output=(text[:2 * 1024 * 1024] if text else None),
                duration_ms=duration_ms,
            )
            if block.nested_in_turn:
                return [process]
            leading = [] if block.has_run else [UserMsg(
                ts=block.started_at,
                msg_id=block.message_id,
                client_msg_id=block.client_msg_id,
                prompt=block.prompt,
            )]
            return [*leading, process, TurnEnd(
                ts=ts,
                presentation_id=block.message_id,
                result=TurnResult(
                    subtype=(
                        "command_success" if kind == "success"
                        else "command_error"
                    ),
                    duration_ms=duration_ms,
                    is_error=kind == "error",
                ),
            )]

        if event_type in {"step/start", "step/end", "request/context", "session/end-seed"}:
            return []

        # These records describe persistent session controls, not agent work.
        # Rendering them as process rows before the first human message creates
        # a phantom incomplete conversation turn in both live and history views.
        if event_type in _SESSION_CONFIGURATION_EVENTS:
            return []

        known_generic = {
            "approval/asked", "approval/decided",
            "compaction/start",
            "compaction/prune", "compaction/summary", "compaction/end",
            "feedback/record", "goal/change", "hook/invoked", "hook/result",
            "llm/retry", "llm/retry-started", "schedule/change",
            "session/title-llm-request", "subagent/descriptor",
            "tool-workflow/agent-start", "tool-workflow/agent-end",
            "tool-workflow/run-start", "tool-workflow/run-end",
            "tool/code-dispatch-start", "tool/code-dispatch",
            "web/deepseek-search-llm-request",
        }
        if event_type == "session/title":
            return []
        if event_type in known_generic:
            kind = (
                "compaction" if event_type.startswith("compaction/")
                else "hook" if event_type.startswith("hook/")
                else "agent" if (
                    event_type.startswith("subagent/")
                    or event_type.startswith("tool-workflow/agent-")
                )
                else "web_search" if event_type.startswith("web/")
                else "task"
            )
            phase = (
                "start" if event_type.endswith(("/start", "-start", "/invoked"))
                else "end" if event_type.endswith(("/end", "-end", "/done", "/result"))
                else "snapshot"
            )
            status = (
                "running" if phase == "start"
                else "succeeded" if phase == "end"
                else "unknown"
            )
            return [ProcessEvent(
                ts=ts,
                item_id=_wire_id("dsh-event", seq),
                kind=kind,
                phase=phase,
                status=status,
                title=event_type[:1024],
                # These lifecycle records may be supplied by third-party
                # Cordis plugins.  Their arbitrary data can include hook
                # inputs, environment values or connector credentials.  The
                # semantic event name and lifecycle are enough for Remote's
                # process timeline; never proxy the opaque payload through the
                # relay merely because DSH recognizes its event type.
            )]

        # DSH's log-version contract is explicit: an unknown event may be
        # skipped only when its producer marked it ``ignorable``. Required
        # plugin events can carry relational or surface semantics, so guessing
        # at them would make the live projection disagree with a later history
        # rebuild. Safe extension telemetry still gets a visible process row,
        # but its arbitrary payload is deliberately not proxied through Relay.
        if event.get("ignorable") is not True:
            mode = "history" if self.strict_history else "live"
            raise DshEventError(
                f"unsupported required DSH {mode} event: {event_type}"
            )
        return [ProcessEvent(
            ts=ts,
            item_id=_wire_id("dsh-event", seq),
            kind="task",
            phase="snapshot",
            status="unknown",
            title=event_type[:1024],
            summary="DSH 插件事件（可安全忽略）",
        )]

    def _feed_chunk(self, data: dict[str, Any], ts: float) -> list[Any]:
        turn = self._required_int(data, "turn")
        step = self._required_int(data, "step")
        chunk = data.get("chunk")
        if not isinstance(chunk, dict) or not isinstance(chunk.get("type"), str):
            raise DshEventError("DSH assistant/chunk has an invalid chunk")
        kind = chunk["type"]
        if kind not in {
            "block-start", "text-delta", "reasoning-delta", "block-end",
            "usage", "finish", "tool-call-delta",
        }:
            return []
        if kind in {"usage", "finish", "tool-call-delta"}:
            return []
        index = self._required_int(chunk, "index")
        key = (turn, step, index)
        if kind == "block-start":
            block_type = chunk.get("blockType")
            if block_type not in {"text", "reasoning"}:
                return []
            block = self._block(key)
            if block.ended:
                return []
            if block_type == "reasoning":
                block.channel = "thinking"
            return self._ensure_started(key, block, ts)
        if kind in {"text-delta", "reasoning-delta"}:
            text = chunk.get("text")
            if not isinstance(text, str):
                raise DshEventError("DSH assistant delta omitted text")
            block = self._block(key)
            if block.ended:
                return []
            if kind == "reasoning-delta":
                block.channel = "thinking"
            events = self._ensure_started(key, block, ts)
            if text:
                block.append(text)
                events.append(Delta(
                    ts=ts,
                    message_id=self._assistant_id(key),
                    text=text,
                    channel=block.channel,
                ))
            return events
        assembled = chunk.get("block")
        if not isinstance(assembled, dict):
            raise DshEventError("DSH block-end omitted its assembled block")
        block_type = assembled.get("type")
        if block_type not in {"text", "reasoning"}:
            return []
        block = self._block(key)
        if block.ended:
            return []
        if block_type == "reasoning":
            block.channel = "thinking"
        events = self._ensure_started(key, block, ts)
        text = assembled.get("text")
        if isinstance(text, str):
            suffix = block.suffix(text)
            if suffix:
                block.append(suffix)
                events.append(Delta(
                    ts=ts,
                    message_id=self._assistant_id(key),
                    text=suffix,
                    channel=block.channel,
                ))
        return events

    def _ensure_visible_turn(self, ts: float) -> list[Any]:
        if self.turn_has_visible_user:
            return self._drain_pending_contexts()
        if self.turn_start_seq is None:
            return []
        self.turn_has_visible_user = True
        self.turn_presentation_id = f"dsh-auto-{self.turn_start_seq}"
        return [UserMsg(
            ts=ts,
            msg_id=self.turn_presentation_id,
            prompt="",
        ), *self._drain_pending_contexts()]

    def _drain_pending_contexts(self) -> list[ProcessEvent]:
        if not self.pending_contexts:
            return []
        contexts = self.pending_contexts
        self.pending_contexts = []
        return contexts

    def _feed_assistant_message(
        self, data: dict[str, Any], ts: float,
    ) -> list[Any]:
        turn = self._required_int(data, "turn")
        step = self._required_int(data, "step")
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            raise DshEventError("DSH assistant/message omitted content")
        has_tools = any(
            isinstance(item, dict) and item.get("type") == "tool-call"
            for item in content
        )
        events: list[Any] = []
        for index, item in enumerate(content):
            if not isinstance(item, dict) or item.get("type") not in {"text", "reasoning"}:
                continue
            key = (turn, step, index)
            block = self._block(key)
            if block.ended:
                continue
            channel = (
                "thinking" if item.get("type") == "reasoning"
                else "commentary" if has_tools else "final"
            )
            block.channel = channel
            events.extend(self._ensure_started(key, block, ts))
            text = item.get("text")
            if isinstance(text, str):
                suffix = block.suffix(text)
                if suffix:
                    block.append(suffix)
                    events.append(Delta(
                        ts=ts,
                        message_id=self._assistant_id(key),
                        text=suffix,
                        channel=channel,
                    ))
            if not block.ended:
                block.ended = True
                events.append(AssistantMsgEnd(
                    ts=ts,
                    message_id=self._assistant_id(key),
                    channel=channel,
                ))
            # The assembled message is the terminal representation for this
            # block. Source seq ordering already deduplicates delivery, so the
            # translator need not retain one object (and its streamed text) for
            # every completed step until turn/end.
            self.blocks.pop(key, None)
        return events

    def _block(self, key: tuple[int, int, int]) -> _AssistantBlock:
        block = self.blocks.get(key)
        if block is not None:
            return block
        if len(self.blocks) >= self.MAX_ACTIVE_BLOCKS:
            raise DshEventError("DSH active assistant block limit reached")
        block = _AssistantBlock()
        self.blocks[key] = block
        return block

    def _feed_inbox_splice(self, data: dict[str, Any]) -> None:
        target = data.get("target")
        if target not in {"next-turn", "next-step"}:
            raise DshEventError("DSH inbox splice has an invalid target")
        start = data.get("start")
        removed_count = data.get("removedCount", 0)
        inserted = data.get("inserted")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 0
            or isinstance(removed_count, bool)
            or not isinstance(removed_count, int)
            or removed_count < 0
            or not isinstance(inserted, list)
        ):
            raise DshEventError("DSH inbox splice has invalid coordinates")
        inserted_ids: list[str] = []
        for message in inserted:
            message_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(message_id, str) or not message_id:
                raise DshEventError("DSH inbox splice omitted message identity")
            inserted_ids.append(message_id)
        if len(set(inserted_ids)) != len(inserted_ids):
            raise DshEventError("DSH inbox splice repeats message identity")
        if target != "next-step":
            return

        # A paginated history window can begin after the insertion but at the
        # matching claim deletion.  That first deletion cannot be reconstructed;
        # discard the partial projection and let later complete insert/claim
        # pairs rebuild exact steering classification for the returned tail.
        if (
            start > len(self.next_step_pending)
            or start + removed_count > len(self.next_step_pending)
        ):
            self.next_step_pending.clear()
            self.next_step_claimed.clear()
            return
        candidate = list(self.next_step_pending)
        removed = candidate[start:start + removed_count]
        candidate[start:start + removed_count] = inserted_ids
        if (
            len(candidate) > self.MAX_INBOX_IDENTITIES
            or len(set(candidate)) != len(candidate)
        ):
            raise DshEventError("DSH inbox projection exceeds its limit")
        self.next_step_pending = candidate
        for message_id in inserted_ids:
            self.next_step_claimed.discard(message_id)
        if data.get("outcome") != "canceled":
            self.next_step_claimed.update(removed)
        if len(self.next_step_claimed) > self.MAX_INBOX_IDENTITIES:
            raise DshEventError("DSH claimed steering projection exceeds its limit")

    def _ensure_started(
        self,
        key: tuple[int, int, int],
        block: _AssistantBlock,
        ts: float,
    ) -> list[Any]:
        if block.started:
            return []
        block.started = True
        return [AssistantMsgStart(
            ts=ts,
            message_id=self._assistant_id(key),
            channel=block.channel,
        )]

    @staticmethod
    def _assistant_id(key: tuple[int, int, int]) -> str:
        turn, step, index = key
        return f"dsh-a-{turn}-{step}-{index}"

    @staticmethod
    def _is_append_surface(event: dict[str, Any]) -> bool:
        return event.get("surfaceOp") == "append"

    @staticmethod
    def _required_int(value: dict[str, Any], key: str) -> int:
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise DshEventError(f"DSH event omitted integer {key}")
        return item

    @staticmethod
    def _command_id(value: dict[str, Any]) -> str:
        command_id = value.get("commandId")
        if (
            not isinstance(command_id, str)
            or not command_id
            or len(command_id.encode("utf-8", "surrogatepass")) > 1024
            or "\x00" in command_id
        ):
            raise DshEventError("DSH command event omitted commandId")
        return command_id

    @staticmethod
    def _validate_event(event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise DshEventError("DSH event must be an object")
        if not isinstance(event.get("type"), str) or not event["type"]:
            raise DshEventError("DSH event omitted type")
        if (
            not isinstance(event.get("seq"), int)
            or isinstance(event.get("seq"), bool)
            or event["seq"] < 0
        ):
            raise DshEventError("DSH event omitted seq")
        if not isinstance(event.get("data"), dict):
            raise DshEventError("DSH event omitted data")
        _event_time(event)

    @staticmethod
    def _valid_wire_id(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and 1 <= len(value) <= 128
            and value[0].isalnum()
            and all(ch.isalnum() or ch in "._:@-" for ch in value)
        )

    @staticmethod
    def _context_title(source: dict[str, Any]) -> str:
        kind = source.get("kind")
        if kind == "plugin" and isinstance(source.get("plugin"), str):
            return f"插件上下文 · {source['plugin']}"[:1024]
        return f"DSH 上下文 · {kind or 'unknown'}"[:1024]

    @staticmethod
    def _view_title(view: Any) -> str | None:
        if not isinstance(view, dict):
            return None
        payload = view.get("view")
        if not isinstance(payload, dict):
            return None
        for key in ("title", "label", "summary"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:1024]
        return None

    @staticmethod
    def _view_summary(view: Any) -> str | None:
        if not isinstance(view, dict):
            return None
        payload = view.get("view")
        if not isinstance(payload, dict):
            return None
        for key in ("summary", "title", "label"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:64 * 1024]
        return None
