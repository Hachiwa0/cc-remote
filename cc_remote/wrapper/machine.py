"""The wrapper brain: a pool of per-session contexts + per-turn consumers.

Each SessionContext owns one cc subprocess (SdkHandle) plus its conversation
state (ring buffer, seq, state machine, turn task, translator, pending asks,
emit lock). The machine holds a pool `dict[key, SessionContext]` and a
`focused_sid`; relay/transport are singletons multiplexed by the `sid` field.

Per ctx, the drain contract is unchanged from the single-session design: one
async turn task per query; its consumer always runs to the terminal
ResultMessage (normal `success` or interrupted `error_during_execution`) before
that ctx's state returns to idle. interrupt() sets state=interrupting and the
SAME consumer keeps iterating until the terminal ResultMessage (the drain). A
drain timeout force-reconnects that ctx's SDK.

Reader/queue split: a background task iterates the SDK's async generator
WITHOUT asyncio.wait_for — wrapping __anext__ in wait_for corrupts the generator
when the short poll times out. The turn reads from an asyncio.Queue instead,
and cancelling queue.get (for the drain timeout) is safe and corrupts nothing.

Multi-session model:
- Routing identity is `ctx.key` — the real sid once known, else a temp
  `tmp-<uuid>` for a brand-new session. It equals the pool dict key and is what
  every emit stamps as `sid`, so a pre-capture new session's frames route to the
  right client runtime instead of leaking into whatever is focused.
- Switching the viewed session is FOCUS ONLY (SessionFocus) — no disconnect, so
  the previously-viewed session's turn keeps streaming in the background.
- When a new session captures its real cc id mid-turn, that is a re-key
  (SessionRekey: rename tmp-key -> sid), NOT a focus change — else a background
  session's capture would steal the user's view.
- Concurrency cap `max_concurrent_sessions`: over the cap, evict an idle,
  non-focused session (tear down its subprocess; the client keeps its runtime
  and re-spawns on re-focus). Reject only if ALL resident sessions are running.
- Token-aware: focus switching never reconnects a resident session; resume
  (cold prompt cache = full context re-send) happens ONLY on first spawn or on
  re-focus-after-eviction. Raising the cap trades RAM for fewer cold re-sends —
  don't evict sessions you're actively bouncing between.
"""
from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4
from typing import Optional

from claude_agent_sdk import list_sessions, get_session_messages, rename_session, tag_session, get_session_info, delete_session
from claude_agent_sdk.types import ResultMessage

from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    Error, Hello, Model, Effort, Fast, Perm, BtwOpened, ContextReport, DiffReport, History, AskUser, Pong, Snapshot, StateEvent, State, UserMsg, is_downstream,
    SessionInfo, SessionList, SessionFocus, SessionRekey, DirList,
    ERR_BUSY, ERR_NOT_RUNNING, ERR_BAD_PROMPT, ERR_DRAIN_TIMEOUT,
    ERR_CC_CRASH, ERR_INTERNAL,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.ask import make_ask_server
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.session import load_session_id, save_session_id
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.wrapper.stream import (
    StreamTranslator, extract_session_id, extract_model,
    translate_history, last_assistant_model, transcript_timestamps,
)
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator, codex_session_id, is_turn_terminal, codex_translate_history,
)
from cc_remote.wrapper.codex_sessions import (
    list_codex_sessions, codex_session_cwd, codex_rollout_path, codex_model,
    set_codex_config_fast, codex_fast_enabled,
)
from cc_remote.wrapper.transport import WrapperTransport

log = logger("cc_remote.wrapper.machine")


