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
import glob
import json
import os
import re
import signal
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from typing import Awaitable, Callable, Optional

from cc_remote.log import logger
from cc_remote.wrapper.codex_sessions import codex_model, codex_effort, codex_context_window
from cc_remote.wrapper.child_env import sanitized_child_env

log = logger("cc_remote.wrapper.codex_handle")

_REQ_TIMEOUT = 60.0
_APPROVAL_TIMEOUT = 5 * 60.0
_MAX_SERVER_REQUEST_TASKS = 32
_BIN_CACHE: Optional[str] = None
_MAX_CODEX_CANDIDATES = 16
_MAX_STANDALONE_CANDIDATES = 6
_MAX_NVM_CANDIDATES = 3
_CODEX_VERSION_TIMEOUT = 5

ApprovalCallback = Callable[[str, dict], Awaitable[str]]

_NEW_APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
})
_LEGACY_APPROVAL_METHODS = frozenset({
    "execCommandApproval",
    "applyPatchApproval",
})
_APPROVAL_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})
_LEGACY_DECISIONS = {
    "accept": "approved",
    "acceptForSession": "approved_for_session",
    "decline": "denied",
    "cancel": "abort",
}


def _codex_candidates() -> list[str]:
    """Every codex install we can find, in tie-break order (earlier wins ties).
    Managed standalone releases first: `codex upgrade` writes there, so it's the
    one the user actually updates. An npm-global under nvm is often stale but
    shadows everything else on PATH."""
    home = os.path.expanduser("~")
    out = list(islice(glob.iglob(
        os.path.join(home, ".codex/packages/standalone/releases/*/bin/codex")),
        _MAX_STANDALONE_CANDIDATES))
    out.append(os.path.join(home, ".local/bin/codex"))
    which = shutil.which("codex")
    if which:
        out.append(which)
    out += list(islice(glob.iglob(
        os.path.join(home, ".nvm/versions/node/*/bin/codex")),
        _MAX_NVM_CANDIDATES))
    out += ["/opt/homebrew/bin/codex", "/usr/local/bin/codex", "/usr/bin/codex"]
    seen, uniq = set(), []
    for c in out:
        if not os.path.exists(c):
            continue
        real = os.path.realpath(c)
        if real in seen:
            continue
        seen.add(real)
        uniq.append(c)
        if len(uniq) >= _MAX_CODEX_CANDIDATES:
            break
    return uniq


