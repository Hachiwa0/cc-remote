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

import hashlib
import json
import re
from datetime import datetime

from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    TurnEnd, TurnResult, UserMsg, Error, StateEvent, ERR_CC_CRASH,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input

_TOOL_TYPES = {"commandExecution", "fileChange", "mcpToolCall"}
_MAX_HISTORY_RECORD_CHARS = 16 * 1024 * 1024
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_EMPTY_COMPLETED_MESSAGE = (
    "Codex 回合已结束，但没有返回任何内容；上游服务可能暂时不可用，请重试。"
)


def _bounded_jsonl_records(file):
    """Yield bounded complete records; skip one pathological oversized line."""
    line_no = 0
    while True:
        line = file.readline(_MAX_HISTORY_RECORD_CHARS + 1)
        if not line:
            return
        line_no += 1
        complete = line.endswith("\n") or len(line) < _MAX_HISTORY_RECORD_CHARS + 1
        if complete:
            yield line_no, line
            continue
        while line and not line.endswith("\n"):
            line = file.readline(_MAX_HISTORY_RECORD_CHARS + 1)


class CodexStreamTranslator:
    def __init__(self, tool_result_max: int):
        self.tool_result_max = tool_result_max
        self._started: set[str] = set()       # agentMessage itemIds that emitted a start
        self._text_seen: set[str] = set()     # ids with at least one non-empty delta
        self._open_msg: str | None = None      # currently-open assistant message block
        self._visible_output = False           # text or a visible tool card this turn
        self._terminal_error = False            # a non-retrying error was already emitted

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
                self._text_seen.add(iid)
                self._visible_output = True
                out.append(Delta(message_id=iid, text=p["delta"]))

        elif method == "item/started":
            item = p.get("item") or {}
            if item.get("type") in _TOOL_TYPES:
                self._visible_output = True
                out.extend(self._tool_use(item))

        elif method == "item/completed":
            item = p.get("item") or {}
            t = item.get("type")
            if t == "agentMessage":
                text = item.get("text") if isinstance(item.get("text"), str) else ""
                iid = item.get("id") or (
                    hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
                    if text else "")
                # Some providers send only item/completed with the final text and
                # no delta notification. Preserve that answer instead of turning
                # it into a false empty-completed error.
                if text and iid not in self._text_seen:
                    if iid not in self._started:
                        self._started.add(iid)
                        self._open_msg = iid
                        out.append(AssistantMsgStart(message_id=iid))
                    self._text_seen.add(iid)
                    self._visible_output = True
                    out.append(Delta(message_id=iid, text=text))
                if iid in self._started:
                    out.append(AssistantMsgEnd(message_id=iid))
                    if self._open_msg == iid:
                        self._open_msg = None
            elif t in _TOOL_TYPES:
                self._visible_output = True
                out.append(self._tool_result(item))

        elif method == "error":
            # Retrying provider failures are progress, not terminal errors. Emit a
            # running StateEvent so old clients remain compatible while new clients
            # can replace the generic spinner with a useful status.
            err = p.get("error") if isinstance(p.get("error"), dict) else {}
            if p.get("willRetry"):
                out.append(StateEvent(
                    state="running",
                    phase="retrying",
                    detail=_retry_detail(err),
                ))
            else:
                msg = err.get("message") or "codex 出错"
                det = err.get("additionalDetails")
                message_text, _ = bounded_text(msg, 24 * 1024)
                details_text, _ = bounded_text(det, 8 * 1024)
                detail = "codex: " + message_text
                if details_text:
                    detail += " — " + details_text
                self._terminal_error = True
                out.append(Error(code=ERR_CC_CRASH, message=detail))

        elif method == "turn/completed":
            turn = p.get("turn") or {}
            st = turn.get("status") or "completed"
            # a failed turn carries its reason in turn.error — surface it (the
            # error notifications above may not have fired for every failure mode).
            if st == "failed":
                te = turn.get("error")
                emsg = te.get("message") if isinstance(te, dict) else (te if isinstance(te, str) else None)
                if emsg and not self._terminal_error:
                    message_text, _ = bounded_text(emsg, 32 * 1024)
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message="codex 回合失败: " + message_text,
                    ))
                    self._terminal_error = True
                elif not self._terminal_error:
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message="Codex 回合失败，但没有返回错误详情。",
                    ))
                    self._terminal_error = True
            # Codex 0.144.1 can record an upstream 503 as completed/error=null with
            # only the userMessage item. Treat that impossible "empty success" as
            # a terminal failure, while allowing tool-only turns as visible output.
            if st == "completed" and not self._visible_output:
                if not self._terminal_error:
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message=_EMPTY_COMPLETED_MESSAGE,
                    ))
                    self._terminal_error = True
                st = "failed"
            elif st == "completed" and self._terminal_error:
                st = "failed"
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
            input=bounded_tool_input(inp, self.tool_result_max),
        ))
        return out

    def _tool_result(self, item: dict) -> ToolResult:
        text, was_truncated = bounded_text(
            item.get("aggregatedOutput") or item.get("output") or "",
            self.tool_result_max,
        )
        truncated = True if was_truncated else None
        code = item.get("exitCode")
        return ToolResult(
            tool_use_id=item.get("id") or "",
            content=text,
            is_error=bool(code) if code is not None else False,
            truncated=truncated,
        )


