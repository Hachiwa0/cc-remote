"""Codex app-server lifecycle: connect / query / interrupt / receive / disconnect.

The Codex analog of SdkHandle (sdk.py). Drives one persistent `codex app-server`
subprocess over newline-delimited JSON-RPC 2.0 (stdio) and presents the SAME
async surface the machine's per-turn consumer expects:

  connect(resume_id, cwd) -> initialize/initialized handshake + thread/start|resume
  query(prompt)           -> turn/start (opens a fresh per-turn queue)
  receive_response()      -> async-gen of raw notification dicts until turn/completed
  interrupt()             -> turn/interrupt {threadId, turnId}
  disconnect()            -> terminate the subprocess

Model-agnostic: whatever backend Codex is pointed at (user's cc-switch) is Codex's
concern. We never set a model/provider here.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from cc_remote.log import logger
from cc_remote.wrapper.codex_sessions import codex_model, codex_effort, codex_context_window

log = logger("cc_remote.wrapper.codex_handle")

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
_REQ_TIMEOUT = 60.0


class CodexHandle:
    def __init__(self, cfg, cwd: Optional[str] = None):
        self.cfg = cfg
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self._cwd = cwd
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._turn_q: Optional[asyncio.Queue] = None
        self._reader: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self.last_token_usage: Optional[dict] = None
        self.context_window: Optional[int] = None
        # per-session codex settings, applied on thread/start + turn/start — the
        # Codex equivalents of cc's model / effort / permission-mode. Defaults come
        # from ~/.codex/config.toml; the client overrides them via set_* .
        self.model: Optional[str] = codex_model()
        self.effort: Optional[str] = codex_effort()         # low | medium | high | xhigh
        self.applied_effort = self.effort                   # keep machine's spawn-time check a no-op
        self.approval: str = "never"                        # untrusted | on-request | never
        self.service_tier: Optional[str] = None             # "fast" = Codex Fast mode; None = default
        self.tier_dirty: bool = False                       # service_tier changed -> reconnect next turn to reload config

    async def connect(self, resume_id: Optional[str] = None, cwd: Optional[str] = None,
                      fork: bool = False) -> None:
        self._cwd = cwd or self._cwd or getattr(self.cfg, "cc_cwd", None) or os.getcwd()
        self.proc = await asyncio.create_subprocess_exec(
            CODEX_BIN, "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=os.environ.copy(),
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        await self._request("initialize", {"clientInfo": {"name": "cc-remote", "version": "0.1.0"}})
        await self._notify("initialized")

        if fork and resume_id:
            # ephemeral /btw fork: inherits resume_id's context into a throwaway
            # thread; the parent thread is never touched (verified: fork answers
            # from parent context, parent stays coherent).
            res = await self._request("thread/fork", {
                "threadId": resume_id, "ephemeral": True,
                "cwd": self._cwd, "approvalPolicy": self.approval})
            self.thread_id = _thread_id_of(res)
        elif resume_id:
            res = await self._request("thread/resume", {
                "threadId": resume_id, "cwd": self._cwd, "approvalPolicy": self.approval})
            self.thread_id = _thread_id_of(res) or resume_id
        else:
            res = await self._request("thread/start", {
                "cwd": self._cwd, "approvalPolicy": self.approval})
            self.thread_id = _thread_id_of(res)
        log.info("codex connected", thread_id=self.thread_id, cwd=self._cwd,
                 resume=bool(resume_id), fork=fork)

    async def query(self, prompt) -> None:
        assert self.proc is not None and self.thread_id, "connect() first"
        self._turn_q = asyncio.Queue()
        params = {
            "threadId": self.thread_id,
            "input": _to_input(prompt),
            "approvalPolicy": self.approval,
        }
        if self.model:
            params["model"] = self.model
        if self.effort:
            params["effort"] = self.effort
        if self.service_tier:
            params["serviceTier"] = self.service_tier   # codex Fast mode
        res = await self._request("turn/start", params)
        turn = (res or {}).get("turn") or {}
        self.turn_id = turn.get("id")

    async def receive_response(self):
        """Async-gen of this turn's raw notification dicts, ending at turn/completed."""
        q = self._turn_q
        if q is None:
            return
        try:
            while True:
                msg = await q.get()
                if msg is None:      # sentinel pushed by the reader on turn/completed
                    break
                yield msg
        finally:
            if self._turn_q is q:
                self._turn_q = None

    async def interrupt(self) -> None:
        if self.proc and self.thread_id and self.turn_id:
            try:
                await self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
            except Exception as e:
                log.warning("codex interrupt failed", error=str(e))

    async def disconnect(self) -> None:
        for t in (self._reader, self._stderr_task):
            if t:
                t.cancel()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            except Exception as e:
                log.warning("codex terminate failed", error=str(e))
            self.proc = None

    async def force_reconnect(self, resume_id: Optional[str], cwd: Optional[str] = None,
                              reason: str = "reconnect") -> None:
        log.warning("codex force-reconnect", reason=reason)
        await self.disconnect()
        await self.connect(resume_id=resume_id or self.thread_id, cwd=cwd or self._cwd)

    # --- live controls (applied on the NEXT turn via turn/start overrides) ---
    async def set_model(self, model: str) -> None:
        self.model = model
        log.info("codex model set (applies next turn)", model=model)

    async def set_service_tier(self, tier: Optional[str]) -> None:
        # "" / "default" -> None (off); "fast" -> Codex Fast mode. Applies next turn.
        self.service_tier = tier if tier and tier != "default" else None
        log.info("codex service tier set (applies next turn)", service_tier=self.service_tier)

    async def set_permission_mode(self, mode: str) -> None:
        # Codex "mode" = approval policy (untrusted | on-request | never).
        if mode in ("untrusted", "on-request", "never"):
            self.approval = mode
            log.info("codex approval set (applies next turn)", approval=mode)

    async def get_context_usage(self) -> dict:
        # Real shape (verified, gpt-5.5): tokenUsage = {last:{totalTokens,…},
        # total:{totalTokens,…}, modelContextWindow}. `last.totalTokens` is the most
        # recent turn's full token count ≈ current context depth (what the codex TUI
        # gauges); `total` is the cumulative session sum (over-counts context). Use
        # `last` for the "context full?" reading, falling back to `total`.
        u = self.last_token_usage if isinstance(self.last_token_usage, dict) else {}
        last = u.get("last") if isinstance(u.get("last"), dict) else {}
        total = u.get("total") if isinstance(u.get("total"), dict) else {}
        used = last.get("totalTokens")
        if used is None:
            used = total.get("totalTokens")
        # server value (captured in _dispatch) wins; else the config-declared window.
        win = self.context_window or u.get("modelContextWindow") or codex_context_window()
        return {"used_tokens": used, "context_window": win, "raw": u}

    # ---- internals ----
    async def _request(self, method: str, params: Optional[dict] = None):
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        obj = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            obj["params"] = params
        await self._send(obj)
        try:
            return await asyncio.wait_for(fut, timeout=_REQ_TIMEOUT)
        finally:
            self._pending.pop(rid, None)

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        obj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            obj["params"] = params
        await self._send(obj)

    async def _respond(self, rid, result) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                await self._dispatch(m)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("codex read loop ended", error=str(e))
        finally:
            # unblock any waiting turn/request
            if self._turn_q is not None:
                self._turn_q.put_nowait(None)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("codex app-server closed"))

    async def _dispatch(self, m: dict) -> None:
        has_id = "id" in m
        has_method = "method" in m
        if has_id and not has_method:                       # response to our request
            fut = self._pending.get(m["id"])
            if fut and not fut.done():
                if "error" in m:
                    fut.set_exception(RuntimeError(str(m["error"])))
                else:
                    fut.set_result(m.get("result"))
            return
        if has_id and has_method:                            # server -> client request
            # unattended: approve. approvalPolicy:"never" usually avoids these.
            await self._respond(m["id"], {"decision": "approved"})
            return
        # notification
        method = m.get("method")
        if method == "thread/started" and not self.thread_id:
            self.thread_id = _thread_id_of_notif(m)
        elif method == "thread/tokenUsage/updated":
            tu = (m.get("params") or {}).get("tokenUsage")
            if isinstance(tu, dict):
                self.last_token_usage = tu
                # modelContextWindow is the SERVER-authoritative window (e.g. 258400);
                # keep the last non-null value so get_context_usage always has it.
                mcw = tu.get("modelContextWindow")
                if mcw:
                    self.context_window = mcw
        if self._turn_q is not None:
            self._turn_q.put_nowait(m)
            if method == "turn/completed":
                self._turn_q.put_nowait(None)

    async def _drain_stderr(self) -> None:
        try:
            while self.proc and self.proc.stderr:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                log.debug("codex stderr", line=line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            pass


def _to_input(prompt) -> list:
    """cc-remote sends a str prompt; multimodal (images) can extend this later."""
    if isinstance(prompt, str):
        return [{"type": "text", "text": prompt}]
    return [{"type": "text", "text": str(prompt)}]


def _thread_id_of(res) -> Optional[str]:
    if isinstance(res, dict):
        th = res.get("thread")
        if isinstance(th, dict):
            return th.get("id") or th.get("sessionId")
        return res.get("threadId") or res.get("thread_id")
    return None


def _thread_id_of_notif(m: dict) -> Optional[str]:
    th = (m.get("params") or {}).get("thread")
    if isinstance(th, dict):
        return th.get("id") or th.get("sessionId")
    return None