class WrapperMachine:
    def __init__(self, cfg: WrapperConfig, transport: WrapperTransport):
        self.cfg = cfg
        self.transport = transport
        # Pool of resident sessions, keyed by real session_id (or a `tmp-<uuid>`
        # temp key for a brand-new session until its id is captured).
        self.sessions: dict[str, SessionContext] = {}
        self.focused_sid: Optional[str] = None  # pool key of the viewed session
        self.transport.on_connected = self._on_transport_connected

    # ---- pool helpers ----

    def _focused_ctx(self) -> Optional[SessionContext]:
        return self.sessions.get(self.focused_sid) if self.focused_sid else None

    def _ctx_for(self, sid: Optional[str]) -> Optional[SessionContext]:
        """Resolve a command's target ctx. An EXPLICIT sid that isn't resident
        returns None — NOT the focused ctx: a session whose spawn failed (e.g. bad
        codex config) must not silently reroute its query to whatever is focused
        (that made a failed session look like "no response" while its "在？" landed
        on another session). Only an absent sid (legacy/untagged commands) falls
        back to the focused view. Iterates a snapshot (a turn may re-key mid-scan)."""
        if sid:
            ctx = self.sessions.get(sid)
            if ctx:
                return ctx
            for c in list(self.sessions.values()):
                if c.session_id == sid:
                    return c
            return None
        return self._focused_ctx()

    # ---- lifecycle ----

    async def run(self) -> None:
        self._cleanup_tmp()
        bootstrap_sid = (
            self.cfg.resume_session_id or load_session_id(self.cfg.state_dir, self.cfg.cc_cwd)
        )
        ctx = await self._spawn(resume_id=bootstrap_sid, cwd=self.cfg.cc_cwd, bootstrap=True)
        if ctx is None:
            log.error("bootstrap spawn failed")
            raise RuntimeError("bootstrap session failed to start")
        self.focused_sid = ctx.key
        log.info("wrapper running", session_id=ctx.session_id, key=self.focused_sid)
        await self.transport.start()
        try:
            async for cmd in self.transport.incoming():
                try:
                    await self._handle(cmd)
                except Exception:
                    log.exception("command handling failed", type=cmd.type)
        finally:
            await self.transport.stop()
            for c in list(self.sessions.values()):
                try:
                    await c.sdk.disconnect()
                except Exception:
                    pass

    async def _on_transport_connected(self) -> None:
        ctx = self._focused_ctx()
        await self.transport.send(Hello(
            role="wrapper",
            cc_session_id=ctx.session_id if ctx else None,
            state=(ctx.state if ctx else "idle"),
            buffer_head_seq=(ctx.buffer.head_seq if ctx else 0),
            buffer_tail_seq=(ctx.buffer.tail_seq if ctx else 0),
        ))

    # ---- emit (per-ctx seq + buffer + best-effort send), serialized per ctx ----

    async def _emit_locked(self, ctx: SessionContext, msg) -> None:
        if is_downstream(msg):
            msg.seq = ctx.next_seq()
            ctx.buffer.append(msg)
        # Route by the pool key (real sid once known, else the temp key) so a
        # pre-capture new session's frames land in the right client runtime
        # instead of leaking into whatever is currently focused.
        msg.sid = ctx.session_id or ctx.key
        await self.transport.send(msg)

    async def _emit(self, ctx: SessionContext, msg) -> None:
        async with ctx.emit_lock:
            await self._emit_locked(ctx, msg)

    async def _emit_focused(self, msg) -> None:
        """For control-path errors with no target ctx (cap reached, bad cwd):
        route to the focused ctx if any, else send unbuffered."""
        ctx = self._focused_ctx()
        if ctx is not None:
            await self._emit(ctx, msg)
        else:
            msg.sid = self.focused_sid
            await self.transport.send(msg)

    async def _emit_to_sid(self, sid: Optional[str], msg) -> None:
        """Route a control-path frame to a SPECIFIC session's view (tagged by sid),
        even when it has no ctx — so a failed-to-spawn session's error surfaces on
        that session, not on whatever happens to be focused."""
        ctx = self.sessions.get(sid) if sid else None
        if ctx is not None:
            await self._emit(ctx, msg)
        else:
            msg.sid = sid or self.focused_sid
            await self.transport.send(msg)

    async def _set_state(self, ctx: SessionContext, state: State) -> None:
        ctx.state = state
        await self._emit(ctx, StateEvent(state=state))
        log.info("state transition", sid=ctx.session_id, state=state)

    # ---- command dispatch ----

    async def _handle(self, cmd) -> None:
        t = cmd.type
        if t == "hello" and cmd.role == "client":
            await self._handle_client_hello(cmd)
        elif t == "query":
            await self._handle_query(cmd)
        elif t == "interrupt":
            await self._handle_interrupt(cmd)
        elif t == "set_model":
            await self._handle_set_model(cmd)
        elif t == "set_effort":
            await self._handle_set_effort(cmd)
        elif t == "set_service_tier":
            await self._handle_set_service_tier(cmd)
        elif t == "open_btw":
            await self._handle_open_btw(cmd)
        elif t == "close_btw":
            await self._handle_close_btw(cmd)
        elif t == "set_perm":
            await self._handle_set_perm(cmd)
        elif t == "get_context":
            await self._handle_get_context(cmd)
        elif t == "get_diff":
            await self._handle_get_diff(cmd)
        elif t == "get_history":
            await self._handle_get_history(cmd)
        elif t == "answer_question":
            await self._handle_answer_question(cmd)
        elif t == "list_sessions":
            await self._handle_list_sessions(cmd)
        elif t == "switch_session":
            await self._handle_switch_session(cmd)
        elif t == "new_session":
            await self._handle_new_session(cmd)
        elif t == "list_dir":
            await self._handle_list_dir(cmd)
        elif t == "rename_session":
            await self._handle_rename_session(cmd)
        elif t == "archive_session":
            await self._handle_archive_session(cmd)
        elif t == "ping":
            await self._emit_focused(Pong(n=cmd.n))
        else:
            log.warning("unexpected command", type=t, role=getattr(cmd, "role", None))

    async def _handle_client_hello(self, cmd) -> None:
        # Send ONE lightweight Snapshot per resident session so the client builds a
        # runtime + sidebar status dot for each. History is NO LONGER replayed here
        # — the client fetches it on demand via GetHistory (one bulk frame read from
        # the transcript, like a web chat's GET /conversation). Dropping the
        # per-session full-buffer replay is what kills the multi-thousand-frame
        # reconnect flood that made refreshes slow.
        # snapshot: a concurrent turn may re-key self.sessions across the awaits below
        for key, ctx in list(self.sessions.items()):
            if ctx.btw:
                continue  # /btw forks are ephemeral + per-client; not restored on hello
            sid = ctx.session_id or key
            tail = ctx.buffer.latest_tail_text()
            st = ctx.buffer.latest_state() or ctx.state
            snap = Snapshot(cc_session_id=ctx.session_id, state=st, tail_text=tail, cwd=ctx.cwd)
            async with ctx.emit_lock:
                await self.transport.send(snap.model_copy(update={"to": cmd.client_id, "sid": sid}))
        log.info("client hello handled", client_id=cmd.client_id,
                 sessions=len(self.sessions))

    async def _handle_get_history(self, cmd) -> None:
        """Read a session's history ON-DEMAND from its transcript and return it as
        ONE bulk History frame, routed to the requesting client. No spawn, no ring
        buffer — like a web chat's GET /conversation. The transcript parse runs in
        a thread so it never blocks the event loop or other sessions. Events are
        the SAME classes as the live stream, so the client dedups history vs. the
        live tail by msg_id/message_id."""
        sid = cmd.session_id
        # Reading requires the session's own cwd (transcript lives under it).
        # Prefer a resident ctx's cwd, else the client-provided cwd, else default.
        ctx = self.sessions.get(sid) or next(
            (c for c in self.sessions.values() if c.session_id == sid), None)
        before = getattr(cmd, "before", None)   # page strictly older than this turn id
        limit = getattr(cmd, "limit", None)     # max turns to return (newest-most)
        events: list = []
        mdl = None
        is_codex_hist = ctx is not None and ctx.engine == "codex"
        if is_codex_hist:
            # Codex history lives in ~/.codex/sessions rollout files, not the
            # Claude transcript store.
            try:
                path = await asyncio.to_thread(codex_rollout_path, sid)
                if path:
                    events, mdl = await asyncio.to_thread(
                        codex_translate_history, path, self.cfg.tool_result_max)
            except Exception as e:
                log.warning("codex get_history failed", session_id=sid, error=str(e))
        else:
            directory = (ctx.cwd if ctx else None) or getattr(cmd, "cwd", None) or self.cfg.cc_cwd
            try:
                def _read():
                    return (get_session_messages(sid, directory=directory),
                            transcript_timestamps(sid))
                msgs, tss = await asyncio.to_thread(_read)
                events = translate_history(msgs, self.cfg.tool_result_max, timestamps=tss)
                mdl = last_assistant_model(msgs)
            except Exception as e:
                log.warning("get_history failed", session_id=sid, error=str(e))
        for ev in events:
            ev.sid = sid
        # Group events into turns at each user_msg boundary (a turn = one user
        # message + the assistant's reply); leading non-user events form group 0.
        turns: list[list] = []
        for ev in events:
            if getattr(ev, "type", None) == "user_msg" or not turns:
                turns.append([])
            turns[-1].append(ev)

        def _tid(grp):
            return next((e.msg_id for e in grp if getattr(e, "type", None) == "user_msg"), None)

        # Select the page of turns: newest `limit` turns, ending before `before`.
        end = len(turns)
        if before is not None:
            idx = next((i for i, g in enumerate(turns) if _tid(g) == before), None)
            if idx is not None:
                end = idx
        start = max(0, end - limit) if isinstance(limit, int) and limit > 0 else 0
        page = turns[start:end]
        has_more = start > 0

        payload: list[dict] = []
        # Prepend the model readout only on the newest page (initial load). For cc,
        # only announce Claude-branded models: the user's cc-switch may proxy a
        # Claude alias (e.g. claude-mythos-5) to a different upstream that the
        # transcript records under its raw name (e.g. glm-5.2) — surfacing that raw
        # name would wrongly replace the alias in the model chip. Codex announces
        # its real model (gpt-*).
        if before is None and mdl and (is_codex_hist or mdl.startswith("claude-")):
            m = Model(model=mdl); m.sid = sid
            payload.append(m.model_dump(mode="json"))
        for grp in page:
            for ev in grp:
                payload.append(ev.model_dump(mode="json"))
        hist = History(
            session_id=sid, events=payload, has_more=has_more, before=before,
            oldest_id=(_tid(page[0]) if page else None),
            newest_id=(_tid(page[-1]) if page else None),
        )
        client_id = getattr(cmd, "client_id", None)
        if client_id:
            hist.to = client_id
            await self.transport.send(hist)
        else:
            await self._emit_focused(hist)  # fallback: broadcast (like other one-shots)
        log.info("history sent", session_id=sid, turns=len(page), events=len(payload),
                 has_more=has_more, before=bool(before), client_id=client_id)

    async def _handle_query(self, cmd) -> None:
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            # sid given but not resident (spawn failed / evicted). Tag the error to
            # THAT session so the user sees it there — and never reroute the prompt
            # to a different session.
            await self._emit_to_sid(sid, Error(code=ERR_NOT_RUNNING,
                message="该会话未启动(可能启动失败),重新点进这个会话再发"))
            return
        if ctx.state != "idle":
            await self._emit(ctx, Error(code=ERR_BUSY, message="该会话正忙,先 interrupt"))
            return
        if not cmd.prompt and not cmd.images and not cmd.files:
            await self._emit(ctx, Error(code=ERR_BAD_PROMPT, message="empty prompt"))
            return
        # claim synchronously so a concurrent query on THIS ctx can't race in
        ctx.state = "running"
        async with ctx.emit_lock:
            await self._emit_locked(ctx, UserMsg(msg_id=cmd.msg_id, prompt=cmd.prompt, images=getattr(cmd, "images", None)))
            await self._emit_locked(ctx, StateEvent(state="running"))
        ctx.turn_task = asyncio.create_task(
            self._run_turn(ctx, cmd.prompt, getattr(cmd, "images", None), getattr(cmd, "files", None)))

    async def _handle_interrupt(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            await self._emit_focused(Error(code=ERR_NOT_RUNNING, message="no active session"))
            return
        if ctx.state != "running":
            await self._emit(ctx, Error(code=ERR_NOT_RUNNING, message="该会话没有正在运行的回合"))
            return
        ctx.state = "interrupting"
        await self._emit(ctx, StateEvent(state="interrupting"))
        try:
            await ctx.sdk.interrupt()
        except Exception as e:
            log.exception("interrupt call failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"interrupt failed: {e}"))
            # leave interrupting; the drain timeout will recover

    async def _handle_set_model(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        try:
            await ctx.sdk.set_model(cmd.model)
            ctx.announced_model = cmd.model
            await self._emit(ctx, Model(model=cmd.model))
        except Exception as e:
            log.exception("set_model failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_model failed: {e}"))

    async def _handle_set_effort(self, cmd) -> None:
        # effort (--effort) is spawn-time, so unlike set_model we can't flip it on
        # the live subprocess. Just record the desired level and announce it; the
        # respawn-with-resume happens lazily at the next turn (see _run_turn), so an
        # idle effort tweak costs no tokens until the user actually sends.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        ctx.sdk.effort = cmd.effort
        ctx.announced_effort = cmd.effort
        await self._emit(ctx, Effort(effort=cmd.effort))
        log.info("effort set (applies on next turn via reconnect)", sid=ctx.session_id, effort=cmd.effort)

    async def _handle_set_service_tier(self, cmd) -> None:
        # Codex Fast mode. codex reads `service_tier` from ~/.codex/config.toml at
        # app-server startup, so a simple per-turn param can't turn it OFF when the
        # config still says "fast" (it just falls back to config). So we: (1) edit
        # config.toml — what the user validates against — (2) set the per-turn
        # override on the handle (belt+suspenders), and (3) mark the session for a
        # reconnect so the next turn respawns the app-server against the new config
        # (lazy, like an effort change: no cost until the user actually sends).
        # cc has no service tier — ignore there.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None or ctx.engine != "codex":
            return
        # "toggle" flips whatever config.toml currently says (source of truth the
        # user sees/edits), so the web needs no synced on/off state; "fast"/other
        # still set an explicit state.
        if cmd.service_tier == "toggle":
            on = not codex_fast_enabled()
        else:
            on = (cmd.service_tier == "fast")
        try:
            ok = await asyncio.to_thread(set_codex_config_fast, on)
            await ctx.sdk.set_service_tier("fast" if on else None)
            ctx.sdk.tier_dirty = True   # force a config-reloading reconnect next turn
            await self._emit(ctx, Fast(on=on))   # tell the client fast vs standard
            log.info("codex service tier set", sid=ctx.session_id, fast=on, config_written=ok)
        except Exception as e:
            log.exception("set_service_tier failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_service_tier failed: {e}"))

    async def _handle_open_btw(self, cmd) -> None:
        parent = self._ctx_for(getattr(cmd, "sid", None))
        if parent is None:
            await self._emit_focused(Error(code=ERR_NOT_RUNNING, message="没有可 fork 的会话"))
            return
        if parent.btw:  # never fork a fork — fork its parent instead
            parent = self.sessions.get(parent.parent_sid) or next(
                (c for c in self.sessions.values() if c.session_id == parent.parent_sid), parent)
        btw = await self._spawn_btw(parent)
        if btw is None:
            return  # error already emitted
        ev = BtwOpened(btw_sid=btw.key, parent_sid=parent.session_id or parent.key, engine=btw.engine)
        ev.sid = btw.key
        cid = getattr(cmd, "client_id", None)
        if cid:
            ev.to = cid
        await self.transport.send(ev)
        # a fresh Snapshot so the requester builds a runtime for the fork's key.
        snap = Snapshot(cc_session_id=None, state="idle", tail_text="", cwd=btw.cwd)
        snap.sid = btw.key
        if cid:
            snap.to = cid
        await self.transport.send(snap)
        log.info("btw opened", btw_sid=btw.key, parent=parent.session_id, client_id=cid)

    async def _handle_close_btw(self, cmd) -> None:
        sid = getattr(cmd, "sid", None)
        ctx = self.sessions.get(sid) if sid else None
        if ctx is None or not ctx.btw:
            return
        self.sessions.pop(ctx.key, None)
        try:
            if ctx.turn_task and not ctx.turn_task.done():
                ctx.turn_task.cancel()
            await ctx.sdk.disconnect()
        except Exception as e:
            log.warning("btw close disconnect failed", error=str(e))
        # codex forks are ephemeral (no rollout); cc fork_session persists a
        # transcript under btw_real_id — hard-delete it so it never clutters the
        # session list. Best-effort.
        if ctx.engine != "codex" and ctx.btw_real_id:
            try:
                await asyncio.to_thread(delete_session, ctx.btw_real_id, directory=ctx.cwd)
                log.info("btw fork transcript deleted", forked=ctx.btw_real_id)
            except Exception as e:
                log.warning("btw fork transcript delete failed", forked=ctx.btw_real_id, error=str(e))
        log.info("btw closed", btw_sid=sid)

    async def _handle_set_perm(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        try:
            await ctx.sdk.set_permission_mode(cmd.mode)
            ctx.announced_perm = cmd.mode
            await self._emit(ctx, Perm(mode=cmd.mode))
        except Exception as e:
            log.exception("set_permission_mode failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_perm failed: {e}"))

    async def _on_set_mode(self, ctx: SessionContext, mode: str) -> None:
        """Agent-facing set_mode MCP tool (called within a turn). Same effect as
        SetPerm: sdk.set_permission_mode + Perm broadcast on this ctx."""
        try:
            await ctx.sdk.set_permission_mode(mode)
            ctx.announced_perm = mode
            await self._emit(ctx, Perm(mode=mode))
            log.info("agent set permission mode", sid=ctx.session_id, mode=mode)
        except Exception as e:
            log.exception("agent set_mode failed", error=str(e))
            raise

    async def _handle_get_context(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        try:
            usage = await ctx.sdk.get_context_usage()
            if ctx.engine == "codex":
                used = usage.get("used_tokens") or 0
                win = usage.get("context_window") or 0
                await self._emit(ctx, ContextReport(
                    total_tokens=used, max_tokens=win,
                    percentage=(used / win * 100.0) if win else 0.0,
                    model=ctx.sdk.model, is_auto_compact_enabled=None, categories=[]))
                return
            await self._emit(ctx, ContextReport(
                total_tokens=usage.get("totalTokens", 0),
                max_tokens=usage.get("maxTokens", 0),
                percentage=usage.get("percentage", 0.0),
                model=usage.get("model"),
                is_auto_compact_enabled=usage.get("isAutoCompactEnabled"),
                categories=usage.get("categories", []) or [],
            ))
        except Exception as e:
            log.exception("get_context_usage failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"get_context failed: {e}"))

    async def _handle_get_diff(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None)) or self._focused_ctx()
        if ctx is None:
            return
        try:
            diff = await self._git_diff(ctx.cwd, cmd.file)
            await self._emit(ctx, DiffReport(file=cmd.file, diff=diff))
        except Exception as e:
            log.exception("get_diff failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"get_diff failed: {e}"))

    # ---- ask_user MCP tool (agent asks the user a multiple-choice question) ----

    async def _on_ask(self, ctx: SessionContext, question: str, options: list[dict[str, str]]) -> str:
        """Called by THIS ctx's in-process MCP server when the agent invokes
        `ask_user`. Emits AskUser on the ctx and blocks until AnswerQuestion.
        Runs in the ctx's reader task while its turn loop is blocked on
        receive_response(); other ctxs' turns are unaffected."""
        ask_id = f"ask-{ctx.next_seq()}"
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        ctx.pending_asks[ask_id] = fut
        await self._emit(ctx, AskUser(ask_id=ask_id, question=question, options=options))
        log.info("ask_user emitted", sid=ctx.session_id, ask_id=ask_id, options=len(options))
        try:
            return await asyncio.wait_for(fut, timeout=30 * 60)
        except asyncio.TimeoutError:
            log.warning("ask_user timed out", ask_id=ask_id)
            return "(用户未回答，已超时)"
        finally:
            ctx.pending_asks.pop(ask_id, None)

    async def _handle_answer_question(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        fut = ctx.pending_asks.get(cmd.ask_id)
        if fut is None:
            log.warning("answer for unknown ask_id", ask_id=cmd.ask_id)
            return
        if not fut.done():
            fut.set_result(cmd.answer)
            log.info("ask_user answered", ask_id=cmd.ask_id)
        else:
            log.warning("answer for already-done ask_id", ask_id=cmd.ask_id)

    async def _git_diff(self, cwd: str, file: str) -> str:
        """Raw `git diff` (vs HEAD) text for a cwd. Empty file => all files; a
        single untracked file falls back to --no-index (full-add diff)."""
        if not file:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "diff", "HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate()
            return out.decode(errors="replace") if out else ""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "diff", "HEAD", "--", file,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        diff = out.decode(errors="replace") if out else ""
        if diff.strip():
            return diff
        proc2 = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, "diff", "--no-index", "/dev/null", file,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out2, _ = await proc2.communicate()
        return out2.decode(errors="replace") if out2 else ""

    # ---- sessions (list / switch / new) ----

    async def _handle_list_sessions(self, cmd) -> None:
        if getattr(cmd, "engine", "claude") == "codex":
            await self._list_codex_sessions()
            return
        try:
            infos = list_sessions()
            blocked = await asyncio.to_thread(self._bg_blocked_session_ids)
            resident_ids = {c.session_id for c in self.sessions.values() if c.session_id}
            resident_state = {c.session_id: c.state for c in self.sessions.values() if c.session_id}
            sessions = [
                SessionInfo(
                    session_id=i.session_id,
                    summary=i.summary or (i.custom_title if hasattr(i, "custom_title") else None),
                    last_modified=str(i.last_modified) if i.last_modified else None,
                    first_prompt=i.first_prompt,
                    git_branch=i.git_branch,
                    cwd=i.cwd,
                    tag=i.tag,
                    state=resident_state.get(i.session_id),
                )
                for i in infos
                if i.session_id not in blocked
            ]
            await self._emit_focused(SessionList(sessions=sessions))
            log.info("listed sessions", count=len(sessions), resident=len(resident_ids))
        except Exception as e:
            log.exception("list_sessions failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_sessions failed: {e}"))

    async def _list_codex_sessions(self) -> None:
        """Sidebar list for the Codex engine: threads from ~/.codex/sessions."""
        try:
            raw = await asyncio.to_thread(list_codex_sessions, 60)
            resident_state = {c.session_id: c.state for c in self.sessions.values()
                              if c.session_id and c.engine == "codex"}
            sessions = [
                SessionInfo(
                    session_id=r["session_id"],
                    first_prompt=r.get("first_prompt"),
                    cwd=r.get("cwd"),
                    last_modified=r.get("last_modified"),
                    engine="codex",
                    state=resident_state.get(r["session_id"]),
                )
                for r in raw
            ]
            await self._emit_focused(SessionList(sessions=sessions))
            log.info("listed codex sessions", count=len(sessions))
        except Exception as e:
            log.exception("list_codex_sessions failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_codex_sessions failed: {e}"))

    async def _handle_switch_session(self, cmd) -> None:
        # Focus change — NO disconnect. If the session is already resident, just
        # focus it (its turn keeps running in the background). If not resident,
        # spawn (resume) it. The previously-focused session is NOT interrupted.
        sid = cmd.session_id
        ctx = self.sessions.get(sid)
        if ctx is None:
            ctx = next((c for c in self.sessions.values() if c.session_id == sid), None)
        newly_spawned = ctx is None
        if ctx is None:
            ctx = await self._spawn(resume_id=sid, engine=getattr(cmd, "engine", None) or "claude")
            if ctx is None:
                # surface it on the session the user switched INTO (not the stale
                # focused one), so a spawn failure never looks like silent "no
                # response". Common cause: bad codex config / backend.
                await self._emit_to_sid(sid, Error(code=ERR_CC_CRASH,
                    message="会话启动失败:可能是 codex 配置/后端问题(见服务端日志)"))
                return
        self.focused_sid = ctx.key
        # A newly-spawned session isn't tracked by the client yet — send its
        # snapshot + full replay so the client builds a runtime for it (else the
        # client would show an empty/wrong view).
        if newly_spawned:
            # Build the client's runtime for a freshly-resumed session with a
            # lightweight Snapshot (state/cwd/id); its HISTORY arrives via the
            # client's GetHistory request — no full buffer replay (that was a flood).
            snap = Snapshot(cc_session_id=ctx.session_id,
                            state=ctx.buffer.latest_state() or ctx.state,
                            tail_text=ctx.buffer.latest_tail_text(), cwd=ctx.cwd)
            await self._emit(ctx, snap)
        await self._emit(ctx, SessionFocus(session_id=ctx.session_id or self.focused_sid or sid, cwd=ctx.cwd))
        # Seed the Fast-mode chip on entering a codex session (state is global,
        # from config.toml), so it's correct before the first turn/toggle.
        if ctx.engine == "codex":
            await self._emit(ctx, Fast(on=codex_fast_enabled()))

    async def _capture_session_id(self, ctx: SessionContext, sid: str) -> None:
        """A brand-new session learned its real cc id (from the first
        ResultMessage/init). Re-key the pool temp-key -> sid, keep ctx.key in
        sync, migrate focus ONLY if this ctx was the focused one, and tell the
        client to re-key its runtime (SessionRekey — NOT SessionFocus, else a
        background session's capture would steal the user's view)."""
        # /btw forks keep their stable `btw-<uuid>` pool key so their events always
        # route to the side panel; they're ephemeral (never resumed/saved/re-keyed).
        # Record the real forked id (cc fork_session persists one) for close-time
        # cleanup, but don't route/save under it.
        if ctx.btw:
            ctx.btw_real_id = sid
            return
        old_key = ctx.key
        ctx.session_id = sid
        if ctx.engine != "codex":
            save_session_id(self.cfg.state_dir, ctx.cwd, sid)
        if old_key and old_key != sid:
            self.sessions.pop(old_key, None)
            self.sessions[sid] = ctx
            ctx.key = sid
            if self.focused_sid == old_key:
                self.focused_sid = sid
            await self._emit(ctx, SessionRekey(old_key=old_key, session_id=sid, cwd=ctx.cwd))
        log.info("captured cc session id", sid=sid, focus_followed=(self.focused_sid == sid))

    async def _handle_new_session(self, cmd) -> None:
        ctx = await self._spawn(resume_id=None, cwd=getattr(cmd, "cwd", None),
                                engine=getattr(cmd, "engine", "claude"),
                                model=getattr(cmd, "model", None),
                                effort=getattr(cmd, "effort", None))
        if ctx is None:
            return  # error already emitted
        self.focused_sid = ctx.key
        # session_id is None until captured in _run_turn; use the pool (temp) key
        # as the focus id — the client migrates to the real sid on capture.
        await self._emit(ctx, SessionFocus(session_id=self.focused_sid, cwd=ctx.cwd))

    async def _handle_rename_session(self, cmd) -> None:
        try:
            await asyncio.to_thread(rename_session, cmd.session_id, cmd.title)
            log.info("session renamed", session_id=cmd.session_id, title=cmd.title)
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("rename_session failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"rename failed: {e}"))

    async def _handle_archive_session(self, cmd) -> None:
        try:
            tag = "archived" if cmd.archived else None
            await asyncio.to_thread(tag_session, cmd.session_id, tag)
            log.info("session archive toggled", session_id=cmd.session_id, archived=cmd.archived)
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("archive_session failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"archive failed: {e}"))

    # ---- directory picker (arbitrary-cwd session creation) ----

    async def _handle_list_dir(self, cmd) -> None:
        try:
            path, parent, dirs = await asyncio.to_thread(self._scan_dir, cmd.path)
            await self._emit_focused(DirList(path=path, parent=parent, dirs=dirs))
        except Exception as e:
            log.exception("list_dir failed", path=getattr(cmd, "path", None), error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_dir failed: {e}"))

    @staticmethod
    def _scan_dir(path: Optional[str]) -> tuple[str, Optional[str], list[dict[str, str]]]:
        base = path or os.path.expanduser("~")
        base = os.path.realpath(os.path.expanduser(base))
        if not os.path.isdir(base):
            raise FileNotFoundError(base)
        parent = os.path.dirname(base) or None
        if parent and os.path.realpath(parent) == base:
            parent = None
        dirs: list[dict[str, str]] = []
        try:
            for name in sorted(os.listdir(base)):
                if name.startswith("."):
                    continue
                full = os.path.join(base, name)
                if os.path.isdir(full):
                    dirs.append({"name": name, "path": full})
        except PermissionError:
            pass
        return base, parent, dirs

    @staticmethod
    def _bg_blocked_session_ids() -> set[str]:
        ids: set[str] = set()
        jobs = os.path.expanduser("~/.claude/jobs")
        if not os.path.isdir(jobs):
            return ids
        for name in os.listdir(jobs):
            st = os.path.join(jobs, name, "state.json")
            if not os.path.isfile(st):
                continue
            try:
                with open(st) as f:
                    s = json.load(f)
            except Exception:
                continue
            if s.get("state") != "done" and s.get("sessionId"):
                ids.add(s["sessionId"])
        return ids

    # ---- spawn (build a ctx: SdkHandle + connect + history) ----

    async def _spawn(self, resume_id: Optional[str], cwd: Optional[str] = None,
                     bootstrap: bool = False, engine: str = "claude",
                     model: Optional[str] = None, effort: Optional[str] = None) -> Optional[SessionContext]:
        """Create a SessionContext, connect its SDK subprocess, load history.
        Returns the ctx (added to the pool under its real or temp key) or None
        on failure (an Error has been emitted). `bootstrap` exempts the cap and
        retries resume→fresh on connect failure. `model`/`effort` (new_session
        only) pre-select those at spawn: effort BEFORE connect so the first turn
        runs at that strength with no respawn; cc model via a live set_model
        after connect; codex model as a per-turn field. Omitted => engine
        defaults (unchanged behavior)."""
        # Concurrency cap (bootstrap always allowed). When full, evict an idle,
        # non-focused session (tear down its subprocess; the client keeps its
        # runtime and re-spawns on re-focus). Only reject if ALL are running —
        # so merely browsing between sessions never wedges you.
        if not bootstrap and len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((k for k, c in self.sessions.items()
                           if k != self.focused_sid and c.state == "idle" and not c.btw), None)
            if victim is None:
                await self._emit_focused(Error(
                    code=ERR_BUSY,
                    message="所有会话都在运行,先中断一个再切换"))
                return None
            vc = self.sessions.pop(victim)
            try:
                await vc.sdk.disconnect()
            except Exception:
                pass
            log.info("evicted idle session for cap", key=victim)
        # Resolve the target cwd.
        if resume_id and engine == "codex":
            # Codex sessions live in ~/.codex/sessions (not the Claude SDK's store),
            # so resolve cwd from the rollout meta, not get_session_info.
            cwd_hint = await asyncio.to_thread(codex_session_cwd, resume_id)
            target_cwd = cwd_hint or self.cfg.cc_cwd
            if not os.path.isdir(target_cwd):
                target_cwd = self.cfg.cc_cwd
        elif resume_id:
            try:
                info = await asyncio.to_thread(get_session_info, resume_id)
            except Exception as e:
                log.warning("get_session_info failed", session_id=resume_id, error=str(e))
                info = None
            if info is None:
                if bootstrap:
                    # A saved bootstrap id that can't be resumed (e.g. it now points
                    # at a codex thread, or the session was deleted) must NOT crash
                    # startup — fall back to a fresh session.
                    log.warning("saved bootstrap session not resumable; starting fresh", session_id=resume_id)
                    resume_id = None
                    target_cwd = self.cfg.cc_cwd
                else:
                    await self._emit_focused(Error(code=ERR_INTERNAL, message=f"session not found: {resume_id}"))
                    return None
            else:
                target_cwd = info.cwd or self.cfg.cc_cwd
            # The session's original cwd may be gone (e.g. a deleted /tmp scratch
            # dir). cc can't chdir into a missing dir → "Working directory does not
            # exist" crash on switch. Recreate it (empty) so resume still works —
            # history loads by session id regardless; fall back to the default cwd
            # only if recreation fails.
            if not os.path.isdir(target_cwd):
                try:
                    os.makedirs(target_cwd, exist_ok=True)
                    log.warning("recreated missing session cwd for resume", session_id=resume_id, cwd=target_cwd)
                except Exception as e:
                    log.warning("session cwd missing, using default", session_id=resume_id, cwd=target_cwd, error=str(e))
                    target_cwd = self.cfg.cc_cwd
        elif cwd:
            target_cwd = os.path.realpath(os.path.expanduser(cwd))
            if not os.path.isdir(target_cwd):
                await self._emit_focused(Error(code=ERR_INTERNAL, message=f"目录不存在: {cwd}"))
                return None
        else:
            target_cwd = self.cfg.cc_cwd

        sdk = CodexHandle(self.cfg, cwd=target_cwd) if engine == "codex" else SdkHandle(self.cfg)
        # Pre-select effort at spawn (before connect): cc reads it via _options at
        # connect so --effort is baked into the first turn (no respawn); codex uses
        # it as a per-turn param. codex model is also a per-turn field, so set it
        # here; cc's model needs a live set_model AFTER connect (below). Set
        # applied_effort too so _run_turn's "effort != applied" reconnect check
        # sees the first turn as already-applied (cc's connect re-syncs it anyway;
        # this is what keeps codex from a spurious first-turn reconnect).
        if effort:
            sdk.effort = effort
            sdk.applied_effort = effort
        if model and engine == "codex":
            sdk.model = model
        ctx = SessionContext(
            session_id=resume_id,
            sdk=sdk,
            buffer=RingBuffer(self.cfg.ring_max_events, self.cfg.ring_max_bytes),
            cwd=target_cwd,
            engine=engine,
        )
        # Per-ctx MCP ask server is Claude-only (the cc-remote-ask tools). Codex
        # handles approvals through its own app-server protocol, so skip it.
        if engine != "codex":
            ctx.sdk.ask_server = make_ask_server(
                lambda q, o: self._on_ask(ctx, q, o),
                lambda m: self._on_set_mode(ctx, m),
            )

        try:
            await ctx.sdk.connect(resume_id=resume_id, cwd=target_cwd)
        except Exception as e:
            if bootstrap and resume_id:
                log.warning("resume failed, starting a fresh session", error=str(e))
                ctx.session_id = None
                try:
                    await ctx.sdk.connect(resume_id=None, cwd=target_cwd)
                except Exception as e2:
                    log.exception("fresh connect also failed", error=str(e2))
                    await self._emit_focused(Error(code=ERR_CC_CRASH, message=f"connect failed: {e2}"))
                    return None
            else:
                log.exception("connect failed", error=str(e))
                await self._emit_focused(Error(code=ERR_CC_CRASH, message=f"connect failed: {e}"))
                return None

        # cc model is a runtime switch on the live subprocess (set_model), so apply
        # a pre-selected model now that we're connected. codex was set pre-connect.
        if model and engine != "codex":
            try:
                await ctx.sdk.set_model(model)
            except Exception as e:
                log.warning("spawn set_model failed", model=model, error=str(e))
        # Record the pre-selected values so _run_turn doesn't redundantly re-announce
        # them (the client already reflects its own pick optimistically).
        if model:
            ctx.announced_model = model
        if effort:
            ctx.announced_effort = effort
        # Pool key: real sid if known, else a temp id until captured in _run_turn.
        key = resume_id or f"tmp-{uuid4().hex}"
        self.sessions[key] = ctx
        ctx.key = key
        if resume_id and engine != "codex":
            save_session_id(self.cfg.state_dir, target_cwd, resume_id)
        await self._load_history(ctx, resume_id)
        if bootstrap:
            ctx.announced_perm = "bypassPermissions"
            await self._emit(ctx, Perm(mode=ctx.announced_perm))
        log.info("session spawned", resume=resume_id, cwd=target_cwd, key=key,
                 resident=len(self.sessions))
        return ctx

    async def _spawn_btw(self, parent: SessionContext) -> Optional[SessionContext]:
        """Spawn an ephemeral /btw fork of `parent`: a throwaway side-session that
        inherits the parent's context (codex thread/fork · cc fork_session) and
        streams under a stable `btw-<uuid>` key. Never persisted / listed / focused;
        discarded on close. Its turns reuse the normal _run_turn path."""
        parent_id = parent.session_id
        if not parent_id:
            await self._emit_focused(Error(code=ERR_INTERNAL,
                message="这个会话还没有上下文,先发一条消息再开 btw"))
            return None
        # btw counts toward the cap; evict an idle, non-focused, non-btw victim.
        if len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((k for k, c in self.sessions.items()
                           if k != self.focused_sid and c.state == "idle" and not c.btw), None)
            if victim is None:
                await self._emit_focused(Error(code=ERR_BUSY, message="会话已满,先关闭一个再开 btw"))
                return None
            vc = self.sessions.pop(victim)
            try:
                await vc.sdk.disconnect()
            except Exception:
                pass
            log.info("evicted idle session for btw", key=victim)
        engine = parent.engine
        sdk = CodexHandle(self.cfg, cwd=parent.cwd) if engine == "codex" else SdkHandle(self.cfg)
        # /btw is a quick side question — run the fork at LOW effort so the first
        # reply is snappy (the parent's own effort can be high/xhigh, which makes a
        # context-inheriting fork slow). Applied at connect (cc) / per-turn (codex).
        sdk.effort = "low"
        ctx = SessionContext(
            session_id=None, sdk=sdk,
            buffer=RingBuffer(self.cfg.ring_max_events, self.cfg.ring_max_bytes),
            cwd=parent.cwd, engine=engine, btw=True, parent_sid=parent_id)
        if engine != "codex":
            ctx.sdk.ask_server = make_ask_server(
                lambda q, o: self._on_ask(ctx, q, o),
                lambda m: self._on_set_mode(ctx, m),
            )
        try:
            await ctx.sdk.connect(resume_id=parent_id, cwd=parent.cwd, fork=True)
        except Exception as e:
            log.exception("btw fork connect failed", error=str(e))
            await self._emit_focused(Error(code=ERR_CC_CRASH, message=f"btw fork 失败: {e}"))
            return None
        key = f"btw-{uuid4().hex}"
        self.sessions[key] = ctx
        ctx.key = key
        log.info("btw fork spawned", parent=parent_id, key=key, engine=engine,
                 fork_thread=getattr(ctx.sdk, "thread_id", None))
        return ctx

    async def _load_history(self, ctx: SessionContext, session_id: Optional[str]) -> None:
        if not session_id or ctx.engine == "codex":
            return  # codex history replay (rollout files) is a later feature
        try:
            msgs = await asyncio.to_thread(
                get_session_messages, session_id, directory=ctx.cwd,
            )
        except Exception as e:
            log.warning("get_session_messages failed", session_id=session_id, error=str(e))
            return
        try:
            events = translate_history(msgs, self.cfg.tool_result_max)
            mdl = last_assistant_model(msgs)
        except Exception as e:
            # a single malformed history message must never break the resume — the
            # session still connects; the client just won't get the replayed history.
            log.exception("translate_history failed; resuming without replay", session_id=session_id, error=str(e))
            return
        async with ctx.emit_lock:
            if mdl and mdl.startswith("claude-") and mdl != ctx.announced_model:
                ctx.announced_model = mdl
                m = Model(model=mdl)
                m.seq = ctx.next_seq()
                m.sid = ctx.session_id
                ctx.buffer.append(m)
            for ev in events:
                ev.seq = ctx.next_seq()
                ev.sid = ctx.session_id
                ctx.buffer.append(ev)
        log.info("history loaded", session_id=session_id, events=len(events),
                 model=mdl, head=ctx.buffer.head_seq, tail=ctx.buffer.tail_seq)

    # ---- the per-turn consumer (reader task + queue), per ctx ----

    async def _next_from_queue(self, ctx: SessionContext, queue: asyncio.Queue):
        if ctx.state == "interrupting":
            return await asyncio.wait_for(queue.get(), timeout=self.cfg.drain_timeout)
        return await queue.get()

    def _stash_files(self, prompt: str, files: list, engine: str = "claude") -> str:
        """Write attached files to /tmp and reference them in the prompt. cc reads
        the `@path` convention; codex has no `@` layer over the app-server and
        ignores `mention` items (verified), but reads a plain path via its tools —
        so codex gets an explicit 'read these files' block with bare paths."""
        import base64, os, re, time
        paths = []
        for i, f in enumerate(files):
            fn = f.get("filename") or f"file-{i}"
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", fn) or f"file-{i}"
            path = os.path.join("/tmp", f"cc-remote-{int(time.time())}-{safe}")
            try:
                data = base64.b64decode(f.get("data", ""))
                with open(path, "wb") as fp:
                    fp.write(data)
                paths.append(path)
                log.info("attachment stashed", path=path, bytes=len(data))
            except Exception as e:
                log.warning("failed to stash attachment", filename=fn, error=str(e))
        if not paths:
            return prompt
        if engine == "codex":
            block = "[用户附件,请用工具读取以下文件]:\n" + "\n".join(paths)
        else:
            block = " ".join(f"@{p}" for p in paths)
        return (prompt + "\n\n" if prompt else "") + block

    def _stash_images(self, images: list) -> list:
        """Write base64 images to /tmp and return their paths — codex reads images
        via `{type:"localImage", path}` (verified: it saw a blue test image)."""
        import base64, os, time
        _ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                "image/webp": ".webp", "image/gif": ".gif"}
        paths = []
        for i, img in enumerate(images or []):
            mt = img.get("media_type", "image/png")
            path = os.path.join("/tmp", f"cc-remote-{int(time.time())}-img{i}{_ext.get(mt, '.png')}")
            try:
                with open(path, "wb") as fp:
                    fp.write(base64.b64decode(img.get("data", "")))
                paths.append(path)
                log.info("image stashed", path=path)
            except Exception as e:
                log.warning("failed to stash image", error=str(e))
        return paths

    def _cleanup_tmp(self) -> None:
        import os, time
        try:
            cutoff = time.time() - 30 * 86400
            for name in os.listdir("/tmp"):
                if name.startswith("cc-remote-"):
                    p = os.path.join("/tmp", name)
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.remove(p)
        except Exception as e:
            log.warning("tmp cleanup failed", error=str(e))

    async def _run_turn(self, ctx: SessionContext, prompt: str,
                        images: Optional[list] = None, files: Optional[list] = None) -> None:
        is_codex = ctx.engine == "codex"
        ctx.translator = (CodexStreamTranslator(self.cfg.tool_result_max) if is_codex
                          else StreamTranslator(self.cfg.tool_result_max))
        queue: asyncio.Queue = asyncio.Queue()
        reader_exc: list = []
        reader_task: Optional[asyncio.Task] = None

        async def reader() -> None:
            try:
                async for msg in ctx.sdk.receive_response():
                    await queue.put(msg)
            except BaseException as e:
                reader_exc.append(e)
            finally:
                await queue.put(None)

        try:
            # apply a pending effort change: --effort is spawn-time, so respawn the
            # cc subprocess (resume preserves context) before issuing this turn. Only
            # fires when the level actually changed since the live client was spawned;
            # costs one resume (cold prompt cache) on the first turn after a change.
            if not is_codex and ctx.sdk.effort != ctx.sdk.applied_effort:
                log.info("applying effort change via reconnect", sid=ctx.session_id,
                         effort=ctx.sdk.effort, was=ctx.sdk.applied_effort)
                await ctx.sdk.force_reconnect(resume_id=ctx.session_id, cwd=ctx.cwd, reason="effort change")
            # codex Fast-mode toggle changed ~/.codex/config.toml; respawn the
            # app-server so it reloads the new service_tier (resume keeps context).
            if is_codex and getattr(ctx.sdk, "tier_dirty", False):
                log.info("applying codex service tier via reconnect", sid=ctx.session_id,
                         service_tier=ctx.sdk.service_tier)
                await ctx.sdk.force_reconnect(resume_id=ctx.session_id, cwd=ctx.cwd, reason="service tier change")
                ctx.sdk.tier_dirty = False
            if files:
                prompt = self._stash_files(prompt, files, ctx.engine)
            if is_codex:
                # codex: images -> /tmp -> localImage input items; files already
                # referenced by path in the prompt text above.
                img_paths = self._stash_images(images) if images else []
                await ctx.sdk.query(prompt, images=img_paths)
            elif images:
                content: list = []
                if prompt:
                    content.append({"type": "text", "text": prompt})
                for img in images:
                    content.append({"type": "image", "source": {
                        "type": "base64",
                        "media_type": img.get("media_type", "image/png"),
                        "data": img.get("data", ""),
                    }})

                async def msg_stream():
                    yield {"type": "user", "message": {"role": "user", "content": content},
                           "parent_tool_use_id": None}

                await ctx.sdk.query(msg_stream())
            else:
                await ctx.sdk.query(prompt)
            # Codex sessions don't emit a Model event like cc's init SystemMessage,
            # so announce the configured codex model (gpt-*) once — else the header
            # would keep showing a stale Claude model.
            if is_codex:
                if ctx.announced_model != ctx.sdk.model:
                    ctx.announced_model = ctx.sdk.model
                    await self._emit(ctx, Model(model=ctx.announced_model))
                if ctx.announced_effort != ctx.sdk.effort:
                    ctx.announced_effort = ctx.sdk.effort
                    await self._emit(ctx, Effort(effort=ctx.sdk.effort))
                # seed/refresh the Fast-mode chip from the live config each turn.
                await self._emit(ctx, Fast(on=codex_fast_enabled()))
            reader_task = asyncio.create_task(reader())
            while True:
                msg = await self._next_from_queue(ctx, queue)
                if msg is None:
                    if reader_exc:
                        raise reader_exc[0]
                    raise RuntimeError("cc stream ended without a ResultMessage")

                if is_codex:
                    sid = codex_session_id(msg)
                    if sid and not ctx.session_id:
                        await self._capture_session_id(ctx, sid)
                    for ev in ctx.translator.feed(msg):
                        await self._emit(ctx, ev)
                    if is_turn_terminal(msg):
                        break
                    continue

                log.debug("sdk msg", sid=ctx.session_id, msg_type=type(msg).__name__)

                sid = extract_session_id(msg)
                if sid and not ctx.session_id:
                    await self._capture_session_id(ctx, sid)

                # cc-only path (the codex branch continues above). Only announce
                # Claude-branded models so a cc-switch proxy's raw upstream name
                # (e.g. glm-5.2) never replaces the user's Claude alias in the chip.
                mdl = extract_model(msg)
                if mdl and mdl != ctx.announced_model and mdl.startswith("claude-"):
                    ctx.announced_model = mdl
                    await self._emit(ctx, Model(model=mdl))

                for ev in ctx.translator.feed(msg):
                    await self._emit(ctx, ev)

                if isinstance(msg, ResultMessage):
                    break

            await self._set_state(ctx, "idle")
        except asyncio.TimeoutError:
            log.error("drain timeout — interrupt did not yield a ResultMessage", prompt=prompt[:80])
            await self._emit(ctx, Error(code=ERR_DRAIN_TIMEOUT, message="interrupt drain timed out; reconnecting cc"))
            try:
                await ctx.sdk.force_reconnect(ctx.session_id, ctx.cwd)
            except Exception as e:
                log.exception("force reconnect failed", error=str(e))
                await self._emit(ctx, Error(code=ERR_CC_CRASH, message=f"reconnect failed: {e}"))
            await self._set_state(ctx, "idle")
        except Exception as e:
            log.exception("turn failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_CC_CRASH, message=str(e)))
            await self._set_state(ctx, "idle")
        finally:
            ctx.translator = None
            ctx.turn_task = None
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