def _retry_detail(error: dict) -> str:
    """Return a bounded, credential-free retry status for the client."""
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    details = (error.get("additionalDetails")
               if isinstance(error.get("additionalDetails"), str) else "")
    combined = message + " " + details
    status_match = re.search(r"\b([45]\d\d)\b", combined)
    status = status_match.group(1) if status_match else _structured_http_status(error)
    attempt = re.search(r"\b(\d+\s*/\s*\d+)\b", combined)
    if status:
        text = f"上游服务返回 HTTP {status}，Codex 正在重试"
    else:
        text = "Codex 上游请求暂时失败，正在重试"
    if attempt:
        text += f"（{attempt.group(1).replace(' ', '')}）"
    return text + "…"


def _structured_http_status(error: dict) -> str | None:
    """Find a bounded codexErrorInfo.httpStatusCode without exposing details."""
    stack = [error.get("codexErrorInfo")]
    seen = 0
    while stack and seen < 32:
        value = stack.pop()
        seen += 1
        if not isinstance(value, dict):
            continue
        status = value.get("httpStatusCode")
        if isinstance(status, int) and 400 <= status <= 599:
            return str(status)
        stack.extend(list(value.values())[:16])
    return None


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
    session_id: str | None = None
    turn_open = False
    active_turn_id: str | None = None
    active_msg_id: str | None = None
    pending_turn_id: str | None = None
    turn_visible = False
    turn_text_visible = False
    assistant_open = False
    cur_mid: str | None = None
    last_ts = None
    pending_images: list = []   # input_image blocks seen before the next user_message

    def _ts(iso: str):
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _stable_id(kind: str, line_no: int, raw_ts: str = "", identity=None) -> str:
        """Deterministic fallback for rollout records that carry no item id."""
        seed = "\0".join((
            session_id or path,
            kind,
            str(identity or active_turn_id or ""),
            str(line_no),
            raw_ts,
        ))
        return hashlib.sha256(seed.encode("utf-8", "surrogatepass")).hexdigest()[:32]

    def _history_id(value, kind: str, line_no: int, raw_ts: str = "") -> str:
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
            return value
        identity = value[:1024] if isinstance(value, str) else type(value).__name__
        return _stable_id(kind, line_no, raw_ts, identity)

    def _duration(payload: dict) -> int:
        try:
            return int(payload.get("duration_ms") or payload.get("durationMs") or 0)
        except (TypeError, ValueError):
            return 0

    def _completed_ts(payload: dict, fallback):
        value = payload.get("completed_at") or payload.get("completedAt")
        if isinstance(value, (int, float)):
            value = float(value)
            return value / 1000 if value > 100_000_000_000 else value
        if isinstance(value, str):
            return _ts(value) or fallback
        return fallback

    def ensure_assistant(line_no: int, raw_ts: str = "", item_id=None):
        nonlocal assistant_open, cur_mid
        if not assistant_open:
            cur_mid = _history_id(item_id, "assistant", line_no, raw_ts)
            assistant_open = True
            events.append(AssistantMsgStart(message_id=cur_mid))

    def close_assistant():
        nonlocal assistant_open, cur_mid
        if assistant_open and cur_mid:
            events.append(AssistantMsgEnd(message_id=cur_mid))
        assistant_open = False
        cur_mid = None

    def close_turn(subtype: str, duration_ms: int, is_error: bool, completed_ts=None):
        nonlocal turn_open, active_turn_id, active_msg_id, pending_turn_id
        nonlocal assistant_open, cur_mid, turn_visible, turn_text_visible
        if not turn_open:
            return
        close_assistant()
        te = TurnEnd(result=TurnResult(
            subtype=subtype, duration_ms=duration_ms, is_error=is_error))
        terminal_ts = completed_ts if completed_ts is not None else last_ts
        if terminal_ts is not None:
            te.ts = terminal_ts
        events.append(te)
        turn_open = False
        pending_turn_id = None
        active_turn_id = None
        active_msg_id = None
        turn_visible = False
        turn_text_visible = False

    try:
        f = open(path)
    except Exception:
        return [], None
    with f:
        for line_no, line in _bounded_jsonl_records(f):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = d.get("type")
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            raw_ts = d.get("timestamp", "")
            ts = _ts(raw_ts)
            payload_type = p.get("type")

            if t == "session_meta" and p.get("id"):
                session_id = str(p["id"])
            elif t == "turn_context":
                if p.get("model"):
                    model = p["model"]
                context_turn_id = p.get("turn_id")
                if context_turn_id:
                    context_turn_id = str(context_turn_id)
                    # Codex can start an automatic continuation with a new turn_id
                    # but no new user_message. It is still the same visible chat
                    # turn, so only a real user_message creates a boundary.
                    pending_turn_id = context_turn_id
            elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                # the raw user turn carries any uploaded images (input_image, a
                # data: URI). It precedes the clean event_msg/user_message; buffer
                # them and attach to that UserMsg so images replay on reload.
                for it in (p.get("content") or []):
                    if isinstance(it, dict) and it.get("type") == "input_image":
                        img = _data_uri_to_img(it.get("image_url"))
                        if img:
                            pending_images.append(img)
            elif t == "event_msg" and payload_type == "task_started":
                next_turn_id = p.get("turn_id")
                if next_turn_id:
                    next_turn_id = str(next_turn_id)
                    pending_turn_id = next_turn_id
            elif t == "event_msg" and payload_type == "user_message":
                msg = p.get("message") or ""
                if msg and not msg.lstrip().startswith("<"):
                    next_turn_id = p.get("turn_id") or pending_turn_id
                    if turn_open:
                        close_turn("error", 0, True)
                    active_turn_id = str(next_turn_id) if next_turn_id else None
                    pending_turn_id = active_turn_id
                    uid = _history_id(active_turn_id, "user", line_no, raw_ts)
                    active_msg_id = uid
                    um = UserMsg(msg_id=uid, prompt=msg)
                    if pending_images:
                        um.images = pending_images
                    if ts is not None:
                        um.ts = ts
                    events.append(um)
                    turn_open = True
                pending_images = []   # consume (per user turn)
            elif t == "response_item" and p.get("type") == "function_call":
                ensure_assistant(line_no, raw_ts)
                turn_visible = True
                events.append(ToolUse(
                    message_id=cur_mid or "",
                    tool_use_id=_history_id(
                        p.get("call_id") or p.get("id"),
                        "tool-use", line_no, raw_ts),
                    tool=_hist_tool_name(p.get("name")),
                    input=_hist_tool_input(p.get("arguments")),
                ))
            elif t == "response_item" and p.get("type") == "function_call_output":
                turn_visible = True
                out, was_truncated = bounded_text(
                    p.get("output"), tool_result_max)
                truncated = True if was_truncated else None
                events.append(ToolResult(
                    tool_use_id=_history_id(
                        p.get("call_id"), "tool-result", line_no, raw_ts),
                    content=out,
                    is_error=_exit_is_error(out),
                    truncated=truncated,
                ))
            elif t == "event_msg" and payload_type == "agent_message":
                ensure_assistant(line_no, raw_ts, p.get("id") or p.get("message_id"))
                txt = p.get("message") or ""
                if txt:
                    turn_visible = True
                    turn_text_visible = True
                    events.append(Delta(message_id=cur_mid, text=txt))
            elif t == "event_msg" and payload_type == "task_complete":
                if turn_open:
                    last = p.get("last_agent_message")
                    if not turn_text_visible and isinstance(last, str) and last:
                        ensure_assistant(line_no, raw_ts)
                        events.append(Delta(message_id=cur_mid, text=last))
                        turn_visible = True
                        turn_text_visible = True
                    if turn_visible:
                        close_turn("success", _duration(p), False,
                                   _completed_ts(p, ts))
                    else:
                        events.append(Error(
                            code=ERR_CC_CRASH,
                            message=_EMPTY_COMPLETED_MESSAGE,
                            msg_id=active_msg_id,
                        ))
                        close_turn("error", _duration(p), True,
                                   _completed_ts(p, ts))
            elif t == "event_msg" and payload_type == "turn_aborted":
                if turn_open:
                    interrupted = p.get("reason") == "interrupted"
                    close_turn(
                        "error_during_execution" if interrupted else "error",
                        _duration(p), True, _completed_ts(p, ts))
            elif t == "event_msg" and payload_type in {
                    "task_failed", "turn_failed", "task_error"}:
                if turn_open:
                    close_turn("error", _duration(p), True, _completed_ts(p, ts))
            # session_meta / world_state / reasoning / token_count / task_* : skipped
            if ts is not None:
                last_ts = ts
    # A file can be read while Codex is still appending the current turn. Close
    # only its current text block; deliberately omit TurnEnd so the reducer keeps
    # the turn not-done instead of fabricating a completed status.
    close_assistant()
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
        return bounded_tool_input({"args": a}, 64 * 1024)
    out: dict = {}
    if a.get("cmd") is not None:
        out["command"] = a["cmd"]
    if a.get("workdir") is not None:
        out["cwd"] = a["workdir"]
    for k, v in a.items():
        if k not in ("cmd", "workdir", "yield_time_ms"):
            out[k] = v
    return bounded_tool_input(out, 64 * 1024)


def _exit_is_error(output: str) -> bool:
    m = re.search(r"exited with code (\d+)", output or "")
    return bool(m) and m.group(1) != "0"


def _data_uri_to_img(url) -> dict | None:
    """`data:image/png;base64,XXXX` -> {media_type, data} (the web's QueryImg shape)."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        head, data = url.split(",", 1)
        mt = head[5:].split(";")[0] or "image/png"
        return {"media_type": mt, "data": data}
    except Exception:
        return None
