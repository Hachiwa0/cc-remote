"""Translate codex app-server notifications into wire-protocol events.

Codex analog of stream.py's StreamTranslator: stateful per turn, emits the SAME
wire events (AssistantMsgStart / Delta / ToolUse / ToolResult / AssistantMsgEnd /
TurnEnd) so the Codex engine reuses the entire client + reducer unchanged. Fed by
CodexHandle (codex_handle.py), which yields raw JSON-RPC notification dicts.

Mapping (validated against real gpt-5.5 turns):
  item/agentMessage/delta {itemId, delta}      -> AssistantMsgStart(once) + Delta
  item/completed  agentMessage {id, text}      -> AssistantMsgEnd
  item/started    commandExecution {id, cmd}   -> ToolUse(shell/listFiles/…)
  item/completed  commandExecution {out, exit} -> ToolResult(is_error = exit != 0)
  item/*          fileChange / mcpToolCall      -> ToolUse / ToolResult
  turn/completed  {turn:{status, durationMs}}   -> TurnEnd
  reasoning / userMessage / hooks / mcp-status  -> skipped (parity with cc)
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    TurnEnd, TurnResult, UserMsg,
)

_TOOL_TYPES = {"commandExecution", "fileChange", "mcpToolCall"}


class CodexStreamTranslator:
    def __init__(self, tool_result_max: int):
        self.tool_result_max = tool_result_max
        self._started: set[str] = set()       # agentMessage itemIds that emitted a start
        self._open_msg: str | None = None      # currently-open assistant message block

    def feed(self, msg: dict) -> list:
        method = msg.get("method")
        p = msg.get("params") or {}
        out: list = []

        if method == "item/agentMessage/delta":
            iid = p.get("itemId") or ""
            if iid and iid not in self._started:
                self._started.add(iid)
                self._open_msg = iid
                out.append(AssistantMsgStart(message_id=iid))
            if p.get("delta"):
                out.append(Delta(message_id=iid, text=p["delta"]))

        elif method == "item/started":
            item = p.get("item") or {}
            if item.get("type") in _TOOL_TYPES:
                out.extend(self._tool_use(item))

        elif method == "item/completed":
            item = p.get("item") or {}
            t = item.get("type")
            if t == "agentMessage":
                iid = item.get("id") or ""
                if iid in self._started:
                    out.append(AssistantMsgEnd(message_id=iid))
                    if self._open_msg == iid:
                        self._open_msg = None
            elif t in _TOOL_TYPES:
                out.append(self._tool_result(item))

        elif method == "turn/completed":
            turn = p.get("turn") or {}
            st = turn.get("status") or "completed"
            # Map codex TurnStatus (completed|interrupted|failed) onto cc's wire
            # subtype vocabulary so the engine-agnostic reducer treats them right:
            # "interrupted" -> "error_during_execution" is the token the client keys
            # on to render the "— 已打断 —" note (verified: turn/interrupt yields
            # turn/completed{status:"interrupted"}).
            subtype = ("success" if st == "completed"
                       else "error_during_execution" if st == "interrupted"
                       else "error")
            out.append(TurnEnd(result=TurnResult(
                subtype=subtype,
                duration_ms=int(turn.get("durationMs") or 0),
                is_error=(st != "completed"),
            )))

        # everything else (reasoning, userMessage, hook/*, mcpServer/startupStatus,
        # thread/status, account/rateLimits, tokenUsage, remoteControl…) -> skip.
        return out

    # ---- helpers ----
    def _ensure_block(self, mid: str, out: list) -> None:
        """A tool card needs an assistant message block to hang under (the reducer
        keys tool cards by message_id); open one lazily if none is active."""
        if self._open_msg is None:
            self._open_msg = mid
            self._started.add(mid)
            out.append(AssistantMsgStart(message_id=mid))

    def _tool_use(self, item: dict) -> list:
        out: list = []
        mid = self._open_msg or item.get("id") or ""
        self._ensure_block(mid, out)
        inp: dict = {}
        for k in ("command", "cwd", "changes"):
            if item.get(k) is not None:
                inp[k] = item[k]
        out.append(ToolUse(
            message_id=self._open_msg or "",
            tool_use_id=item.get("id") or "",
            tool=_tool_name(item),
            input=inp,
        ))
        return out

    def _tool_result(self, item: dict) -> ToolResult:
        text = item.get("aggregatedOutput") or item.get("output") or ""
        if not isinstance(text, str):
            text = str(text)
        truncated = None
        if len(text) > self.tool_result_max:
            text = text[: self.tool_result_max]
            truncated = True
        code = item.get("exitCode")
        return ToolResult(
            tool_use_id=item.get("id") or "",
            content=text,
            is_error=bool(code) if code is not None else False,
            truncated=truncated,
        )


def _tool_name(item: dict) -> str:
    t = item.get("type")
    if t == "commandExecution":
        acts = item.get("commandActions") or []
        if acts and isinstance(acts[0], dict) and acts[0].get("type"):
            return acts[0]["type"]   # listFiles / readFile / editFile / …
        return "shell"
    if t == "fileChange":
        return "apply_patch"
    if t == "mcpToolCall":
        return item.get("toolName") or "mcp"
    return t or "tool"


# ---- helpers the machine loop needs (codex analogs of stream.extract_*) ----

def codex_session_id(msg: dict) -> str | None:
    """Thread id from any notification that carries the thread object."""
    p = msg.get("params") or {}
    th = p.get("thread")
    if isinstance(th, dict):
        return th.get("id") or th.get("sessionId")
    return None


def is_turn_terminal(msg: dict) -> bool:
    """Codex's turn/completed plays the role of Claude's ResultMessage."""
    return msg.get("method") == "turn/completed"


# ---- on-disk Codex rollout -> wire events (session history) ----

def codex_translate_history(path: str, tool_result_max: int) -> tuple[list, str | None]:
    """Translate a Codex rollout .jsonl into wire events (same vocabulary as the
    live stream) + the model used. Codex analog of stream.translate_history.

    A turn = event_msg/user_message -> (function_call/reasoning...) -> agent_message.
    Skips the <environment_context>/<permissions> developer/user envelope messages;
    uses the clean event_msg user_message / agent_message text. Returns
    (events, model)."""
    events: list = []
    model: str | None = None
    turn_open = False
    assistant_open = False
    cur_mid: str | None = None
    last_ts = None

    def _ts(iso: str):
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def ensure_assistant():
        nonlocal assistant_open, cur_mid
        if not assistant_open:
            cur_mid = uuid.uuid4().hex
            assistant_open = True
            events.append(AssistantMsgStart(message_id=cur_mid))

    def close_turn():
        nonlocal turn_open, assistant_open, cur_mid
        if assistant_open and cur_mid:
            events.append(AssistantMsgEnd(message_id=cur_mid))
        assistant_open = False
        cur_mid = None
        if turn_open:
            te = TurnEnd(result=TurnResult(subtype="success", duration_ms=0, is_error=False))
            if last_ts is not None:
                te.ts = last_ts
            events.append(te)
            turn_open = False

    try:
        f = open(path)
    except Exception:
        return [], None
    with f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            ts = _ts(d.get("timestamp", ""))
            if ts is not None:
                last_ts = ts

            if t == "turn_context" and p.get("model"):
                model = p["model"]
            elif t == "event_msg" and p.get("type") == "user_message":
                msg = p.get("message") or ""
                if msg and not msg.lstrip().startswith("<"):
                    close_turn()
                    um = UserMsg(msg_id=uuid.uuid4().hex, prompt=msg)
                    if ts is not None:
                        um.ts = ts
                    events.append(um)
                    turn_open = True
            elif t == "response_item" and p.get("type") == "function_call":
                ensure_assistant()
                events.append(ToolUse(
                    message_id=cur_mid or "",
                    tool_use_id=p.get("call_id") or p.get("id") or "",
                    tool=_hist_tool_name(p.get("name")),
                    input=_hist_tool_input(p.get("arguments")),
                ))
            elif t == "response_item" and p.get("type") == "function_call_output":
                out = p.get("output")
                out = out if isinstance(out, str) else json.dumps(out) if out is not None else ""
                truncated = None
                if len(out) > tool_result_max:
                    out = out[:tool_result_max]
                    truncated = True
                events.append(ToolResult(
                    tool_use_id=p.get("call_id") or "",
                    content=out,
                    is_error=_exit_is_error(out),
                    truncated=truncated,
                ))
            elif t == "event_msg" and p.get("type") == "agent_message":
                ensure_assistant()
                txt = p.get("message") or ""
                if txt:
                    events.append(Delta(message_id=cur_mid, text=txt))
            # session_meta / world_state / reasoning / token_count / task_* : skipped
    close_turn()
    return events, model


def _hist_tool_name(name) -> str:
    if name in ("exec_command", "shell", "local_shell"):
        return "shell"
    if name in ("apply_patch",):
        return "apply_patch"
    return name or "tool"


def _hist_tool_input(arguments) -> dict:
    try:
        a = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except Exception:
        a = {}
    if not isinstance(a, dict):
        return {"args": a}
    out: dict = {}
    if a.get("cmd") is not None:
        out["command"] = a["cmd"]
    if a.get("workdir") is not None:
        out["cwd"] = a["workdir"]
    for k, v in a.items():
        if k not in ("cmd", "workdir", "yield_time_ms"):
            out[k] = v
    return out


def _exit_is_error(output: str) -> bool:
    m = re.search(r"exited with code (\d+)", output or "")
    return bool(m) and m.group(1) != "0"