def _codex_version(path: str) -> tuple[int, ...]:
    """`codex --version` -> (0, 144, 1). (-1,) when it can't be run/parsed, so a
    broken install always loses to a working one."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           timeout=_CODEX_VERSION_TIMEOUT, env=_codex_env(path))
    except Exception:
        return (-1,)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
    return tuple(int(g) for g in m.groups()) if m else (-1,)


def _resolve_codex_bin() -> str:
    """Locate the codex CLI, preferring the NEWEST install.

    $CODEX_BIN short-circuits. Otherwise we version-probe every candidate and take
    the highest — plain PATH order is wrong: a stale npm-global (nvm bin sits first
    on the wrapper's PATH) shadowed a newer standalone release, so the wrapper kept
    spawning an old app-server whose `model/list` predated the current model family.
    The app-server IS our model catalog, so serving a stale one silently corrupts
    every model/effort decision downstream. Blocking (subprocess); cached for the
    process — call via asyncio.to_thread from async code."""
    global _BIN_CACHE
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    if _BIN_CACHE:
        return _BIN_CACHE
    cands = _codex_candidates()
    if not cands:
        return "codex"  # last resort — errors clearly if truly absent
    # A broken candidate must not stall startup serially.  Keep both the list and
    # the worker pool small; each individual probe also has a hard timeout.
    with ThreadPoolExecutor(max_workers=min(4, len(cands))) as pool:
        probed = list(pool.map(_codex_version, cands))
    versions = list(zip(probed, cands))
    best_v, best = max(versions, key=lambda p: p[0])
    if best_v == (-1,):
        best = cands[0]
    _BIN_CACHE = best
    log.info("codex bin resolved", path=best, version=".".join(map(str, best_v)),
             considered=[{"path": c, "version": ".".join(map(str, v))} for v, c in versions])
    return best


def _codex_env(bin_path: str) -> dict:
    """Child env for the codex subprocess. codex.js runs via `#!/usr/bin/env
    node`, so the child needs `node` on PATH. When codex was resolved from a
    dir that also ships node (nvm / npm-global bin), prepend that dir so the
    shebang resolves even if the wrapper's own PATH lacks it."""
    env = sanitized_child_env()
    bindir = os.path.dirname(os.path.abspath(bin_path)) if os.sep in bin_path else ""
    if bindir and os.path.exists(os.path.join(bindir, "node")):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


class CodexHandle:
    def __init__(self, cfg, cwd: Optional[str] = None,
                 approval_callback: Optional[ApprovalCallback] = None):
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
        # Human approval can take minutes.  It must not block the sole stdout
        # reader, which still has to consume turn/interrupt and other RPC replies.
        # Keep detached request handlers generation-owned and cancel them on
        # disconnect so an old approval cannot reply to a new app-server process.
        self._server_request_tasks: set[asyncio.Task] = set()
        # POSIX app-server processes get their own group so disconnecting a
        # session also terminates any shell/tool descendants it spawned.
        self._process_group: Optional[int] = None
        self._generation = 0
        self._dead = False
        self.approval_callback = approval_callback
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
        if self.proc is not None:
            await self.disconnect()
        self._cwd = cwd or self._cwd or getattr(self.cfg, "cc_cwd", None) or os.getcwd()
        # version-probes subprocesses on first call; keep it off the event loop.
        codex_bin = await asyncio.to_thread(_resolve_codex_bin)
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=_codex_env(codex_bin),
            # a single JSON-RPC line can exceed asyncio's default 64KB StreamReader
            # cap (e.g. an image echo or a big tool output) and crash readline —
            # raise it so the reader never dies mid-turn.
            limit=16 * 1024 * 1024,
            start_new_session=(os.name == "posix"),
        )
        self.proc = proc
        self._process_group = proc.pid if os.name == "posix" else None
        self._generation += 1
        generation = self._generation
        self._dead = False
        self._reader = asyncio.create_task(self._read_loop(proc, generation))
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc, generation))

        try:
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
            if not self.thread_id:
                raise RuntimeError("codex app-server did not return a thread id")
        except BaseException:
            await self.disconnect()
            raise
        log.info("codex connected", thread_id=self.thread_id, cwd=self._cwd,
                 resume=bool(resume_id), fork=fork)

    async def query(self, prompt, images=None) -> None:
        if self.thread_id and (
            self.proc is None or self._dead or self.proc.returncode is not None
        ):
            await self.force_reconnect(self.thread_id, self._cwd, reason="app-server unavailable")
        assert self.proc is not None and self.thread_id, "connect() first"
        self._turn_q = asyncio.Queue(
            maxsize=max(1, getattr(self.cfg, "turn_reader_queue_cap", 4)))
        params = {
            "threadId": self.thread_id,
            "input": _to_input(prompt, images),
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
        proc = self.proc
        process_group = self._process_group
        tasks = [t for t in (self._reader, self._stderr_task)
                 if t is not None and t is not asyncio.current_task()]
        server_tasks = [
            task for task in self._server_request_tasks
            if task is not asyncio.current_task()
        ]
        self._server_request_tasks.clear()
        self.proc = None
        self._process_group = None
        self._reader = None
        self._stderr_task = None
        self._generation += 1  # invalidate callbacks from the old process
        self._dead = True
        for t in tasks + server_tasks:
            t.cancel()
        if tasks or server_tasks:
            await asyncio.gather(*tasks, *server_tasks, return_exceptions=True)
        if proc is not None:
            def stop(sig: signal.Signals, *, force: bool = False) -> None:
                try:
                    if process_group is not None:
                        os.killpg(process_group, sig)
                    elif proc.returncode is None:
                        (proc.kill() if force else proc.terminate())
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    log.warning("codex process stop failed",
                                signal=sig.name, error=str(exc))

            if proc.returncode is None:
                stop(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    stop(getattr(signal, "SIGKILL", signal.SIGTERM), force=True)
                    await proc.wait()
            # The app-server parent can exit before a tool subprocess that ignored
            # SIGTERM. A final group kill guarantees no descendant survives session
            # eviction, /btw close, or reconnect.
            if process_group is not None:
                stop(signal.SIGKILL, force=True)
        if self._turn_q is not None:
            self._force_turn_sentinel(self._turn_q)
            self._turn_q = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("codex app-server disconnected"))
        self._pending.clear()
        self.turn_id = None

    async def force_reconnect(self, resume_id: Optional[str], cwd: Optional[str] = None,
                              reason: str = "reconnect") -> None:
        log.warning("codex force-reconnect", reason=reason)
        target = resume_id or self.thread_id
        await self.disconnect()
        await self.connect(resume_id=target, cwd=cwd or self._cwd)

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
        if mode not in ("untrusted", "on-request", "never"):
            raise ValueError(f"unsupported codex approval policy: {mode}")
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

    async def _respond_error(self, rid, code: int, message: str) -> None:
        await self._send({
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": code, "message": message},
        })

    async def _approval_decision(self, method: str, params: dict) -> str:
        """Return one current-schema approval decision, failing closed."""
        if self.approval == "never":
            return "decline"
        if self.approval not in {"on-request", "untrusted"}:
            log.warning("invalid codex approval policy; denying", approval=self.approval)
            return "decline"
        callback = self.approval_callback
        if callback is None:
            log.warning("codex approval requested without a client callback", method=method)
            return "decline"
        try:
            decision = await asyncio.wait_for(
                callback(method, params), timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("codex approval timed out; denying", method=method)
            return "decline"
        except Exception as exc:
            log.warning("codex approval callback failed; denying",
                        method=method, error=str(exc))
            return "decline"
        if decision not in _APPROVAL_DECISIONS:
            log.warning("invalid codex approval decision; denying",
                        method=method, decision=decision)
            return "decline"
        return decision

    async def _handle_server_request(self, message: dict) -> None:
        rid = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str):
            await self._respond_error(rid, -32600, "invalid server request")
            return
        if method not in _NEW_APPROVAL_METHODS | _LEGACY_APPROVAL_METHODS:
            log.warning("unsupported codex server request; rejecting", method=method)
            await self._respond_error(rid, -32601, f"unsupported server request: {method}")
            return
        if not isinstance(params, dict):
            await self._respond_error(rid, -32602, f"invalid params for {method}")
            return

        decision = await self._approval_decision(method, params)
        if method in _LEGACY_APPROVAL_METHODS:
            decision = _LEGACY_DECISIONS[decision]
        await self._respond(rid, {"decision": decision})

    def _server_request_done(self, task: asyncio.Task) -> None:
        """Observe a detached server-request task and keep the set bounded."""
        self._server_request_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.warning(
                "codex server request handler failed",
                error_type=type(error).__name__,
            )

    async def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_loop(self, proc: asyncio.subprocess.Process,
                         generation: int) -> None:
        assert proc.stdout
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if generation != self._generation:
                    return
                await self._dispatch(m)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("codex read loop ended", error=str(e))
        finally:
            # A stale reader belongs to an app-server generation already replaced
            # by disconnect/reconnect.  Leave the new generation alone, but do not
            # `return` from finally: that would suppress cancellation or an active
            # exception from this reader task.
            if generation == self._generation:
                self._dead = True
                # The process can no longer receive approval responses.  Release
                # any AskUser callbacks tied to this generation immediately.
                for task in list(self._server_request_tasks):
                    task.cancel()
                # unblock any waiting turn/request
                if self._turn_q is not None:
                    self._force_turn_sentinel(self._turn_q)
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
            if len(self._server_request_tasks) >= _MAX_SERVER_REQUEST_TASKS:
                method = m.get("method")
                rid = m.get("id")
                log.warning("codex server request cap reached; rejecting",
                            method=method)
                if method in _NEW_APPROVAL_METHODS:
                    await self._respond(rid, {"decision": "decline"})
                elif method in _LEGACY_APPROVAL_METHODS:
                    await self._respond(rid, {"decision": "denied"})
                else:
                    await self._respond_error(
                        rid, -32000, "too many pending server requests")
                return
            task = asyncio.create_task(self._handle_server_request(m))
            self._server_request_tasks.add(task)
            task.add_done_callback(self._server_request_done)
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
            queue = self._turn_q
            await queue.put(m)
            if method == "turn/completed":
                await queue.put(None)

    @staticmethod
    def _force_turn_sentinel(queue: asyncio.Queue) -> None:
        """Wake a consumer during disconnect even when the bounded queue is full."""
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _drain_stderr(self, proc: asyncio.subprocess.Process,
                            generation: int) -> None:
        try:
            while generation == self._generation and proc.stderr:
                line = await proc.stderr.readline()
                if not line:
                    break
                # Stderr can contain one frame-sized line. Its content is already
                # intentionally suppressed by the formatter, so avoid a redundant
                # multi-megabyte decode/copy just to redact it again.
                log.debug("codex stderr", bytes=len(line))
        except asyncio.CancelledError:
            pass


def _to_input(prompt, images=None) -> list:
    """Build codex turn input: a text item + one localImage item per attached
    image path (verified codex reads localImage). `images` is a list of /tmp paths."""
    out = [{"type": "text", "text": prompt if isinstance(prompt, str) else str(prompt)}]
    for path in (images or []):
        out.append({"type": "localImage", "path": path})
    return out


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
