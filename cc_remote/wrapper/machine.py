"""The wrapper brain: state machine + per-turn consumer + interrupt/drain.

States: idle / running / interrupting / draining.

MVP policy: reject-while-busy. Any `query` while state != idle is rejected with
`busy`. This structurally prevents the SDK drain footgun — there is never a
second `query()` racing the drain. One async turn task per query; its consumer
always runs to the terminal ResultMessage (normal `success` or interrupted
`error_during_execution`) before state returns to idle.

interrupt(): set state=interrupting, call sdk.interrupt(), and let the SAME
consumer keep iterating until the terminal ResultMessage arrives — that is the
drain. A drain timeout force-reconnects the SDK as a safety net.

Reader/queue split: a background task iterates the SDK's async generator
WITHOUT asyncio.wait_for — wrapping __anext__ in wait_for corrupts the generator
when the short poll times out (e.g. during model latency). The turn reads from
an asyncio.Queue instead, and cancelling queue.get (for the drain timeout) is
safe and corrupts nothing.

An `_emit_lock` serializes outgoing frames so a client's replay batch (sent
under the lock) is not interleaved with a running turn's live events. Replay
frames are tagged `to=<client_id>` so the relay routes them to that client only.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from claude_agent_sdk import list_sessions, get_session_messages, rename_session, tag_session, get_session_info
from claude_agent_sdk.types import ResultMessage

from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    Error, Hello, Model, Perm, ContextReport, DiffReport, AskUser, Pong, StateEvent, State, UserMsg, is_downstream,
    SessionInfo, SessionList, SessionSwitched,
    ERR_BUSY, ERR_NOT_RUNNING, ERR_BAD_PROMPT, ERR_DRAIN_TIMEOUT,
    ERR_CC_CRASH, ERR_INTERNAL,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.ask import make_ask_server
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.session import load_session_id, save_session_id
from cc_remote.wrapper.stream import (
    StreamTranslator, extract_session_id, extract_model,
    translate_history, last_assistant_model,
)
from cc_remote.wrapper.transport import WrapperTransport

log = logger("cc_remote.wrapper.machine")


class WrapperMachine:
    def __init__(self, cfg: WrapperConfig, sdk: SdkHandle, transport: WrapperTransport):
        self.cfg = cfg
        self.sdk = sdk
        self.transport = transport
        self.buffer = RingBuffer(cfg.ring_max_events, cfg.ring_max_bytes)
        self.state: State = "idle"
        self.cc_session_id: Optional[str] = (
            cfg.resume_session_id or load_session_id(cfg.state_dir, cfg.cc_cwd)
        )
        # Active cc cwd — starts at the configured default, moves with each
        # cross-project session switch (resume requires cwd to match the jsonl path).
        self.cc_cwd: str = cfg.cc_cwd
        self._seq = 0
        self._turn_task: Optional[asyncio.Task] = None
        self._translator: Optional[StreamTranslator] = None
        self._emit_lock = asyncio.Lock()
        self._announced_model: Optional[str] = None
        self._announced_perm: Optional[str] = None
        # ask_user MCP tool: ask_id -> Future the handler awaits until the
        # client returns an AnswerQuestion. The in-process MCP server is wired
        # into ClaudeAgentOptions.mcp_servers so the agent can call `ask_user`.
        self._pending_asks: dict[str, asyncio.Future] = {}
        self.sdk.ask_server = make_ask_server(self._on_ask)

    # ---- lifecycle ----

    async def run(self) -> None:
        self._cleanup_tmp()
        try:
            await self._connect_sdk()
        except Exception:
            log.exception("sdk connect failed")
            raise
        # Load the resumed session's history into the buffer so a wrapper restart
        # doesn't leave the chat empty — reconnecting clients replay it via hello.
        await self._load_history(self.cc_session_id)
        # Announce the initial permission mode (bypassPermissions from options) into the
        # ring buffer so a connecting client's chip reflects reality; set_perm updates it.
        self._announced_perm = "bypassPermissions"
        await self._emit(Perm(mode=self._announced_perm))
        self.transport.on_connected = self._on_transport_connected
        await self.transport.start()
        log.info("wrapper running", state=self.state, session_id=self.cc_session_id)
        try:
            async for cmd in self.transport.incoming():
                try:
                    await self._handle(cmd)
                except Exception:
                    log.exception("command handling failed", type=cmd.type)
        finally:
            await self.transport.stop()
            await self.sdk.disconnect()

    async def _connect_sdk(self) -> None:
        try:
            await self.sdk.connect(resume_id=self.cc_session_id)
        except Exception as e:
            if self.cc_session_id:
                log.warning("resume failed, starting a fresh session", error=str(e))
                self.cc_session_id = None
                await self.sdk.connect(resume_id=None)
            else:
                raise

    async def _on_transport_connected(self) -> None:
        await self.transport.send(Hello(
            role="wrapper",
            cc_session_id=self.cc_session_id,
            state=self.state,
            buffer_head_seq=self.buffer.head_seq,
            buffer_tail_seq=self.buffer.tail_seq,
        ))

    # ---- emit (seq + buffer + best-effort send), serialized ----

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _emit_locked(self, msg) -> None:
        """Emit assuming _emit_lock is held: assign seq, buffer, best-effort send."""
        if is_downstream(msg):
            msg.seq = self._next_seq()
            self.buffer.append(msg)
        msg.sid = self.cc_session_id
        await self.transport.send(msg)

    async def _emit(self, msg) -> None:
        async with self._emit_lock:
            await self._emit_locked(msg)

    async def _set_state(self, state: State) -> None:
        self.state = state
        await self._emit(StateEvent(state=state))
        log.info("state transition", state=state)

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
        elif t == "set_perm":
            await self._handle_set_perm(cmd)
        elif t == "get_context":
            await self._handle_get_context(cmd)
        elif t == "get_diff":
            await self._handle_get_diff(cmd)
        elif t == "answer_question":
            await self._handle_answer_question(cmd)
        elif t == "list_sessions":
            await self._handle_list_sessions(cmd)
        elif t == "switch_session":
            await self._handle_switch_session(cmd)
        elif t == "new_session":
            await self._handle_new_session(cmd)
        elif t == "rename_session":
            await self._handle_rename_session(cmd)
        elif t == "archive_session":
            await self._handle_archive_session(cmd)
        elif t == "ping":
            await self._emit(Pong(n=cmd.n))
        else:
            log.warning("unexpected command", type=t, role=getattr(cmd, "role", None))

    async def _handle_client_hello(self, cmd) -> None:
        tail = self.buffer.latest_tail_text()
        st = self.buffer.latest_state() or self.state
        frames = self.buffer.replay_from(
            cmd.last_seq,
            cc_session_id=self.cc_session_id,
            state=st,
            tail_text=tail,
        )
        # Send the whole replay batch under the lock so it isn't interleaved with
        # a running turn's live emits. Tag each frame `to=<client_id>` so the
        # relay routes it only to this client. Copy so we don't mutate buffered
        # objects (replayed events share references with the ring buffer).
        async with self._emit_lock:
            for f in frames:
                f = f.model_copy(update={"to": cmd.client_id, "sid": self.cc_session_id})
                await self.transport.send(f)
        log.info("client hello handled", client_id=cmd.client_id, last_seq=cmd.last_seq,
                 frames=len(frames), head=self.buffer.head_seq, tail=self.buffer.tail_seq)

    async def _handle_query(self, cmd) -> None:
        if self.state != "idle":
            await self._emit(Error(code=ERR_BUSY, message="a turn is already running; interrupt first"))
            return
        if not cmd.prompt and not cmd.images and not cmd.files:
            await self._emit(Error(code=ERR_BAD_PROMPT, message="empty prompt"))
            return
        # claim synchronously so a concurrent query can't race in
        self.state = "running"
        # Broadcast the user's prompt (so other devices see the full
        # conversation) + the running state, atomically under one lock so a
        # connecting client's replay can't split them.
        async with self._emit_lock:
            await self._emit_locked(UserMsg(msg_id=cmd.msg_id, prompt=cmd.prompt))
            await self._emit_locked(StateEvent(state="running"))
        self._turn_task = asyncio.create_task(self._run_turn(cmd.prompt, getattr(cmd, "images", None), getattr(cmd, "files", None)))

    async def _handle_interrupt(self, cmd) -> None:
        if self.state != "running":
            await self._emit(Error(code=ERR_NOT_RUNNING, message="no running turn to interrupt"))
            return
        # claim synchronously so _next_from_queue's drain-timeout branch activates
        self.state = "interrupting"
        await self._emit(StateEvent(state="interrupting"))
        try:
            await self.sdk.interrupt()
        except Exception as e:
            log.exception("interrupt call failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"interrupt failed: {e}"))
            # leave interrupting; _next_from_queue's drain timeout will recover

    async def _handle_set_model(self, cmd) -> None:
        try:
            await self.sdk.set_model(cmd.model)
            self._announced_model = cmd.model
            await self._emit(Model(model=cmd.model))
        except Exception as e:
            log.exception("set_model failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"set_model failed: {e}"))

    async def _handle_set_perm(self, cmd) -> None:
        try:
            await self.sdk.set_permission_mode(cmd.mode)
            self._announced_perm = cmd.mode
            await self._emit(Perm(mode=cmd.mode))
        except Exception as e:
            log.exception("set_permission_mode failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"set_perm failed: {e}"))

    async def _handle_get_context(self, cmd) -> None:
        try:
            usage = await self.sdk.get_context_usage()
            await self._emit(ContextReport(
                total_tokens=usage.get("totalTokens", 0),
                max_tokens=usage.get("maxTokens", 0),
                percentage=usage.get("percentage", 0.0),
                model=usage.get("model"),
                is_auto_compact_enabled=usage.get("isAutoCompactEnabled"),
                categories=usage.get("categories", []) or [],
            ))
        except Exception as e:
            log.exception("get_context_usage failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"get_context failed: {e}"))

    async def _handle_get_diff(self, cmd) -> None:
        try:
            diff = await self._git_diff(cmd.file)
            await self._emit(DiffReport(file=cmd.file, diff=diff))
        except Exception as e:
            log.exception("get_diff failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"get_diff failed: {e}"))

    # ---- ask_user MCP tool (agent asks the user a multiple-choice question) ----

    async def _on_ask(self, question: str, options: list[dict[str, str]]) -> str:
        """Called by the in-process MCP server when the agent invokes `ask_user`.
        Emits an AskUser event to the client and blocks (awaits a Future) until
        the client returns AnswerQuestion. Runs in the SDK's reader task while
        the turn loop is blocked on receive_response() — the wrapper's command
        loop is a separate task, so it can still deliver the answer."""
        ask_id = f"ask-{self._next_seq()}"
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_asks[ask_id] = fut
        await self._emit(AskUser(ask_id=ask_id, question=question, options=options))
        log.info("ask_user emitted", ask_id=ask_id, options=len(options))
        try:
            # 30 min ceiling so a forgotten prompt doesn't wedge the turn forever.
            return await asyncio.wait_for(fut, timeout=30 * 60)
        except asyncio.TimeoutError:
            log.warning("ask_user timed out", ask_id=ask_id)
            return "(用户未回答，已超时)"
        finally:
            self._pending_asks.pop(ask_id, None)

    async def _handle_answer_question(self, cmd) -> None:
        fut = self._pending_asks.get(cmd.ask_id)
        if fut is None:
            log.warning("answer for unknown ask_id", ask_id=cmd.ask_id)
            return
        if not fut.done():
            fut.set_result(cmd.answer)
            log.info("ask_user answered", ask_id=cmd.ask_id)
        else:
            log.warning("answer for already-done ask_id", ask_id=cmd.ask_id)

    async def _git_diff(self, file: str) -> str:
        """Raw `git diff` (vs HEAD) text. Empty file => all files (with
        `diff --git` headers); a single file falls back to --no-index for
        new/untracked files (full-add diff). The client parses + renders this
        with its own Claude-style green/red gutter (theme-adaptive, no pager)."""
        if not file:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", self.cc_cwd, "diff", "HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await proc.communicate()
            return out.decode(errors="replace") if out else ""
        # single tracked file vs HEAD
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self.cc_cwd, "diff", "HEAD", "--", file,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        diff = out.decode(errors="replace") if out else ""
        if diff.strip():
            return diff
        # untracked/new file => diff against /dev/null (full add)
        proc2 = await asyncio.create_subprocess_exec(
            "git", "-C", self.cc_cwd, "diff", "--no-index", "/dev/null", file,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out2, _ = await proc2.communicate()
        return out2.decode(errors="replace") if out2 else ""

    # ---- sessions (list / switch / new) ----

    async def _handle_list_sessions(self, cmd) -> None:
        try:
            # No directory -> list sessions across ALL projects (each carries its cwd).
            infos = list_sessions()
            sessions = [
                SessionInfo(
                    session_id=i.session_id,
                    summary=i.summary or (i.custom_title if hasattr(i, "custom_title") else None),
                    last_modified=str(i.last_modified) if i.last_modified else None,
                    first_prompt=i.first_prompt,
                    git_branch=i.git_branch,
                    cwd=i.cwd,
                    tag=i.tag,
                )
                for i in infos
            ]
            await self._emit(SessionList(sessions=sessions))
            log.info("listed sessions", count=len(sessions))
        except Exception as e:
            log.exception("list_sessions failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"list_sessions failed: {e}"))

    async def _handle_switch_session(self, cmd) -> None:
        if self.state != "idle":
            await self._emit(Error(code=ERR_BUSY, message="等当前回合结束再切换会话"))
            return
        await self._switch_to(cmd.session_id)

    async def _handle_new_session(self, cmd) -> None:
        if self.state != "idle":
            await self._emit(Error(code=ERR_BUSY, message="等当前回合结束再切换会话"))
            return
        await self._switch_to(None)

    async def _handle_rename_session(self, cmd) -> None:
        # Writing the custom-title jsonl entry doesn't touch the cc subprocess,
        # so this is safe even mid-turn. Refresh the list so every client sees
        # the new title.
        try:
            await asyncio.to_thread(rename_session, cmd.session_id, cmd.title)
            log.info("session renamed", session_id=cmd.session_id, title=cmd.title)
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("rename_session failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"rename failed: {e}"))

    async def _handle_archive_session(self, cmd) -> None:
        try:
            tag = "archived" if cmd.archived else None
            await asyncio.to_thread(tag_session, cmd.session_id, tag)
            log.info("session archive toggled", session_id=cmd.session_id, archived=cmd.archived)
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("archive_session failed", error=str(e))
            await self._emit(Error(code=ERR_INTERNAL, message=f"archive failed: {e}"))

    async def _switch_to(self, resume_id: Optional[str]) -> None:
        """Disconnect the cc client, clear the ring buffer, reconnect with the
        new resume id (None = fresh session) using that session's cwd, and load
        its on-disk history into the buffer. The relay WS stays up; the client
        clears turns + re-hellos on SessionSwitched and replays the history."""
        # Resolve the target cwd BEFORE reconnecting: cc's --resume locates the
        # session jsonl under ~/.claude/projects/<cwd-with->/, so cwd must match.
        # New session -> stay in the currently active project's cwd.
        if resume_id:
            try:
                info = await asyncio.to_thread(get_session_info, resume_id)
            except Exception as e:
                log.warning("get_session_info failed", session_id=resume_id, error=str(e))
                info = None
            if info is None:
                await self._emit(Error(code=ERR_INTERNAL, message=f"session not found: {resume_id}"))
                return
            target_cwd = info.cwd or self.cfg.cc_cwd
        else:
            target_cwd = self.cc_cwd
        log.info("switching session", resume=resume_id, cwd=target_cwd)
        self.state = "idle"
        self._announced_model = None
        self.cc_session_id = resume_id
        self.cc_cwd = target_cwd
        # clear the buffer + seq so the new session starts clean
        self.buffer = RingBuffer(self.cfg.ring_max_events, self.cfg.ring_max_bytes)
        self._seq = 0
        try:
            await self.sdk.disconnect()
        except Exception as e:
            log.warning("disconnect during switch failed", error=str(e))
        try:
            await self.sdk.connect(resume_id=resume_id, cwd=target_cwd)
        except Exception as e:
            log.exception("reconnect during switch failed", error=str(e))
            await self._emit(Error(code=ERR_CC_CRASH, message=f"switch failed: {e}"))
            return
        # Persist under the (possibly new) cwd key so a wrapper restart resumes
        # the switched-to session, not the originally configured one.
        if resume_id:
            save_session_id(self.cfg.state_dir, self.cc_cwd, resume_id)
        # Populate the buffer with the session's past turns BEFORE notifying
        # clients: on SessionSwitched every client re-hellos with last_seq=null
        # and replays the whole buffer, so the history must already be in it or
        # the switched chat would look empty. Buffer-only (no broadcast) — each
        # client gets the history via its own hello replay.
        await self._load_history(resume_id)
        await self._emit(SessionSwitched(session_id=resume_id or ""))
        # re-announce to the relay (buffer bounds changed)
        await self._on_transport_connected()

    async def _load_history(self, session_id: Optional[str]) -> None:
        """Read a switched session's on-disk transcript and append it to the
        ring buffer as wire events, so a reconnecting client sees the past
        conversation. Buffer-only — not broadcast — because every client
        replays the buffer via hello(last_seq=null) on SessionSwitched."""
        if not session_id:
            return
        try:
            # get_session_messages does sync file IO; run off the event loop.
            msgs = await asyncio.to_thread(
                get_session_messages, session_id, directory=self.cc_cwd,
            )
        except Exception as e:
            log.warning("get_session_messages failed", session_id=session_id, error=str(e))
            return
        events = translate_history(msgs, self.cfg.tool_result_max)
        mdl = last_assistant_model(msgs)
        async with self._emit_lock:
            # Announce the model only for claude-* ids: the proxy reports
            # claude-* names (which the frontend maps to a friendly name), but
            # older transcripts may carry the raw backend id (e.g. glm-5.2) that
            # the frontend can't map — skip those so the chip stays on the last
            # known Claude model until a live init SystemMessage refreshes it.
            if mdl and mdl.startswith("claude-") and mdl != self._announced_model:
                self._announced_model = mdl
                m = Model(model=mdl)
                m.seq = self._next_seq()
                m.sid = self.cc_session_id
                self.buffer.append(m)
            for ev in events:
                ev.seq = self._next_seq()
                ev.sid = self.cc_session_id
                self.buffer.append(ev)
        log.info("history loaded", session_id=session_id, events=len(events),
                 model=mdl, head=self.buffer.head_seq, tail=self.buffer.tail_seq)

    # ---- the per-turn consumer (reader task + queue) ----

    async def _next_from_queue(self, queue: asyncio.Queue):
        """Wait for the next SDK message. While interrupting, enforce the drain
        timeout (cancelling a queue.get is safe — it does NOT corrupt the SDK's
        async generator, unlike cancelling __anext__); while running, wait
        without a timeout (model latency can be seconds; the user can interrupt
        a stuck turn)."""
        if self.state == "interrupting":
            return await asyncio.wait_for(queue.get(), timeout=self.cfg.drain_timeout)
        return await queue.get()

    def _stash_files(self, prompt: str, files: list) -> str:
        """Write uploaded file attachments to /tmp and append @path references to
        the prompt so cc's Read tool picks them up (avoids dumping file contents
        into the prompt). Files are cleaned up after 30 days (see _cleanup_tmp)."""
        import base64, os, re, time
        refs = []
        for i, f in enumerate(files):
            fn = f.get("filename") or f"file-{i}"
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", fn) or f"file-{i}"
            path = os.path.join("/tmp", f"cc-remote-{int(time.time())}-{safe}")
            try:
                data = base64.b64decode(f.get("data", ""))
                with open(path, "wb") as fp:
                    fp.write(data)
                refs.append(f"@{path}")
                log.info("attachment stashed", path=path, bytes=len(data))
            except Exception as e:
                log.warning("failed to stash attachment", filename=fn, error=str(e))
        if refs:
            return (prompt + "\n\n" if prompt else "") + " ".join(refs)
        return prompt

    def _cleanup_tmp(self) -> None:
        """Remove cc-remote-* files in /tmp older than 30 days."""
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

    async def _run_turn(self, prompt: str, images: Optional[list] = None, files: Optional[list] = None) -> None:
        self._translator = StreamTranslator(self.cfg.tool_result_max)
        queue: asyncio.Queue = asyncio.Queue()
        reader_exc: list = []
        reader_task: Optional[asyncio.Task] = None

        async def reader() -> None:
            # Iterate the SDK generator with NO wait_for — wrapping __anext__ in
            # wait_for corrupts the generator when the poll times out.
            try:
                async for msg in self.sdk.receive_response():
                    await queue.put(msg)
            except BaseException as e:
                reader_exc.append(e)
            finally:
                await queue.put(None)  # sentinel: stream ended

        try:
            if files:
                prompt = self._stash_files(prompt, files)
            if images:
                # Multimodal: build content blocks (text + base64 images) and stream
                # as an async iterable of user-message dicts (SDK query accepts this).
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

                await self.sdk.query(msg_stream())
            else:
                await self.sdk.query(prompt)
            reader_task = asyncio.create_task(reader())
            while True:
                msg = await self._next_from_queue(queue)
                if msg is None:
                    if reader_exc:
                        raise reader_exc[0]
                    raise RuntimeError("cc stream ended without a ResultMessage")
                log.debug("sdk msg", msg_type=type(msg).__name__)

                sid = extract_session_id(msg)
                if sid and not self.cc_session_id:
                    self.cc_session_id = sid
                    save_session_id(self.cfg.state_dir, self.cc_cwd, sid)
                    log.info("captured cc session id", session_id=sid)

                mdl = extract_model(msg)
                if mdl and mdl != self._announced_model:
                    self._announced_model = mdl
                    await self._emit(Model(model=mdl))

                for ev in self._translator.feed(msg):
                    await self._emit(ev)

                if isinstance(msg, ResultMessage):
                    break

            await self._set_state("idle")
        except asyncio.TimeoutError:
            log.error("drain timeout — interrupt did not yield a ResultMessage", prompt=prompt[:80])
            await self._emit(Error(code=ERR_DRAIN_TIMEOUT, message="interrupt drain timed out; reconnecting cc"))
            try:
                await self.sdk.force_reconnect(self.cc_session_id, self.cc_cwd)
            except Exception as e:
                log.exception("force reconnect failed", error=str(e))
                await self._emit(Error(code=ERR_CC_CRASH, message=f"reconnect failed: {e}"))
            await self._set_state("idle")
        except Exception as e:
            log.exception("turn failed", error=str(e))
            await self._emit(Error(code=ERR_CC_CRASH, message=str(e)))
            await self._set_state("idle")
        finally:
            self._translator = None
            self._turn_task = None
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
