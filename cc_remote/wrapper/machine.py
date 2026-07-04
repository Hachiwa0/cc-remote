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

from claude_agent_sdk.types import ResultMessage

from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    Error, Hello, Pong, StateEvent, State, UserMsg, is_downstream,
    ERR_BUSY, ERR_NOT_RUNNING, ERR_BAD_PROMPT, ERR_DRAIN_TIMEOUT,
    ERR_CC_CRASH, ERR_INTERNAL,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.session import load_session_id, save_session_id
from cc_remote.wrapper.stream import StreamTranslator, extract_session_id
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
        self._seq = 0
        self._turn_task: Optional[asyncio.Task] = None
        self._translator: Optional[StreamTranslator] = None
        self._emit_lock = asyncio.Lock()

    # ---- lifecycle ----

    async def run(self) -> None:
        try:
            await self._connect_sdk()
        except Exception:
            log.exception("sdk connect failed")
            raise
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
        if not cmd.prompt:
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
        self._turn_task = asyncio.create_task(self._run_turn(cmd.prompt))

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

    async def _run_turn(self, prompt: str) -> None:
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
                    save_session_id(self.cfg.state_dir, self.cfg.cc_cwd, sid)
                    log.info("captured cc session id", session_id=sid)

                for ev in self._translator.feed(msg):
                    await self._emit(ev)

                if isinstance(msg, ResultMessage):
                    break

            await self._set_state("idle")
        except asyncio.TimeoutError:
            log.error("drain timeout — interrupt did not yield a ResultMessage", prompt=prompt[:80])
            await self._emit(Error(code=ERR_DRAIN_TIMEOUT, message="interrupt drain timed out; reconnecting cc"))
            try:
                await self.sdk.force_reconnect(self.cc_session_id)
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
