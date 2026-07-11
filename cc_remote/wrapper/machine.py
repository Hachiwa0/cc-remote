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
import re
import signal
import shutil
import stat
import tempfile
import time
from collections import OrderedDict
from uuid import uuid4
from typing import Optional

from claude_agent_sdk import list_sessions, get_session_messages, rename_session, tag_session, get_session_info, delete_session
from claude_agent_sdk.types import ResultMessage

from cc_remote.attachments import decode_attachment, validate_attachments
from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    Error, Hello, Query, CommandAck, Model, Models, Effort, Fast, Perm, BtwOpened, ContextReport, DiffReport, History, AskUser, Pong, Snapshot, StateEvent, State, UserMsg, TurnEnd, TurnResult, is_downstream, is_reliable_command,
    SessionInfo, SessionList, SessionFocus, SessionRekey, DirList,
    ERR_BUSY, ERR_NOT_RUNNING, ERR_BAD_PROMPT, ERR_DRAIN_TIMEOUT,
    ERR_CC_CRASH, ERR_INTERNAL, ERR_AUTH,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.ask import make_ask_server
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.session import load_session_id, save_session_id
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.wrapper.stream import (
    StreamTranslator, extract_session_id, extract_model,
    translate_history, last_assistant_model, transcript_timestamps, transcript_path,
)
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator, codex_session_id, is_turn_terminal, codex_translate_history,
)
from cc_remote.wrapper.codex_sessions import (
    list_codex_sessions, codex_session_cwd, codex_rollout_path, codex_model,
    codex_session_settings, set_codex_config_fast, codex_fast_enabled,
)
from cc_remote.wrapper.codex_models import codex_catalog, clamp_effort
from cc_remote.wrapper.transport import WrapperTransport

log = logger("cc_remote.wrapper.machine")

CLAUDE_PERMISSION_MODES = frozenset({
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
})
CODEX_PERMISSION_MODES = frozenset({"never", "on-request", "untrusted"})


class _BtwSpawnFailure(Exception):
    """Expected /btw rejection that must be correlated and ACKed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class WrapperMachine:
    # Browser/TUI outboxes hold at most 256 commands. Keep a 2x retry window per
    # client and at most the relay's configured maximum of 64 client identities;
    # cached one-shot responses must not become an unbounded second history store.
    COMMAND_IDS_PER_CLIENT = 512
    COMMAND_CLIENTS = 64
    SESSION_ALIAS_CAP = 256
    SESSION_ALIAS_TTL = 7 * 24 * 3600
    SESSION_ALIAS_FILE_MAX_BYTES = 2 * 1024 * 1024
    # A worst-case 4096-byte cwd can expand ~6x under JSON escaping. 64 entries
    # therefore still fit the 2 MiB state-file cap while matching the maximum
    # number of resident sessions allowed by config validation.
    PRIVATE_BTW_CAP = 64
    PRIVATE_BTW_FILE_MAX_BYTES = 2 * 1024 * 1024
    BG_JOB_SCAN_MAX = 1_000
    BG_JOB_STATE_MAX_BYTES = 64 * 1024
    SAFE_RETRY_COMMANDS = frozenset({
        "list_sessions", "get_history", "get_models",
        "get_context", "get_diff", "list_dir",
    })
    # Commands whose target is a runtime ``sid``.  A /btw runtime is private to
    # the client that created it, so every operation against that sid must pass
    # the owner check before its handler is allowed to read or mutate state.
    BTW_SID_COMMANDS = frozenset({
        "query", "interrupt", "set_model", "set_effort",
        "set_service_tier", "open_btw", "close_btw", "set_perm",
        "get_context", "get_diff", "answer_question",
    })
    # These commands address a session through ``session_id`` instead.
    BTW_SESSION_COMMANDS = frozenset({
        "get_history", "switch_session", "rename_session", "archive_session",
    })

    def __init__(self, cfg: WrapperConfig, transport: WrapperTransport):
        self.cfg = cfg
        self.transport = transport
        self.instance_id = uuid4().hex
        # Pool of resident sessions, keyed by real session_id (or a `tmp-<uuid>`
        # temp key for a brand-new session until its id is captured).
        self.sessions: dict[str, SessionContext] = {}
        self.focused_sid: Optional[str] = None  # pool key of the viewed session
        self.transport.on_connected = self._on_transport_connected
        # Transcript mirror: sessions a client has opened (registered on GetHistory),
        # sid -> {"path", "size", "engine"}. The watcher polls each file's SIZE and,
        # when it grows without us having written it, mirrors the append to clients.
        self._watch: dict[str, dict] = {}
        self._watch_task: Optional[asyncio.Task] = None
        # In-memory at-most-once window for client retries. The outer and inner
        # OrderedDicts are both bounded; wrapper process restart intentionally
        # resets this window (documented residual risk, not durable exactly-once).
        self._processed_commands: OrderedDict[
            str, OrderedDict[str, tuple[object, ...]]
        ] = OrderedDict()
        # Durable temp-key -> real-id recovery. SessionRekey is a control frame;
        # if its one live send is lost after NewSession was ACKed, the next Hello
        # uses this map to replay the rekey before cursor catch-up.
        self._session_aliases = self._load_session_aliases()
        # Claude fork_session writes a real transcript even though /btw is an
        # ephemeral, owner-only UI. Persist tombstones until that transcript is
        # deleted so a crash or failed cleanup cannot expose it in SessionList or
        # let another client cold-resume it as a normal session.
        self._private_btw_sessions = self._load_private_btw_sessions()

    # ---- pool helpers ----

    def _focused_ctx(self) -> Optional[SessionContext]:
        return self.sessions.get(self.focused_sid) if self.focused_sid else None

    def _resolve_session_alias(self, sid: Optional[str]) -> Optional[str]:
        if not sid:
            return sid
        alias = self._session_aliases.get(sid)
        if alias is None:
            return sid
        self._session_aliases.move_to_end(sid)
        return alias["session_id"]

    def _ctx_for(self, sid: Optional[str]) -> Optional[SessionContext]:
        """Resolve a command's target ctx. An EXPLICIT sid that isn't resident
        returns None — NOT the focused ctx: a session whose spawn failed (e.g. bad
        codex config) must not silently reroute its query to whatever is focused
        (that made a failed session look like "no response" while its "在？" landed
        on another session). Only an absent sid (legacy/untagged commands) falls
        back to the focused view. Iterates a snapshot (a turn may re-key mid-scan)."""
        if sid:
            sid = self._resolve_session_alias(sid)
            ctx = self.sessions.get(sid)
            if ctx:
                return ctx
            for c in list(self.sessions.values()):
                if c.session_id == sid:
                    return c
            return None
        return self._focused_ctx()

    def _btw_ctx_for_command(self, cmd) -> Optional[SessionContext]:
        """Return the private /btw context targeted by ``cmd``, if any."""
        if cmd.type in self.BTW_SID_COMMANDS:
            sid = getattr(cmd, "sid", None)
        elif cmd.type in self.BTW_SESSION_COMMANDS:
            sid = getattr(cmd, "session_id", None)
        else:
            return None
        if not sid:
            return None
        for ctx in list(self.sessions.values()):
            if ctx.btw and sid in {ctx.key, ctx.session_id, ctx.btw_real_id}:
                return ctx
        return None

    async def _reject_nonowner_btw_command(self, cmd) -> Optional[Error]:
        """Fail closed when a client tries to operate another client's fork."""
        ctx = self._btw_ctx_for_command(cmd)
        target = (getattr(cmd, "sid", None) if cmd.type in self.BTW_SID_COMMANDS
                  else getattr(cmd, "session_id", None)
                  if cmd.type in self.BTW_SESSION_COMMANDS else None)
        tombstoned = bool(target and target in self._private_btw_sessions)
        if ctx is None and not tombstoned:
            return None
        client_id = getattr(cmd, "client_id", None)
        owner = ctx.owner_client_id if ctx is not None else None
        # Only the stable btw-* key is a valid live target. The persisted Claude
        # session id is internal, and session-store operations (switch/history/
        # rename/archive) would turn an ephemeral fork into a normal cold session.
        invalid_private_target = bool(
            tombstoned
            or cmd.type in self.BTW_SESSION_COMMANDS
            or (ctx is not None and target == ctx.btw_real_id)
        )
        if (not invalid_private_target and client_id and client_id == owner):
            return None
        # Relay-bound commands always carry a client id.  If an internal/legacy
        # caller omits it, route the denial to the owner rather than accidentally
        # broadcasting a frame about a private runtime.
        recipient = client_id or owner
        error = Error(
            code=ERR_AUTH,
            message="btw session belongs to another client",
            request_id=getattr(cmd, "request_id", None),
            msg_id=getattr(cmd, "msg_id", None),
            sid=(ctx.key if ctx is not None else target),
            to=recipient,
        )
        if recipient:
            await self.transport.send(error)
        log.warning(
            "rejected private btw command",
            type=cmd.type,
            btw_sid=(ctx.key if ctx is not None else target),
            client_id=client_id,
        )
        return error

    def _alias_file(self):
        return self.cfg.state_dir / "session-aliases.json"

    def _load_session_aliases(self) -> OrderedDict[str, dict]:
        aliases: OrderedDict[str, dict] = OrderedDict()
        try:
            path = self._alias_file()
            if path.stat().st_size > self.SESSION_ALIAS_FILE_MAX_BYTES:
                raise ValueError("session alias state exceeds size limit")
            with path.open() as stream:
                raw_text = stream.read(self.SESSION_ALIAS_FILE_MAX_BYTES + 1)
            if len(raw_text.encode("utf-8", "surrogatepass")) \
                    > self.SESSION_ALIAS_FILE_MAX_BYTES:
                raise ValueError("session alias state exceeds size limit")
            raw = json.loads(raw_text)
            now = time.time()
            for old_key, entry in (raw.items() if isinstance(raw, dict) else []):
                if not isinstance(entry, dict):
                    continue
                real = entry.get("session_id")
                cwd = entry.get("cwd")
                created = entry.get("created_at", 0)
                if (
                    isinstance(old_key, str)
                    and re.fullmatch(r"tmp-[0-9a-f]{32}", old_key)
                    and isinstance(real, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", real)
                    and isinstance(cwd, str) and "\x00" not in cwd
                    and len(cwd.encode("utf-8", "surrogatepass")) <= 4096
                    and isinstance(created, (int, float))
                    and 0 <= now - created <= self.SESSION_ALIAS_TTL
                ):
                    aliases[old_key] = {
                        "session_id": real, "cwd": cwd, "created_at": created,
                    }
            while len(aliases) > self.SESSION_ALIAS_CAP:
                aliases.popitem(last=False)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("session alias state ignored", error_type=type(exc).__name__)
        return aliases

    def _remember_session_alias(self, old_key: str, session_id: str, cwd: str) -> None:
        self._session_aliases[old_key] = {
            "session_id": session_id,
            "cwd": cwd,
            "created_at": time.time(),
        }
        self._session_aliases.move_to_end(old_key)
        while len(self._session_aliases) > self.SESSION_ALIAS_CAP:
            self._session_aliases.popitem(last=False)
        try:
            path = self._alias_file()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._session_aliases, separators=(",", ":")))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception as exc:
            log.warning("session alias state not persisted",
                        error_type=type(exc).__name__)

    def _private_btw_file(self):
        return self.cfg.state_dir / "private-btw-sessions.json"

    def _load_private_btw_sessions(self) -> OrderedDict[str, dict]:
        entries: OrderedDict[str, dict] = OrderedDict()
        try:
            path = self._private_btw_file()
            if path.stat().st_size > self.PRIVATE_BTW_FILE_MAX_BYTES:
                raise ValueError("private btw state exceeds size limit")
            with path.open() as stream:
                raw_text = stream.read(self.PRIVATE_BTW_FILE_MAX_BYTES + 1)
            if len(raw_text.encode("utf-8", "surrogatepass")) \
                    > self.PRIVATE_BTW_FILE_MAX_BYTES:
                raise ValueError("private btw state exceeds size limit")
            raw = json.loads(raw_text)
            if not isinstance(raw, dict) or len(raw) > self.PRIVATE_BTW_CAP:
                raise ValueError("private btw state has an invalid shape")
            for sid, entry in raw.items():
                if not isinstance(entry, dict):
                    raise ValueError("private btw state has an invalid entry")
                cwd = entry.get("cwd")
                created = entry.get("created_at", 0)
                if (
                    isinstance(sid, str)
                    and re.fullmatch(
                        r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}",
                        sid,
                    )
                    and isinstance(cwd, str) and "\x00" not in cwd
                    and len(cwd.encode("utf-8", "surrogatepass")) <= 4096
                    and isinstance(created, (int, float))
                ):
                    entries[sid] = {"cwd": cwd, "created_at": created}
                else:
                    raise ValueError("private btw state has an invalid entry")
        except FileNotFoundError:
            pass
        except Exception as exc:
            # This file is the durable privacy boundary for Claude fork_session
            # transcripts. Treating corruption as "no private sessions" would
            # publish every surviving fork on the next SessionList.
            raise RuntimeError(
                "private btw state is unreadable; refusing fail-open startup"
            ) from exc
        return entries

    def _persist_private_btw_sessions(
        self, entries: Optional[OrderedDict[str, dict]] = None,
    ) -> None:
        entries = self._private_btw_sessions if entries is None else entries
        path = self._private_btw_file()
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            payload = json.dumps(entries, separators=(",", ":"))
            if len(payload.encode("utf-8")) > self.PRIVATE_BTW_FILE_MAX_BYTES:
                raise ValueError("private btw state exceeds size limit")
            with tmp.open("w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            # Make the rename durable, not merely atomic in the page cache.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            raise RuntimeError("private btw state could not be persisted") from exc

    def _remember_private_btw(self, session_id: str, cwd: str) -> None:
        updated = OrderedDict(self._private_btw_sessions)
        updated[session_id] = {
            "cwd": cwd, "created_at": time.time(),
        }
        updated.move_to_end(session_id)
        # Reaching this bound means cleanup is persistently broken. Fail closed by
        # retaining the oldest tombstones and refusing to forget a private id.
        if len(updated) > self.PRIVATE_BTW_CAP:
            raise RuntimeError("private btw tombstone capacity exhausted")
        self._persist_private_btw_sessions(updated)
        self._private_btw_sessions = updated

    async def _delete_private_btw(
        self, session_id: str, cwd: str, *, forget: bool = True,
    ) -> bool:
        try:
            await asyncio.to_thread(delete_session, session_id, directory=cwd)
        except FileNotFoundError:
            pass  # already absent is the desired state
        except Exception as exc:
            log.warning("btw fork transcript delete failed", forked=session_id,
                        error=str(exc))
            return False
        if forget:
            updated = OrderedDict(self._private_btw_sessions)
            updated.pop(session_id, None)
            try:
                self._persist_private_btw_sessions(updated)
            except RuntimeError as exc:
                # The transcript is gone, so retaining a stale in-memory/on-disk
                # tombstone is safe. Never forget it only in RAM while disk still
                # claims the private fork exists.
                log.warning("private btw tombstone removal not persisted",
                            error_type=type(exc).__name__)
            else:
                self._private_btw_sessions = updated
        log.info("btw fork transcript deleted", forked=session_id)
        return True

    async def _cleanup_private_btw_sessions(self) -> None:
        for session_id, entry in list(self._private_btw_sessions.items()):
            await self._delete_private_btw(session_id, entry["cwd"])

    # ---- lifecycle ----

    async def run(self) -> None:
        self._cleanup_tmp()
        # A previous process may have died while a Claude /btw fork was live.
        # Remove its persisted private transcript before accepting any client
        # command or publishing SessionList.
        await self._cleanup_private_btw_sessions()
        # Bring up the relay first.  Claude is an optional engine: a missing CLI or
        # incompatible SDK must not prevent the wrapper from accepting a subsequent
        # `new_session(engine="codex")` command.
        await self.transport.start()
        try:
            bootstrap_sid = (
                self.cfg.resume_session_id
                or load_session_id(self.cfg.state_dir, self.cfg.cc_cwd)
            )
            ctx = await self._spawn(
                resume_id=bootstrap_sid, cwd=self.cfg.cc_cwd, bootstrap=True)
            if ctx is not None:
                self.focused_sid = ctx.key
                log.info("wrapper running", session_id=ctx.session_id,
                         key=self.focused_sid)
                # If the transport connected while bootstrap was still spawning, its
                # first hello described an empty pool. Re-announce the focused session;
                # when still disconnected this remains a harmless best-effort send.
                await self._on_transport_connected()
            else:
                log.warning("Claude bootstrap unavailable; continuing with empty pool")

            self._watch_task = asyncio.create_task(self._watch_loop())
            async for cmd in self.transport.incoming():
                try:
                    await self._process_command(cmd)
                except Exception:
                    log.exception("command handling failed", type=cmd.type)
        finally:
            if self._watch_task:
                self._watch_task.cancel()
                try:
                    await self._watch_task
                except asyncio.CancelledError:
                    pass
            await self.transport.stop()
            for c in list(self.sessions.values()):
                disconnected = False
                try:
                    await c.sdk.disconnect()
                    disconnected = True
                except Exception:
                    pass
                if c.btw and c.engine != "codex" and c.btw_real_id:
                    await self._delete_private_btw(
                        c.btw_real_id, c.cwd, forget=disconnected)

    async def _on_transport_connected(self) -> None:
        ctx = self._focused_ctx()
        await self.transport.send(Hello(
            role="wrapper",
            wrapper_generation=self.instance_id,
            cc_session_id=ctx.session_id if ctx else None,
            state=(ctx.state if ctx else "idle"),
            buffer_head_seq=(ctx.buffer.head_seq if ctx else 0),
            buffer_tail_seq=(ctx.buffer.tail_seq if ctx else 0),
        ))

    # ---- emit (per-ctx seq + buffer + best-effort send), serialized per ctx ----

    async def _emit_locked(self, ctx: SessionContext, msg) -> None:
        # Stamp routing before buffering so byte accounting includes the final
        # wire shape.  Every live and replayable /btw frame is owner-only; relay
        # treats an absent ``to`` as broadcast, so fail closed for an impossible
        # ownerless fork rather than leaking its contents.
        msg.sid = ctx.session_id or ctx.key
        if ctx.btw:
            if not ctx.owner_client_id:
                log.error("dropping frame for ownerless btw", sid=ctx.key,
                          type=getattr(msg, "type", None))
                return
            msg.to = ctx.owner_client_id
        if is_downstream(msg):
            msg.seq = ctx.next_seq()
            ctx.buffer.append(msg)
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

    def _command_seen(self, client_id: str, cmd_id: str) -> tuple[bool, tuple[object, ...]]:
        bucket = self._processed_commands.get(client_id)
        if bucket is None or cmd_id not in bucket:
            return False, ()
        bucket.move_to_end(cmd_id)
        self._processed_commands.move_to_end(client_id)
        return True, bucket[cmd_id]

    def _remember_command(self, client_id: str, cmd_id: str,
                          responses: tuple[object, ...] = ()) -> None:
        bucket = self._processed_commands.get(client_id)
        if bucket is None:
            bucket = OrderedDict()
            self._processed_commands[client_id] = bucket
        bucket[cmd_id] = responses
        bucket.move_to_end(cmd_id)
        self._processed_commands.move_to_end(client_id)
        while len(bucket) > self.COMMAND_IDS_PER_CLIENT:
            bucket.popitem(last=False)
        while len(self._processed_commands) > self.COMMAND_CLIENTS:
            self._processed_commands.popitem(last=False)

    def _rekey_cached_create_responses(self, old_key: str, session_id: str,
                                       cwd: str) -> None:
        """Keep a cached create response usable if id capture happened while its
        original Focus/ACK were lost. Replay re-key first, then focus the real id."""
        for bucket in self._processed_commands.values():
            for cmd_id, responses in list(bucket.items()):
                updated: list[object] = []
                changed = False
                for response in responses:
                    if (isinstance(response, SessionFocus)
                            and response.session_id == old_key):
                        updated.append(SessionRekey(
                            old_key=old_key,
                            session_id=session_id,
                            cwd=cwd,
                            sid=session_id,
                            to=response.to,
                        ))
                        updated.append(response.model_copy(deep=True, update={
                            "session_id": session_id,
                            "cwd": cwd,
                            "sid": session_id,
                        }))
                        changed = True
                    elif (not isinstance(response, Snapshot)
                          and getattr(response, "sid", None) == old_key):
                        # Snapshot deliberately stays temp-keyed so the following
                        # SessionRekey has a runtime to migrate. All later cached
                        # frames must target the real id after that migration.
                        updated.append(response.model_copy(
                            deep=True, update={"sid": session_id}))
                        changed = True
                    else:
                        updated.append(response)
                if changed:
                    bucket[cmd_id] = tuple(updated)

    async def _send_command_ack(self, client_id: str, cmd_id: str) -> None:
        await self.transport.send(CommandAck(
            cmd_id=cmd_id,
            client_id=client_id,
            to=client_id,
        ))

    def _refresh_cached_response(self, response):
        """Copy a cached one-shot response, refreshing volatile snapshots.

        A create command can be retried after its first response was lost while
        the newly-created turn is already running. Replaying the original idle
        Snapshot would roll the client back to idle, so derive current state from
        the resident context without re-executing the command.
        """
        replay = response.model_copy(deep=True)
        if isinstance(replay, Snapshot) and replay.sid:
            ctx = self._ctx_for(replay.sid)
            if ctx is not None:
                replay.state = ctx.buffer.latest_state() or ctx.state
                replay.tail_text = ctx.buffer.latest_tail_text()
                replay.cc_session_id = ctx.session_id
                replay.cwd = ctx.cwd
                replay.generation = self.instance_id
        return replay

    async def _process_command(self, cmd) -> None:
        """Deduplicate reliable client commands and ACK completed handlers.

        A business rejection that emits Error still returns normally and is
        acknowledged. An unexpected exception is deliberately neither remembered
        nor ACKed, so the client can retry instead of accepting partial failure.
        """
        client_id = getattr(cmd, "client_id", None)
        cmd_id = getattr(cmd, "cmd_id", None)
        reliable = is_reliable_command(cmd) and client_id and cmd_id
        seen, cached_responses = (
            self._command_seen(client_id, cmd_id) if reliable else (False, ()))
        if seen:
            if cmd.type in self.SAFE_RETRY_COMMANDS:
                # The original one-shot response may have died on the same link as
                # its ACK. Safe reads are intentionally re-run to recreate it.
                await self._handle(cmd)
                log.info("duplicate read command replayed", client_id=client_id,
                         cmd_id=cmd_id, type=cmd.type)
            else:
                for response in cached_responses:
                    replay = self._refresh_cached_response(response)
                    # A cached business error may originally have been broadcast
                    # with its turn. Retry it only to the command's origin.
                    if getattr(replay, "to", None) is None:
                        replay.to = client_id
                    await self.transport.send(replay)
                log.info("duplicate command suppressed", client_id=client_id,
                         cmd_id=cmd_id, type=cmd.type,
                         responses=len(cached_responses))
            await self._send_command_ack(client_id, cmd_id)
            return

        result = await self._handle(cmd)

        if reliable:
            candidates = result if isinstance(result, (tuple, list)) else (result,)
            responses = tuple(
                response.model_copy(deep=True)
                for response in candidates
                if getattr(response, "type", None)
            )
            self._remember_command(client_id, cmd_id, responses)
            await self._send_command_ack(client_id, cmd_id)

    async def _handle(self, cmd) -> None:
        rejected = await self._reject_nonowner_btw_command(cmd)
        if rejected is not None:
            return rejected
        t = cmd.type
        if t == "hello" and cmd.role == "client":
            await self._handle_client_hello(cmd)
        elif t == "query":
            return await self._handle_query(cmd)
        elif t == "interrupt":
            await self._handle_interrupt(cmd)
        elif t == "set_model":
            await self._handle_set_model(cmd)
        elif t == "set_effort":
            await self._handle_set_effort(cmd)
        elif t == "set_service_tier":
            await self._handle_set_service_tier(cmd)
        elif t == "open_btw":
            return await self._handle_open_btw(cmd)
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
        elif t == "get_models":
            await self._handle_get_models(cmd)
        elif t == "answer_question":
            await self._handle_answer_question(cmd)
        elif t == "list_sessions":
            await self._handle_list_sessions(cmd)
        elif t == "switch_session":
            return await self._handle_switch_session(cmd)
        elif t == "new_session":
            return await self._handle_new_session(cmd)
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
        # A fresh client (no cursor for a sid) gets exactly one lightweight
        # Snapshot for that session. A reconnecting client explicitly names the
        # sids it already knows and receives only seq > cursor from those rings.
        # This restores live tail loss without reviving the old all-session replay
        # flood. A concurrent turn may re-key sessions across the awaits below.
        supplied_cursors = getattr(cmd, "cursors", None)
        cursors = dict(supplied_cursors) if isinstance(supplied_cursors, dict) else {}
        supplied_generations = getattr(cmd, "generations", None)
        generations = (dict(supplied_generations)
                       if isinstance(supplied_generations, dict) else {})
        # Compatibility for the TUI/live scripts that predate per-session cursor
        # maps. It is necessarily scoped to the wrapper's focused session.
        legacy_cursor = getattr(cmd, "last_seq", None)
        focused = self._focused_ctx()
        if not cursors and isinstance(legacy_cursor, int) and focused is not None:
            focused_id = focused.session_id or focused.key
            if focused_id:
                cursors[focused_id] = legacy_cursor
        for old_key in list(cursors):
            alias = self._session_aliases.get(old_key)
            if alias is None:
                continue
            real_id = alias["session_id"]
            await self.transport.send(SessionRekey(
                old_key=old_key,
                session_id=real_id,
                cwd=alias["cwd"],
                sid=real_id,
                to=cmd.client_id,
                route_id=getattr(cmd, "route_id", None),
            ))
            old_cursor = cursors.pop(old_key)
            cursors[real_id] = max(cursors.get(real_id, 0), old_cursor)
            if old_key in generations:
                generations[real_id] = generations.pop(old_key)
            self._session_aliases.move_to_end(old_key)
        replayed = 0
        for key, ctx in list(self.sessions.items()):
            if ctx.btw and ctx.owner_client_id != cmd.client_id:
                continue  # ephemeral fork replay is private to its creating client
            sid = ctx.session_id or key
            tail = ctx.buffer.latest_tail_text()
            st = ctx.buffer.latest_state() or ctx.state
            async with ctx.emit_lock:
                if sid in cursors:
                    same_generation = generations.get(sid) == self.instance_id
                    frames = ctx.buffer.replay_from(
                        cursors[sid] if same_generation else 0,
                        cc_session_id=ctx.session_id,
                        state=st,
                        tail_text=tail,
                        cwd=ctx.cwd,
                        rebuild=not same_generation,
                        generation=self.instance_id,
                    )
                    replayed += 1
                else:
                    frames = [Snapshot(
                        cc_session_id=ctx.session_id,
                        state=st,
                        tail_text=tail,
                        cwd=ctx.cwd,
                        generation=self.instance_id,
                    )]
                    if st != "idle":
                        frames.extend(ctx.buffer.current_turn_replay(
                            generation=self.instance_id,
                            message_id=ctx.active_msg_id))
                for frame in frames:
                    # Never mutate a shared ring event with per-client routing.
                    await self.transport.send(frame.model_copy(
                        deep=True, update={
                            "to": cmd.client_id,
                            "sid": sid,
                            "route_id": getattr(cmd, "route_id", None),
                        }))
        log.info("client hello handled", client_id=cmd.client_id,
                 sessions=len(self.sessions), replayed=replayed)

    # ---- history: on-demand transcript read + EXTERNAL-append mirror ----

    EXTERNAL_TTL = 60.0     # a session stays read-only this long after the last external append
    OWN_WRITE_GRACE = 10.0  # appends this soon after one of OUR turns are ours, not external
    MIRROR_LIMIT = 60       # turns per mirrored push (matches the client's initial page)
    WATCH_MAX = 32          # cap on simultaneously watched transcripts

    def _ctx_by_sid(self, sid: str) -> Optional[SessionContext]:
        sid = self._resolve_session_alias(sid) or sid
        return self.sessions.get(sid) or next(
            (c for c in self.sessions.values() if c.session_id == sid), None)

    def _is_external(self, sid: str) -> bool:
        """This session's transcript was appended to by someone other than us, recently."""
        w = self._watch.get(sid)
        return bool(w) and (time.time() - w["external_ts"]) < self.EXTERNAL_TTL

    def _own_write(self, sid: str) -> bool:
        """True if WE plausibly produced the append: a turn is streaming, or one just
        finished (cc keeps writing for a moment after the result)."""
        ctx = self._ctx_by_sid(sid)
        if ctx is None:
            return False                       # not resident => we cannot have written it
        if ctx.state != "idle":
            return True
        return (time.time() - ctx.last_turn_end) < self.OWN_WRITE_GRACE

    def _resync_watch(self, sid: str) -> None:
        """Re-baseline a watched transcript's size after WE appended to it outside of
        a turn. rename_session/tag_session write an `operation` record straight into
        the .jsonl; without this the watcher would see that growth, call it external,
        and wrongly flag the user's own session read-only."""
        w = self._watch.get(sid)
        if not w:
            return
        try:
            w["size"] = os.path.getsize(w["path"])
        except OSError:
            pass

    def _watch_session(self, sid: str) -> None:
        """Start mirroring a session's transcript. Called when a client opens it.

        CLAUDE ONLY. Codex is deliberately excluded: its app-server flushes the rollout
        ASYNCHRONOUSLY, up to ~50s after turn/completed (measured: turn ended 11:19:11,
        rollout still growing at 11:20:01). Our own-write rule ("growth while no turn is
        running") therefore misfires on codex and would lock the user out of their OWN
        codex session for the whole external window. (Resuming a codex thread does NOT
        write — that part is fine; the post-turn flush is the problem.) The mirror
        exists for a native `claude` in the terminal, so this costs nothing.
        """
        if not sid or sid.startswith("tmp-") or sid in self._watch:
            return
        ctx = self._ctx_by_sid(sid)
        engine = ctx.engine if ctx else "claude"
        if engine == "codex":
            return
        path = transcript_path(sid)
        if not path:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if len(self._watch) >= self.WATCH_MAX:
            self._watch.pop(next(iter(self._watch)), None)   # drop the oldest
        self._watch[sid] = {"path": path, "size": size, "engine": engine,
                            "external_ts": 0.0, "flagged": False}
        log.info("watching transcript", session_id=sid, engine=engine, size=size)

    async def _watch_loop(self) -> None:
        """Mirror EXTERNAL appends to watched transcripts.

        A native `claude`/`codex` in the user's terminal owns its session and appends
        to the very .jsonl we read. Poll st_SIZE — NOT st_mtime: `claude --resume`
        touches mtime without writing a single byte (verified), so mtime would
        false-positive on every session we merely spawn. Growth we did not cause =>
        an external writer => rebuild the history, broadcast it, and mark the session
        read-only + stale (our resumed child's in-memory context has moved on)."""
        while True:
            try:
                await asyncio.sleep(1.5)
                for sid, w in list(self._watch.items()):
                    try:
                        size = await asyncio.to_thread(os.path.getsize, w["path"])
                    except OSError:
                        continue
                    if size < w["size"]:
                        w["size"] = size   # truncated/rotated: re-baseline, else a stale
                        continue           # larger baseline would swallow future appends
                    if size == w["size"]:
                        continue
                    grew, w["size"] = size - w["size"], size
                    if self._own_write(sid):
                        continue                      # our own turn wrote it; live stream covers it
                    w["external_ts"] = time.time()
                    w["flagged"] = True
                    ctx = self._ctx_by_sid(sid)
                    if ctx is not None:
                        ctx.external_ts = w["external_ts"]
                        ctx.needs_reload = True       # resumed child now holds a stale context
                    log.info("external transcript append -> mirroring", session_id=sid,
                             bytes=grew, resident=ctx is not None)
                    hist = await self._build_history(sid, limit=self.MIRROR_LIMIT)
                    hist.sid = sid                    # tag with the MIRRORED session, not the focused one
                    await self.transport.send(hist)   # broadcast; clients dedup by msg_id

                # UNLOCK pass: the watcher only pushes on GROWTH, so once the terminal
                # exits nothing would ever tell clients the session is writable again —
                # their `external` would stay stuck true forever. When the window
                # expires, push one History(external=false) to release the read-only UI.
                for sid, w in list(self._watch.items()):
                    if not w.get("flagged") or self._is_external(sid):
                        continue
                    w["flagged"] = False
                    ctx = self._ctx_by_sid(sid)
                    if ctx is not None:
                        ctx.external_ts = 0.0
                    log.info("external window expired -> unlocking", session_id=sid)
                    hist = await self._build_history(sid, limit=self.MIRROR_LIMIT)
                    hist.sid = sid
                    await self.transport.send(hist)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("transcript watch loop error")

    async def _build_history(self, sid: str, before=None, limit=None, cwd_hint=None) -> History:
        """Read a session's transcript and assemble ONE History frame. Shared by
        GetHistory (routed to the requester) and the watcher (broadcast on external
        append). No spawn, no ring buffer; the parse runs in a thread."""
        # Reading requires the session's own cwd (transcript lives under it).
        # Prefer a resident ctx's cwd, else the client-provided cwd, else default.
        ctx = self._ctx_by_sid(sid)
        in_progress = bool(ctx is not None and ctx.state != "idle")
        events: list = []
        mdl = None
        is_codex_hist = ctx is not None and ctx.engine == "codex"
        source_path = None
        try:
            source_path = await asyncio.to_thread(
                codex_rollout_path if is_codex_hist else transcript_path, sid)
            if (source_path
                    and await asyncio.to_thread(os.path.getsize, source_path)
                    > self.cfg.history_source_max_bytes):
                notice = Error(
                    code=ERR_INTERNAL,
                    message=("历史文件超过安全读取上限；请调大 "
                             "HISTORY_SOURCE_MAX_BYTES 或在终端中查看"),
                    sid=sid,
                )
                return History(
                    session_id=sid,
                    events=[notice.model_dump(mode="json")],
                    has_more=False,
                    before=before,
                    external=self._is_external(sid),
                    in_progress=in_progress,
                )
        except OSError:
            source_path = None
        if is_codex_hist:
            # Codex history lives in ~/.codex/sessions rollout files, not the
            # Claude transcript store.
            try:
                path = source_path or await asyncio.to_thread(codex_rollout_path, sid)
                if path:
                    events, mdl = await asyncio.to_thread(
                        codex_translate_history, path, self.cfg.tool_result_max)
            except Exception as e:
                log.warning("codex get_history failed", session_id=sid, error=str(e))
        else:
            directory = (ctx.cwd if ctx else None) or cwd_hint or self.cfg.cc_cwd
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
            if isinstance(ev, UserMsg):
                ev.prompt, restored_files = self._strip_attachment_paths(ev.prompt)
                if restored_files and not ev.files:
                    ev.files = restored_files
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

        # Prepend the model readout only on the newest page (initial load). For cc,
        # only announce Claude-branded models: the user's cc-switch may proxy a
        # Claude alias to a different upstream, whose raw name must not replace the
        # selected alias. Codex announces its real model (gpt-*).
        model_row = None
        if before is None and mdl and (is_codex_hist or mdl.startswith("claude-")):
            model_event = Model(model=mdl)
            model_event.sid = sid
            model_row = model_event.model_dump(mode="json")

        def make_history(selected: list[list], effective_start: int) -> History:
            payload: list[dict] = ([model_row.copy()] if model_row else [])
            for group in selected:
                payload.extend(ev.model_dump(mode="json") for ev in group)
            return History(
                session_id=sid,
                events=payload,
                has_more=effective_start > 0,
                before=before,
                oldest_id=(_tid(selected[0]) if selected else None),
                newest_id=(_tid(selected[-1]) if selected else None),
                external=self._is_external(sid),
                in_progress=in_progress,
            )

        # A History response is one WebSocket frame. Keep it below the transport
        # cap by reducing the number of oldest turns in this page; pagination can
        # retrieve those turns on the next request. This prevents a large transcript
        # from tearing down the wrapper<->relay connection.
        selected = page
        effective_start = start
        history = make_history(selected, effective_start)
        margin = min(64 * 1024, max(1024, self.cfg.ws_max_size_bytes // 16))
        frame_budget = max(1024, self.cfg.ws_max_size_bytes - margin)
        frame_size = len(history.model_dump_json().encode())
        if frame_size > frame_budget and len(selected) > 1:
            # Find the smallest number of oldest turns to drop. Re-serializing
            # after every single removal is quadratic for a legal transcript
            # containing many small turns; binary search bounds this to O(log n)
            # complete serializations while retaining the largest fitting page.
            low, high = 1, len(selected) - 1
            best_drop = len(selected) - 1
            best_history = make_history(selected[-1:], start + best_drop)
            while low <= high:
                drop = (low + high) // 2
                candidate = make_history(selected[drop:], start + drop)
                candidate_size = len(candidate.model_dump_json().encode())
                if candidate_size <= frame_budget:
                    best_drop = drop
                    best_history = candidate
                    high = drop - 1
                else:
                    low = drop + 1
            selected = selected[best_drop:]
            effective_start = start + best_drop
            history = best_history
            frame_size = len(history.model_dump_json().encode())

        if frame_size > frame_budget:
            # A single legacy turn may predate today's attachment limits. First
            # omit historical image bodies. If it is still too large, preserve a
            # bounded prompt + terminal marker and surface an explicit error event
            # instead of silently dropping the connection or pretending completeness.
            for row in history.events:
                if row.get("type") == "user_msg" and row.get("images"):
                    row["images"] = None
            frame_size = len(history.model_dump_json().encode())
            if frame_size > frame_budget:
                compact: list[dict] = ([model_row.copy()] if model_row else [])
                for row in history.events:
                    if row.get("type") == "user_msg":
                        kept = row.copy()
                        prompt_text = str(kept.get("prompt", ""))
                        kept["prompt"] = prompt_text[:32 * 1024]
                        kept["images"] = None
                        compact.append(kept)
                        break
                notice = Error(
                    code=ERR_INTERNAL,
                    message="该历史回合超过传输上限，已省略过大的回复或附件",
                )
                notice.sid = sid
                compact.append(notice.model_dump(mode="json"))
                terminal = next(
                    (row for row in reversed(history.events)
                     if row.get("type") == "turn_end"),
                    None,
                )
                if terminal:
                    compact.append(terminal)
                history.events = compact
                log.warning("oversized history turn compacted", session_id=sid,
                            frame_budget=frame_budget)
                frame_size = len(history.model_dump_json().encode())
        if frame_size > frame_budget:
            notice = Error(
                code=ERR_INTERNAL,
                message="该历史回合超过传输上限，无法在当前帧限制内显示",
            )
            notice.sid = sid
            history.events = [notice.model_dump(mode="json")]
            history.oldest_id = None
            history.newest_id = None
        return history

    async def _handle_get_history(self, cmd) -> None:
        """Client opened a session: return its history as ONE bulk frame, routed to
        the requester — like a web chat's GET /conversation. Opening it also starts
        MIRRORING its transcript, so appends made by a native `claude` in the user's
        terminal stream through to every client (read-only)."""
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        self._watch_session(sid)   # snapshot size before we read, so no append is missed
        hist = await self._build_history(
            sid, before=getattr(cmd, "before", None), limit=getattr(cmd, "limit", None),
            cwd_hint=getattr(cmd, "cwd", None))
        client_id = getattr(cmd, "client_id", None)
        if client_id:
            hist.to = client_id            # relay routes it to just this client
        hist.sid = sid                     # tag with THIS session (never the focused one)
        await self.transport.send(hist)
        log.info("history sent", session_id=sid, events=len(hist.events),
                 has_more=hist.has_more, before=bool(hist.before),
                 external=hist.external, client_id=client_id)

    async def _handle_query(self, cmd):
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            # sid given but not resident (spawn failed / evicted). Tag the error to
            # THAT session so the user sees it there — and never reroute the prompt
            # to a different session.
            error = Error(code=ERR_NOT_RUNNING,
                message="该会话未启动(可能启动失败),重新点进这个会话再发",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit_to_sid(sid, error)
            return error
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY, message="该会话正忙,先 interrupt",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit(ctx, error)
            return error
        if not cmd.prompt and not cmd.images and not cmd.files:
            error = Error(
                code=ERR_BAD_PROMPT, message="empty prompt",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit(ctx, error)
            return error
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            error = Error(
                code=ERR_BAD_PROMPT,
                message=f"invalid attachments: {attachment_error}",
                msg_id=getattr(cmd, "msg_id", None),
            )
            await self._emit(ctx, error)
            return error
        # claim synchronously so a concurrent query on THIS ctx can't race in
        ctx.interrupt_event.clear()
        ctx.interrupt_deadline = None
        ctx.active_msg_id = cmd.msg_id
        ctx.state = "running"
        async with ctx.emit_lock:
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
        # Set the deadline and wake the turn consumer before entering any await:
        # it may already be blocked in the queue.get() that began while running.
        ctx.interrupt_deadline = (
            asyncio.get_running_loop().time() + self.cfg.drain_timeout
        )
        ctx.state = "interrupting"
        ctx.interrupt_event.set()
        await self._emit(ctx, StateEvent(state="interrupting"))
        # A turn can still be reconnecting to apply effort/tier changes and may
        # not have submitted its query yet.  Serialize against that final launch
        # window: if the turn observes our event and aborts, it changes state to
        # idle; otherwise query() has returned and the new live turn is safe to
        # interrupt here.
        async with ctx.launch_lock:
            if ctx.state != "interrupting":
                return
            try:
                await ctx.sdk.interrupt()
            except Exception as e:
                log.exception("interrupt call failed", error=str(e))
                await self._emit(ctx, Error(
                    code=ERR_INTERNAL, message=f"interrupt failed: {e}"))
                # leave interrupting; the drain timeout will recover

    async def _handle_get_models(self, cmd) -> None:
        """Answer with the ENGINE's own catalog. codex's app-server knows exactly
        which reasoning levels each model takes; cc has no equivalent RPC, so we
        return nothing and the client keeps its static table."""
        engine = getattr(cmd, "engine", None) or "cc"
        models = await codex_catalog() if engine == "codex" else []
        default_model = None
        if engine == "codex":
            # config.toml's `model` = what a NEW session (and the terminal codex)
            # starts on. Only offer it if the catalog actually has it, so a stale
            # config can't preselect a model that isn't there.
            cfg_model = await asyncio.to_thread(codex_model, "")
            if cfg_model and any(m["id"] == cfg_model for m in models):
                default_model = cfg_model
        msg = Models(engine=engine, models=models, default_model=default_model)
        client_id = getattr(cmd, "client_id", None)
        if client_id:
            msg.to = client_id           # relay routes it to just this client
        await self.transport.send(msg)
        log.info("models sent", engine=engine, count=len(models),
                 default_model=default_model, client_id=client_id)

    async def _handle_set_model(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        try:
            await ctx.sdk.set_model(cmd.model)
            ctx.announced_model = cmd.model
            await self._emit(ctx, Model(model=cmd.model))
            if ctx.engine == "codex":
                # The new model may not support the level we're carrying (sol has
                # `ultra`, luna tops out at `max`). Clamp before the next turn/start
                # sends it — codex accepts any string here and only fails at the API.
                applied = await self._apply_codex_effort(ctx, ctx.announced_effort)
                if applied and applied != ctx.announced_effort:
                    ctx.announced_effort = applied
                    await self._emit(ctx, Effort(effort=applied))
        except Exception as e:
            log.exception("set_model failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_model failed: {e}"))

    async def _apply_codex_effort(self, ctx, effort: Optional[str]) -> Optional[str]:
        """Clamp `effort` to what ctx's codex model supports and apply it to the live
        handle. Returns the APPLIED level (may differ from the request); the caller
        announces it. Doesn't touch ctx.announced_effort.

        Session-scoped on purpose: nothing here writes config.toml. codex applies
        model/effort per turn and records them in the session's own rollout, so this
        session's pick can't leak into another session — or into the terminal's
        default. Mirrors codex's own thread/settings/update, which never touches
        config.toml either."""
        applied = await clamp_effort(getattr(ctx.sdk, "model", None), effort)
        if not applied:
            return applied
        # codex effort is a per-turn turn/start param, so keep applied_effort in
        # lockstep — _run_turn's "effort != applied" check must not force a respawn.
        ctx.sdk.effort = applied
        ctx.sdk.applied_effort = applied
        return applied

    async def _handle_set_effort(self, cmd) -> None:
        # cc: effort is a spawn-time flag (--effort), so we can't flip it on the live
        # subprocess — record it and let _run_turn respawn-with-resume lazily at the
        # next turn (an idle tweak then costs no tokens).
        # codex: effort is a per-turn turn/start param, so the live session honors it
        # immediately — no reconnect needed.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        if ctx.engine == "codex":
            applied = await self._apply_codex_effort(ctx, cmd.effort) or cmd.effort
            ctx.announced_effort = applied
            await self._emit(ctx, Effort(effort=applied))
            log.info("effort set", sid=ctx.session_id, effort=applied,
                     requested=cmd.effort, engine=ctx.engine)
            return
        ctx.sdk.effort = cmd.effort
        ctx.announced_effort = cmd.effort
        await self._emit(ctx, Effort(effort=cmd.effort))
        log.info("effort set", sid=ctx.session_id, effort=cmd.effort, engine=ctx.engine)

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
            if not ok:
                raise RuntimeError("Codex config.toml was not updated")

            # Fast is a global Codex config switch, not a per-session preference.
            # Every resident app-server must reconnect before its next turn or the
            # UI/config/actual process can disagree depending on which session runs.
            codex_contexts = [
                resident for resident in list(self.sessions.values())
                if resident.engine == "codex"
            ]
            for resident in codex_contexts:
                await resident.sdk.set_service_tier("fast" if on else None)
                resident.sdk.tier_dirty = True
            for resident in codex_contexts:
                await self._emit(resident, Fast(on=on))
            log.info("codex service tier set", sid=ctx.session_id, fast=on,
                     resident_handles=len(codex_contexts))
        except Exception as e:
            log.exception("set_service_tier failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_service_tier failed: {e}"))

    async def _send_btw_error(self, cmd, code: str, message: str) -> Error:
        """Send a one-request /btw rejection without polluting another session.

        The Error is returned so reliable-command dedupe can cache and replay the
        same terminal response if its first copy or ACK was lost.
        """
        client_id = getattr(cmd, "client_id", None)
        error = Error(
            code=code,
            message=message,
            request_id=cmd.request_id,
            sid=getattr(cmd, "sid", None),
            to=client_id,
        )
        if client_id:
            await self.transport.send(error)
        else:
            # ``to=None`` is a relay broadcast. An ownerless request must never
            # turn even a terminal /btw control response into a shared frame.
            log.warning("dropping unroutable btw error", code=code)
        return error

    async def _handle_open_btw(self, cmd):
        if not getattr(cmd, "client_id", None):
            return await self._send_btw_error(
                cmd, ERR_AUTH, "btw requires a bound client")
        parent = self._ctx_for(getattr(cmd, "sid", None))
        if parent is None:
            return await self._send_btw_error(
                cmd, ERR_NOT_RUNNING, "没有可 fork 的会话")
        if parent.btw:  # never fork a fork — fork its parent instead
            parent = self.sessions.get(parent.parent_sid) or next(
                (c for c in self.sessions.values() if c.session_id == parent.parent_sid), parent)
        try:
            btw = await self._spawn_btw(
                parent, owner_client_id=getattr(cmd, "client_id", None))
        except _BtwSpawnFailure as exc:
            return await self._send_btw_error(cmd, exc.code, exc.message)
        ev = BtwOpened(
            request_id=cmd.request_id,
            btw_sid=btw.key,
            parent_sid=parent.session_id or parent.key,
            engine=btw.engine,
        )
        ev.sid = btw.key
        cid = getattr(cmd, "client_id", None)
        if cid:
            ev.to = cid
        await self.transport.send(ev)
        # a fresh Snapshot so the requester builds a runtime for the fork's key.
        snap = Snapshot(
            cc_session_id=None, state="idle", tail_text="", cwd=btw.cwd,
            generation=self.instance_id)
        snap.sid = btw.key
        if cid:
            snap.to = cid
        await self.transport.send(snap)
        log.info("btw opened", btw_sid=btw.key, parent=parent.session_id, client_id=cid)
        # Both one-shot frames are required to reconstruct the fork after a lost
        # response. Reliable-command retries replay this pair without re-forking.
        return ev, snap

    async def _handle_close_btw(self, cmd) -> None:
        sid = getattr(cmd, "sid", None)
        ctx = self.sessions.get(sid) if sid else None
        if ctx is None or not ctx.btw:
            return
        self.sessions.pop(ctx.key, None)
        disconnected = False
        try:
            turn_task = ctx.turn_task
            if turn_task and not turn_task.done():
                turn_task.cancel()
                await asyncio.gather(turn_task, return_exceptions=True)
            await ctx.sdk.disconnect()
            disconnected = True
        except Exception as e:
            log.warning("btw close disconnect failed", error=str(e))
        # Codex forks are ephemeral (no rollout). Claude fork_session persists a
        # transcript under btw_real_id; keep its tombstone on deletion failure so
        # it stays hidden and cannot be cold-resumed.
        if ctx.engine != "codex" and ctx.btw_real_id:
            await self._delete_private_btw(
                ctx.btw_real_id, ctx.cwd, forget=disconnected)
        log.info("btw closed", btw_sid=sid)

    async def _handle_set_perm(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        allowed = (CODEX_PERMISSION_MODES if ctx.engine == "codex"
                   else CLAUDE_PERMISSION_MODES)
        if cmd.mode not in allowed:
            log.warning("invalid permission mode", sid=ctx.session_id,
                        engine=ctx.engine, mode=cmd.mode)
            await self._emit(ctx, Error(
                code=ERR_INTERNAL,
                message=(f"{ctx.engine} 不支持权限模式 {cmd.mode!r}; "
                         f"可选: {', '.join(sorted(allowed))}"),
            ))
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
        if ctx.engine != "claude" or mode not in CLAUDE_PERMISSION_MODES:
            raise ValueError(f"unsupported {ctx.engine} permission mode: {mode}")
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
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            error = Error(
                code=ERR_NOT_RUNNING,
                message="该会话未启动，无法读取代码差异",
                to=getattr(cmd, "client_id", None),
            )
            if sid:
                await self._emit_to_sid(sid, error)
            else:
                await self._emit_focused(error)
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
        # ask_id is an identity, not a downstream sequence.  Consuming next_seq
        # here would leave an invisible hole before _emit assigns AskUser.seq;
        # reconnect replay would then appear to have lost a frame.
        ask_id = f"ask-{uuid4().hex}"
        # Validate model-originated text before registering a pending Future.
        # Otherwise a malformed/oversized AskUser raises during emit and leaves
        # an unreachable entry in pending_asks for the life of the session.
        event = AskUser(ask_id=ask_id, question=question, options=options)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        ctx.pending_asks[ask_id] = fut
        await self._emit(ctx, event)
        log.info("ask_user emitted", sid=ctx.session_id, ask_id=ask_id, options=len(options))
        try:
            return await asyncio.wait_for(fut, timeout=30 * 60)
        except asyncio.TimeoutError:
            log.warning("ask_user timed out", ask_id=ask_id)
            return "(用户未回答，已超时)"
        finally:
            ctx.pending_asks.pop(ask_id, None)

    async def _on_codex_approval(self, ctx: SessionContext, method: str,
                                 params: dict) -> str:
        """Bridge Codex app-server approvals to the existing AskUser flow.

        The callback returns the current app-server schema's exact decision enum.
        Anything unexpected fails closed; CodexHandle independently enforces a
        shorter timeout than the generic ask-user tool.
        """
        def short(value, limit: int = 1200) -> str:
            text = str(value or "").strip()
            return text if len(text) <= limit else text[:limit] + "…"

        command_request = method in {
            "item/commandExecution/requestApproval", "execCommandApproval",
        }
        lines = ["Codex 请求执行命令：" if command_request else "Codex 请求修改文件："]
        if command_request:
            command = params.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            if not command:
                command = params.get("parsedCmd") or params.get("itemId")
            if command:
                lines.append(short(command))
        else:
            changes = params.get("fileChanges")
            if isinstance(changes, dict) and changes:
                paths = list(changes)[:12]
                lines.append("\n".join(short(path, 240) for path in paths))
                if len(changes) > len(paths):
                    lines.append(f"另有 {len(changes) - len(paths)} 个文件")
            elif params.get("itemId"):
                lines.append(f"变更项 {short(params['itemId'], 240)}")
        if params.get("cwd"):
            lines.append(f"目录：{short(params['cwd'], 500)}")
        if params.get("grantRoot"):
            lines.append(f"授权目录：{short(params['grantRoot'], 500)}")
        if params.get("reason"):
            lines.append(f"原因：{short(params['reason'], 500)}")

        options = [
            {"label": "允许一次", "ds": "仅批准这一次操作"},
            {"label": "本会话允许", "ds": "本会话后续同类操作不再询问"},
            {"label": "拒绝", "ds": "拒绝这次操作"},
            {"label": "取消", "ds": "取消当前操作或回合"},
        ]
        answer = await self._on_ask(ctx, "\n".join(lines), options)
        return {
            "允许一次": "accept",
            "本会话允许": "acceptForSession",
            "拒绝": "decline",
            "取消": "cancel",
        }.get(answer, "decline")

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
        max_bytes = max(64 * 1024, min(4 * 1024 * 1024,
                                       self.cfg.ws_max_size_bytes // 2))
        if not file:
            return await self._bounded_process_output(
                ("git", "-C", cwd, "diff", "--no-ext-diff", "--no-textconv",
                 "HEAD"), max_bytes)

        root_text = await self._bounded_process_output(
            ("git", "-C", cwd, "rev-parse", "--show-toplevel"), 4096)
        root = os.path.realpath(root_text.strip().splitlines()[0] if root_text.strip() else cwd)
        candidate = os.path.abspath(
            os.path.expanduser(file) if os.path.isabs(os.path.expanduser(file))
            else os.path.join(cwd, os.path.expanduser(file)))
        # Resolve parent symlinks, but not the final component: a tracked symlink
        # can be diffed safely by git, while a path through a symlinked directory
        # must not escape the repository.
        parent = os.path.realpath(os.path.dirname(candidate))
        contained = os.path.join(parent, os.path.basename(candidate))
        try:
            if os.path.commonpath((root, contained)) != root:
                raise ValueError("diff path is outside the session repository")
        except ValueError as exc:
            raise ValueError("diff path is outside the session repository") from exc
        rel_file = os.path.relpath(contained, root)

        diff = await self._bounded_process_output(
            ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
             "HEAD", "--", rel_file), max_bytes)
        if diff.strip():
            return diff

        # An empty tracked-file diff means "unchanged", not "show its complete
        # contents". Reject untracked special files even when `git ls-files
        # --others` omits them; an explicit FIFO/device must never be opened by a
        # later no-index diff.
        try:
            file_stat = os.lstat(contained)
        except OSError:
            return ""
        if not stat.S_ISREG(file_stat.st_mode):
            tracked = await self._bounded_process_output(
                ("git", "-C", root, "ls-files", "--stage", "--", rel_file),
                64 * 1024,
            )
            if tracked:
                return ""  # unchanged tracked symlink
            raise ValueError("untracked diff target must be a regular file")
        # Only an actual, non-ignored untracked regular file gets no-index.
        untracked = await self._bounded_process_output(
            ("git", "-C", root, "ls-files", "--others", "--exclude-standard",
             "--", rel_file), 64 * 1024)
        if not untracked:
            return ""
        source_cap = getattr(self.cfg, "history_source_max_bytes", 64 * 1024 * 1024)
        if file_stat.st_size > source_cap:
            raise ValueError("untracked diff target exceeds the source size limit")
        return await self._bounded_process_output(
            ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
             "--no-index", "--", "/dev/null", rel_file),
            max_bytes,
        )

    @staticmethod
    async def _bounded_process_output(
        argv: tuple[str, ...], max_bytes: int, timeout: float = 10.0,
    ) -> str:
        """Capture stdout up to a hard byte limit without buffering stderr."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert proc.stdout is not None
        chunks: list[bytes] = []
        total = 0
        truncated = False

        async def read_stdout() -> None:
            nonlocal total, truncated
            while True:
                chunk = await proc.stdout.read(min(64 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                remaining = max_bytes - total
                if len(chunk) > remaining:
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)

        async def discard_stdout_and_wait() -> None:
            """Reap without asking communicate() to accumulate residual stdout."""
            async def discard_stdout() -> None:
                while await proc.stdout.read(64 * 1024):
                    pass

            await asyncio.gather(proc.wait(), discard_stdout())

        timed_out = False

        def stop_group(sig: signal.Signals) -> None:
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass

        reaped = False
        try:
            try:
                await asyncio.wait_for(read_stdout(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
            if timed_out:
                stop_group(signal.SIGKILL)
            elif truncated:
                stop_group(signal.SIGTERM)
            try:
                # Discard residual bytes incrementally. communicate() would collect
                # them all and defeat the hard output cap while SIGTERM is draining.
                await asyncio.wait_for(discard_stdout_and_wait(), timeout=2.0)
            except asyncio.TimeoutError:
                stop_group(signal.SIGKILL)
                await discard_stdout_and_wait()
            reaped = True
        finally:
            if not reaped:
                stop_group(signal.SIGKILL)
                await discard_stdout_and_wait()
        if timed_out:
            raise asyncio.TimeoutError("diff command exceeded its time limit")
        text = b"".join(chunks).decode(errors="replace")
        if truncated:
            text += "\n\n[diff truncated at transport safety limit]\n"
        return text

    # ---- sessions (list / switch / new) ----

    async def _handle_list_sessions(self, cmd) -> None:
        engine = getattr(cmd, "engine", "claude")
        if engine == "codex":
            await self._list_codex_sessions(cmd)
            return
        # Claude may create the fork transcript before its init/session id reaches
        # our turn consumer. Until capture durably tombstones that real id, scanning
        # the global session store could publish it to another client. Fail closed
        # for this one requester; never broadcast a control-plane privacy error.
        if any(
            ctx.btw and ctx.engine != "codex" and not ctx.btw_real_id
            for ctx in self.sessions.values()
        ):
            client_id = getattr(cmd, "client_id", None)
            if client_id:
                await self.transport.send(Error(
                    code=ERR_BUSY,
                    message="临时 btw 会话正在初始化，请稍后刷新会话列表",
                    to=client_id,
                ))
            log.warning("Claude session list withheld during btw id capture",
                        client_id=client_id)
            return
        try:
            infos = await asyncio.to_thread(list_sessions, limit=200)
            blocked = await asyncio.to_thread(self._bg_blocked_session_ids)
            private_btw_ids = set(self._private_btw_sessions)
            resident_ids = {c.session_id for c in self.sessions.values() if c.session_id}
            resident_state = {c.session_id: c.state for c in self.sessions.values() if c.session_id}
            sessions = [
                SessionInfo(
                    session_id=i.session_id,
                    summary=(i.summary or (i.custom_title if hasattr(i, "custom_title") else None) or "")[:500] or None,
                    last_modified=str(i.last_modified) if i.last_modified else None,
                    first_prompt=(i.first_prompt or "")[:2000] or None,
                    git_branch=(i.git_branch or "")[:500] or None,
                    cwd=(i.cwd or "")[:4096] or None,
                    tag=(i.tag or "")[:128] or None,
                    state=resident_state.get(i.session_id),
                )
                for i in infos
                if i.session_id not in blocked
                and i.session_id not in private_btw_ids
            ]
            await self.transport.send(SessionList(
                engine="claude",
                sessions=sessions,
                to=getattr(cmd, "client_id", None),
            ))
            log.info("listed sessions", count=len(sessions), resident=len(resident_ids),
                     client_id=getattr(cmd, "client_id", None))
        except Exception as e:
            log.exception("list_sessions failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_sessions failed: {e}"))

    async def _list_codex_sessions(self, cmd) -> None:
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
            await self.transport.send(SessionList(
                engine="codex",
                sessions=sessions,
                to=getattr(cmd, "client_id", None),
            ))
            log.info("listed codex sessions", count=len(sessions),
                     client_id=getattr(cmd, "client_id", None))
        except Exception as e:
            log.exception("list_codex_sessions failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_codex_sessions failed: {e}"))

    async def _handle_switch_session(self, cmd) -> None:
        # Focus change — NO disconnect. If the session is already resident, just
        # focus it (its turn keeps running in the background). If not resident,
        # spawn (resume) it. The previously-focused session is NOT interrupted.
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
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
                error = Error(code=ERR_CC_CRASH,
                    message="会话启动失败:可能是 codex 配置/后端问题(见服务端日志)")
                await self._emit_to_sid(sid, error)
                return error
        self.focused_sid = ctx.key
        # A newly-spawned session isn't tracked by the client yet — send its
        # snapshot + full replay so the client builds a runtime for it (else the
        # client would show an empty/wrong view).
        snap = None
        if newly_spawned:
            # Build the client's runtime for a freshly-resumed session with a
            # lightweight Snapshot (state/cwd/id); its HISTORY arrives via the
            # client's GetHistory request — no full buffer replay (that was a flood).
            snap = Snapshot(cc_session_id=ctx.session_id,
                            state=ctx.buffer.latest_state() or ctx.state,
                            tail_text=ctx.buffer.latest_tail_text(), cwd=ctx.cwd,
                            generation=self.instance_id)
            await self._emit(ctx, snap)
        focus = SessionFocus(
            session_id=ctx.session_id or self.focused_sid or sid, cwd=ctx.cwd)
        await self._emit(ctx, focus)
        cached_responses = [snap, focus] if snap is not None else [focus]
        # Seed the chips on entering a codex session so they're right before the first
        # turn. Fast is global (config.toml); model/effort are per-session (restored
        # from the rollout in _spawn). Without the Model frame the composer falls back
        # to the engine's first model, so a luna session came up labelled "Sol".
        if ctx.engine == "codex":
            fast_event = Fast(on=codex_fast_enabled())
            await self._emit(ctx, fast_event)
            cached_responses.append(fast_event)
            ctx.announced_perm = ctx.sdk.approval
            perm_event = Perm(mode=ctx.sdk.approval)
            await self._emit(ctx, perm_event)
            cached_responses.append(perm_event)
            if ctx.sdk.model:
                ctx.announced_model = ctx.sdk.model
                model_event = Model(model=ctx.sdk.model)
                await self._emit(ctx, model_event)
                cached_responses.append(model_event)
            if ctx.sdk.effort:
                ctx.announced_effort = ctx.sdk.effort
                effort_event = Effort(effort=ctx.sdk.effort)
                await self._emit(ctx, effort_event)
                cached_responses.append(effort_event)
        return tuple(cached_responses)

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
            if ctx.btw_real_id == sid:
                return
            if ctx.engine == "codex":
                ctx.btw_real_id = sid
                return
            try:
                # Durably hide the real Claude transcript before publishing it
                # even into the live ctx. A crash after this point remains safe.
                self._remember_private_btw(sid, ctx.cwd)
            except Exception as persist_error:
                # Keep a live guard while we fail-stop and delete the fork. This
                # entry may not be durable, so the private session is not allowed
                # to continue running after the persistence failure.
                ctx.btw_real_id = sid
                self._private_btw_sessions[sid] = {
                    "cwd": ctx.cwd, "created_at": time.time(),
                }
                self.sessions.pop(ctx.key, None)
                log.error("private btw persistence failed; terminating fork",
                          error_type=type(persist_error).__name__)
                disconnected = False
                try:
                    await ctx.sdk.disconnect()
                    disconnected = True
                except Exception as disconnect_error:
                    log.warning("failed private btw disconnect failed",
                                error_type=type(disconnect_error).__name__)
                deleted = False
                try:
                    await asyncio.to_thread(
                        delete_session, sid, directory=ctx.cwd)
                except FileNotFoundError:
                    deleted = True
                except Exception as delete_error:
                    log.error("failed private btw transcript could not be deleted",
                              error_type=type(delete_error).__name__)
                    # A transient state-dir failure may have cleared by now. Retry
                    # the tombstone write; if it still fails, RAM remains fail-closed
                    # for the lifetime of this wrapper process.
                    try:
                        self._persist_private_btw_sessions()
                    except RuntimeError:
                        pass
                else:
                    deleted = True
                if disconnected and deleted:
                    # No live writer and no transcript remain. A stale tombstone may
                    # still exist on disk if replace succeeded before fsync failed;
                    # that is harmless and startup cleanup will remove it.
                    self._private_btw_sessions.pop(sid, None)
                raise RuntimeError(
                    "private btw state persistence failed; fork terminated"
                ) from persist_error
            ctx.btw_real_id = sid
            return
        old_key = ctx.key
        ctx.session_id = sid
        if ctx.engine != "codex":
            save_session_id(self.cfg.state_dir, ctx.cwd, sid)
        if old_key and old_key != sid:
            self._remember_session_alias(old_key, sid, ctx.cwd)
            self.sessions.pop(old_key, None)
            self.sessions[sid] = ctx
            ctx.key = sid
            if self.focused_sid == old_key:
                self.focused_sid = sid
            await self._emit(ctx, SessionRekey(old_key=old_key, session_id=sid, cwd=ctx.cwd))
            self._rekey_cached_create_responses(old_key, sid, ctx.cwd)
        log.info("captured cc session id", sid=sid, focus_followed=(self.focused_sid == sid))

    async def _handle_new_session(self, cmd) -> None:
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            error = Error(
                code=ERR_BAD_PROMPT,
                message=f"invalid attachments: {attachment_error}",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = await self._spawn(resume_id=None, cwd=getattr(cmd, "cwd", None),
                                engine=getattr(cmd, "engine", "claude"),
                                model=getattr(cmd, "model", None),
                                effort=getattr(cmd, "effort", None))
        if ctx is None:
            error = Error(
                code=ERR_CC_CRASH,
                message="新会话启动失败，请检查工作目录和引擎配置",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        self.focused_sid = ctx.key
        # Establish the temp runtime's wrapper lifetime before any other
        # sequenced frame. Otherwise its cursor has no paired generation and a
        # normal transport reconnect is mistaken for a wrapper restart.
        snap = Snapshot(
            cc_session_id=None,
            state=ctx.state,
            tail_text=ctx.buffer.latest_tail_text(),
            cwd=ctx.cwd,
            generation=self.instance_id,
        )
        await self._emit(ctx, snap)
        # session_id is None until captured in _run_turn; use the pool (temp) key
        # as the focus id — the client migrates to the real sid on capture.
        focus = SessionFocus(
            session_id=self.focused_sid,
            cwd=ctx.cwd,
            request_id=getattr(cmd, "request_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self._emit(ctx, focus)
        # _spawn records explicit picks as already announced. Emit them now that
        # SessionFocus has created the temp-keyed client runtime; otherwise the
        # removed client-side pending-query effect would leave its chips stale.
        cached_responses = [snap, focus]
        if getattr(cmd, "model", None):
            model_event = Model(model=cmd.model)
            await self._emit(ctx, model_event)
            cached_responses.append(model_event)
        if getattr(cmd, "effort", None):
            effort_event = Effort(effort=cmd.effort)
            await self._emit(ctx, effort_event)
            cached_responses.append(effort_event)
        # A fresh Codex runtime otherwise inherits the web's Claude-only fallback
        # (`bypassPermissions`). Claude already uses that fallback as its real
        # spawn-time mode, so only Codex needs an explicit initial frame here.
        if ctx.engine == "codex":
            ctx.announced_perm = ctx.sdk.approval
            perm_event = Perm(mode=ctx.sdk.approval)
            await self._emit(ctx, perm_event)
            cached_responses.append(perm_event)

        prompt = getattr(cmd, "prompt", None)
        images = getattr(cmd, "images", None)
        files = getattr(cmd, "files", None)
        if prompt is not None or images or files:
            # Explicit sid is the atomicity guarantee: even if focus changes while
            # the create response is in flight, the first turn targets this ctx.
            query_result = await self._handle_query(Query(
                sid=ctx.key,
                prompt=prompt or "",
                msg_id=cmd.msg_id,
                images=images,
                files=files,
            ))
            if getattr(query_result, "type", None):
                cached_responses.append(query_result)
        return tuple(cached_responses)

    async def _handle_rename_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if await self._is_codex_session(sid):
            await self._emit_to_sid(sid, Error(
                code=ERR_INTERNAL,
                message="当前 Codex app-server 不支持重命名会话",
            ))
            return
        try:
            await asyncio.to_thread(rename_session, sid, cmd.title)
            # our own append -> re-baseline, else the watcher calls it an external write
            self._resync_watch(sid)
            log.info("session renamed", session_id=sid,
                     title_length=len(cmd.title))
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("rename_session failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"rename failed: {e}"))

    async def _handle_archive_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if await self._is_codex_session(sid):
            await self._emit_to_sid(sid, Error(
                code=ERR_INTERNAL,
                message="当前 Codex app-server 不支持归档会话",
            ))
            return
        try:
            tag = "archived" if cmd.archived else None
            await asyncio.to_thread(tag_session, sid, tag)
            # our own append -> re-baseline (see _resync_watch)
            self._resync_watch(sid)
            log.info("session archive toggled", session_id=sid, archived=cmd.archived)
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("archive_session failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"archive failed: {e}"))

    async def _is_codex_session(self, session_id: str) -> bool:
        """Capability guard for commands whose wire shape predates `engine`.

        Resident contexts are authoritative. For a cold session, a matching Codex
        rollout identifies it without ever handing the id to the Claude SDK.
        """
        ctx = self._ctx_by_sid(session_id)
        if ctx is not None:
            return ctx.engine == "codex"
        try:
            return bool(await asyncio.to_thread(codex_rollout_path, session_id))
        except Exception as exc:
            log.warning("codex session capability check failed",
                        session_id=session_id, error=str(exc))
            return False

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
            with os.scandir(base) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=True):
                        dirs.append({"name": entry.name[:255], "path": entry.path[:4096]})
                        if len(dirs) >= 1000:
                            break
            dirs.sort(key=lambda item: item["name"])
        except PermissionError:
            pass
        return base, parent, dirs

    @staticmethod
    def _bg_blocked_session_ids() -> set[str]:
        ids: set[str] = set()
        jobs = os.path.expanduser("~/.claude/jobs")
        if not os.path.isdir(jobs):
            return ids
        try:
            with os.scandir(jobs) as entries:
                for index, entry in enumerate(entries):
                    if index >= WrapperMachine.BG_JOB_SCAN_MAX:
                        log.warning("Claude job scan capped", entries=index)
                        break
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        state_path = os.path.join(entry.path, "state.json")
                        state_info = os.stat(state_path, follow_symlinks=False)
                        if (not stat.S_ISREG(state_info.st_mode)
                                or state_info.st_size
                                > WrapperMachine.BG_JOB_STATE_MAX_BYTES):
                            continue
                        with open(state_path) as stream:
                            raw = stream.read(
                                WrapperMachine.BG_JOB_STATE_MAX_BYTES + 1)
                        if len(raw) > WrapperMachine.BG_JOB_STATE_MAX_BYTES:
                            continue
                        state = json.loads(raw)
                    except Exception:
                        # One malformed/racy job record must not abort the bounded
                        # scan or hide every other active-session marker.
                        continue
                    if (isinstance(state, dict)
                            and state.get("state") != "done"
                            and isinstance(state.get("sessionId"), str)
                            and 1 <= len(state["sessionId"]) <= 128):
                        ids.add(state["sessionId"])
        except OSError:
            pass
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
        if resume_id and resume_id in self._private_btw_sessions:
            log.warning("refusing cold resume of private btw transcript",
                        session_id=resume_id)
            await self._emit_to_sid(resume_id, Error(
                code=ERR_AUTH,
                message="临时 btw 会话不可恢复",
            ))
            return None
        # Claude's CLI/version preflight belongs to Claude spawn, not wrapper
        # process startup.  Keep it before cap eviction: an unavailable optional
        # engine must not evict a healthy resident Codex session and then fail.
        if engine != "codex":
            try:
                SdkHandle.preflight(self.cfg.claude_bin)
            except Exception as exc:
                log.warning("Claude preflight failed; engine unavailable",
                            error=str(exc))
                await self._emit_to_sid(resume_id, Error(
                    code=ERR_CC_CRASH,
                    message=f"Claude 引擎不可用: {exc}",
                ))
                return None

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
        if engine == "codex":
            # A resumed codex session's model/effort live in ITS OWN rollout (codex
            # writes a `turn_context` per turn). CodexHandle seeds them from
            # config.toml, which is a single GLOBAL default — so resuming used to
            # silently drag a session the user had switched to luna back to sol, and
            # in multi-session one session's pick leaked into another.
            if resume_id and (not model or not effort):
                prev = await asyncio.to_thread(
                    codex_session_settings, resume_id,
                    self.cfg.history_source_max_bytes)
                model = model or prev.get("model")
                effort = effort or prev.get("effort")
            if model:
                sdk.model = model
                # A stale client can ask for a level this model doesn't have (it used
                # to offer `minimal`, and `ultra` on luna). codex takes any string here
                # and only fails inside the model API, so clamp against the real catalog.
                effort = await clamp_effort(model, effort)
        if effort:
            sdk.effort = effort
            sdk.applied_effort = effort
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
        else:
            ctx.sdk.approval_callback = (
                lambda method, params: self._on_codex_approval(
                    ctx, method, params))

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
            ctx.announced_perm = (ctx.sdk.approval if engine == "codex"
                                  else "bypassPermissions")
            await self._emit(ctx, Perm(mode=ctx.announced_perm))
        log.info("session spawned", resume=resume_id, cwd=target_cwd, key=key,
                 resident=len(self.sessions))
        return ctx

    async def _spawn_btw(
        self, parent: SessionContext, owner_client_id: Optional[str] = None,
    ) -> SessionContext:
        """Spawn an ephemeral /btw fork of `parent`: a throwaway side-session that
        inherits the parent's context (codex thread/fork · cc fork_session) and
        streams under a stable `btw-<uuid>` key. Never persisted / listed / focused;
        discarded on close. Its turns reuse the normal _run_turn path."""
        if not owner_client_id:
            raise _BtwSpawnFailure(ERR_AUTH, "btw requires a bound client")
        pending_private_forks = sum(
            1 for resident in self.sessions.values()
            if resident.btw and resident.engine != "codex"
            and not resident.btw_real_id
        )
        if (len(self._private_btw_sessions) + pending_private_forks
                >= self.PRIVATE_BTW_CAP):
            raise _BtwSpawnFailure(
                ERR_BUSY, "btw 隐私清理队列已满，请先检查 wrapper 日志")
        parent_id = parent.session_id
        if not parent_id:
            raise _BtwSpawnFailure(
                ERR_INTERNAL, "这个会话还没有上下文,先发一条消息再开 btw")
        engine = parent.engine
        if engine != "codex":
            try:
                SdkHandle.preflight(self.cfg.claude_bin)
            except Exception as exc:
                log.warning("Claude preflight failed for btw", error=str(exc))
                raise _BtwSpawnFailure(
                    ERR_CC_CRASH, f"Claude 引擎不可用: {exc}") from exc
        # btw counts toward the cap; evict an idle, non-focused, non-btw victim.
        if len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((k for k, c in self.sessions.items()
                           if k != self.focused_sid and c.state == "idle" and not c.btw), None)
            if victim is None:
                raise _BtwSpawnFailure(ERR_BUSY, "会话已满,先关闭一个再开 btw")
            vc = self.sessions.pop(victim)
            try:
                await vc.sdk.disconnect()
            except Exception:
                pass
            log.info("evicted idle session for btw", key=victim)
        sdk = CodexHandle(self.cfg, cwd=parent.cwd) if engine == "codex" else SdkHandle(self.cfg)
        # /btw is a quick side question — run the fork at LOW effort so the first
        # reply is snappy (the parent's own effort can be high/xhigh, which makes a
        # context-inheriting fork slow). Applied at connect (cc) / per-turn (codex).
        sdk.effort = "low"
        ctx = SessionContext(
            session_id=None, sdk=sdk,
            buffer=RingBuffer(self.cfg.ring_max_events, self.cfg.ring_max_bytes),
            cwd=parent.cwd, engine=engine, btw=True, parent_sid=parent_id,
            owner_client_id=owner_client_id)
        if engine != "codex":
            ctx.sdk.ask_server = make_ask_server(
                lambda q, o: self._on_ask(ctx, q, o),
                lambda m: self._on_set_mode(ctx, m),
            )
        else:
            ctx.sdk.approval = parent.sdk.approval
            ctx.sdk.approval_callback = (
                lambda method, params: self._on_codex_approval(
                    ctx, method, params))
        try:
            await ctx.sdk.connect(resume_id=parent_id, cwd=parent.cwd, fork=True)
        except Exception as e:
            log.exception("btw fork connect failed", error=str(e))
            raise _BtwSpawnFailure(ERR_CC_CRASH, f"btw fork 失败: {e}") from e
        key = f"btw-{uuid4().hex}"
        self.sessions[key] = ctx
        ctx.key = key
        log.info("btw fork spawned", parent=parent_id, key=key, engine=engine,
                 fork_thread=getattr(ctx.sdk, "thread_id", None))
        return ctx

    async def _load_history(self, ctx: SessionContext, session_id: Optional[str]) -> None:
        if not session_id or ctx.engine == "codex":
            return  # codex history replay (rollout files) is a later feature
        path = transcript_path(session_id)
        try:
            if path and os.path.getsize(path) > self.cfg.history_source_max_bytes:
                log.warning("history preload skipped: source too large",
                            session_id=session_id)
                return
        except OSError:
            pass
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
        for event in events:
            if isinstance(event, UserMsg):
                event.prompt, restored_files = self._strip_attachment_paths(event.prompt)
                if restored_files and not event.files:
                    event.files = restored_files
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
        async def during_drain():
            deadline = ctx.interrupt_deadline
            remaining = (self.cfg.drain_timeout if deadline is None else
                         deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                try:
                    return queue.get_nowait()
                except asyncio.QueueEmpty as exc:
                    raise asyncio.TimeoutError from exc
            return await asyncio.wait_for(queue.get(), timeout=remaining)

        if ctx.state == "interrupting":
            return await during_drain()

        # A plain `await queue.get()` cannot notice a later interrupt. Race it
        # against the per-context event; if the event wins, keep waiting on the
        # queue only until the already-established absolute drain deadline.
        get_task = asyncio.create_task(queue.get())
        interrupt_task = asyncio.create_task(ctx.interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                (get_task, interrupt_task), return_when=asyncio.FIRST_COMPLETED)
            if get_task in done:
                return get_task.result()
            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
            return await during_drain()
        finally:
            for task in (get_task, interrupt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, interrupt_task, return_exceptions=True)

    def _stash_files(self, prompt: str, files: list, temp_dir: str,
                     engine: str = "claude") -> str:
        """Write validated files to a private per-turn directory. cc reads
        the `@path` convention; codex has no `@` layer over the app-server and
        ignores `mention` items (verified), but reads a plain path via its tools —
        so codex gets an explicit 'read these files' block with bare paths."""
        paths = []
        for i, f in enumerate(files):
            fn = f.get("filename") or f"file-{i}"
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(fn))
            safe = safe.strip(".") or f"file-{i}"
            path = os.path.join(temp_dir, f"{i:02d}-{safe}")
            data = decode_attachment(f.get("data", ""))
            with open(path, "xb") as fp:
                fp.write(data)
            os.chmod(path, 0o600)
            paths.append(path)
            log.info("attachment stashed", index=i, bytes=len(data))
        if engine == "codex":
            block = "[用户附件,请用工具读取以下文件]:\n" + "\n".join(paths)
        else:
            block = " ".join(f"@{p}" for p in paths)
        return (prompt + "\n\n" if prompt else "") + block

    @staticmethod
    def _strip_attachment_paths(prompt: str) -> tuple[str, list[dict[str, str]]]:
        """Remove expired private temp paths from transcript-facing history."""
        path_re = re.compile(
            r"/\S*cc-remote-turn-[A-Za-z0-9_-]+/\d{2}-([^\s]+)")
        marker = "\n\n[用户附件,请用工具读取以下文件]:\n"
        if marker in prompt:
            original, block = prompt.rsplit(marker, 1)
            names = [match.group(1) for match in path_re.finditer(block)]
            if names:
                return original, [{"filename": name} for name in names]

        matches = list(re.finditer(
            r"@(?P<path>/\S*cc-remote-turn-[A-Za-z0-9_-]+/\d{2}-(?P<name>[^\s]+))",
            prompt,
        ))
        if matches:
            suffix = prompt[matches[0].start():]
            without_paths = re.sub(r"@/\S+", "", suffix)
            if not without_paths.strip():
                return (
                    prompt[:matches[0].start()].rstrip(),
                    [{"filename": match.group("name")} for match in matches],
                )
        return prompt, []

    def _stash_images(self, images: list, temp_dir: str) -> list:
        """Write validated images to the private turn directory for Codex."""
        _ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                "image/webp": ".webp"}
        paths = []
        for i, img in enumerate(images or []):
            mt = img.get("media_type", "image/png")
            path = os.path.join(temp_dir, f"image-{i:02d}{_ext[mt]}")
            data = decode_attachment(img.get("data", ""))
            with open(path, "xb") as fp:
                fp.write(data)
            os.chmod(path, 0o600)
            paths.append(path)
            log.info("image stashed", index=i, bytes=len(data))
        return paths

    def _cleanup_tmp(self) -> None:
        try:
            cutoff = time.time() - 24 * 3600
            tmp_root = tempfile.gettempdir()
            turn_dir = re.compile(r"^cc-remote-turn-[A-Za-z0-9_-]+$")
            legacy_file = re.compile(r"^cc-remote-\d{10}-[A-Za-z0-9._-]+$")
            with os.scandir(tmp_root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 100_000:
                        log.warning("tmp cleanup scan capped", entries=index)
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if info.st_uid != os.getuid() or info.st_mtime >= cutoff:
                            continue
                        if turn_dir.fullmatch(entry.name) and entry.is_dir(
                                follow_symlinks=False):
                            shutil.rmtree(entry.path)
                        elif legacy_file.fullmatch(entry.name) and entry.is_file(
                                follow_symlinks=False):
                            os.remove(entry.path)
                    except (FileNotFoundError, PermissionError):
                        continue
        except Exception as e:
            log.warning("tmp cleanup failed", error=str(e))

    async def _run_turn(self, ctx: SessionContext, prompt: str,
                        images: Optional[list] = None, files: Optional[list] = None) -> None:
        is_codex = ctx.engine == "codex"
        ctx.translator = (CodexStreamTranslator(self.cfg.tool_result_max) if is_codex
                          else StreamTranslator(self.cfg.tool_result_max))
        # Backpressure the SDK reader if downstream handling stalls. Without a
        # bound, a slow relay/client could make one verbose model turn grow this
        # process until OOM even though transport queues themselves are bounded.
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, self.cfg.turn_reader_queue_cap))
        reader_exc: list = []
        reader_task: Optional[asyncio.Task] = None
        temp_dir: Optional[str] = None
        notice_active = False

        async def reader() -> None:
            cancelled = False
            try:
                async for msg in ctx.sdk.receive_response():
                    await queue.put(msg)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except BaseException as e:
                reader_exc.append(e)
            finally:
                if not cancelled:
                    await queue.put(None)

        async def next_turn_message():
            nonlocal notice_active
            """Wait for one raw engine event, warning on a silent Codex turn.

            This is deliberately not a hard timeout: ultra reasoning and long
            tools can be valid. A real interrupt still uses the existing bounded
            drain path, and any raw app-server event rearms the warning timer on
            the next loop iteration.
            """
            warn = (self.cfg.codex_turn_idle_warn_seconds if is_codex else 0)
            if warn <= 0 or ctx.state == "interrupting":
                return await self._next_from_queue(ctx, queue)
            wait_task = asyncio.create_task(self._next_from_queue(ctx, queue))
            try:
                done, _ = await asyncio.wait((wait_task,), timeout=warn)
                if wait_task in done:
                    # Preserve a real drain-timeout exception from
                    # _next_from_queue.
                    return wait_task.result()
                if ctx.state == "running":
                    await self._emit(ctx, StateEvent(
                        state="running",
                        phase="waiting",
                        detail=(f"Codex 已 {warn:g} 秒没有收到新进展，仍在等待；"
                                "可点击停止。"),
                        msg_id=ctx.active_msg_id,
                    ))
                    notice_active = True
                # An interrupt racing the warning now enters the normal absolute
                # DRAIN_TIMEOUT path. Keep awaiting the SAME task: it may have
                # consumed a boundary event just as asyncio.wait timed out.
                return await wait_task
            finally:
                if not wait_task.done():
                    wait_task.cancel()
                    await asyncio.gather(wait_task, return_exceptions=True)

        async def emit_codex_event(event) -> None:
            nonlocal notice_active
            # Translator errors/progress are turn-local, but the translator is
            # intentionally session-agnostic. Correlate them at the machine edge
            # so the web client renders the detail on the exact optimistic turn.
            if isinstance(event, Error) and event.msg_id is None:
                event.msg_id = ctx.active_msg_id
            if isinstance(event, StateEvent) and event.detail:
                # A retry notification may already be queued when the user
                # interrupts. Never regress interrupting/draining/idle back to
                # running just to display stale retry detail.
                if ctx.state != "running":
                    return
                event.state = ctx.state
                if event.msg_id is None:
                    event.msg_id = ctx.active_msg_id
            await self._emit(ctx, event)
            if isinstance(event, StateEvent) and event.detail:
                notice_active = True
            elif isinstance(event, Error):
                notice_active = False

        try:
            # An EXTERNAL process (a native `claude`/`codex` in the user's terminal)
            # appended to this session's transcript since we resumed it, so our child's
            # in-memory context is STALE — continuing from it would fork the
            # conversation. Reload by resuming afresh before issuing the turn.
            if ctx.needs_reload and ctx.session_id:
                log.info("reloading session after external transcript change",
                         sid=ctx.session_id)
                await ctx.sdk.force_reconnect(resume_id=ctx.session_id, cwd=ctx.cwd,
                                              reason="external transcript change")
                ctx.needs_reload = False
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
            # Serialize the final launch window against interrupt().  An interrupt
            # may have arrived while one of the reconnects above was in flight; in
            # that case it targeted no live turn and we must not submit the prompt
            # afterwards.  Re-check once more after the UserMsg send because that
            # await is also a scheduling point.
            file_meta = ([{"filename": item.get("filename", "attachment")}
                          for item in (files or [])] or None)
            async with ctx.launch_lock:
                if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                    # Other clients do not have the origin's optimistic turn. Echo
                    # it before the terminal marker so TurnEnd cannot accidentally
                    # close the previous visible turn on those clients.
                    await self._emit(ctx, UserMsg(
                        msg_id=ctx.active_msg_id or uuid4().hex,
                        prompt=prompt,
                        images=images,
                        files=file_meta,
                    ))
                    await self._emit(ctx, TurnEnd(result=TurnResult(
                        subtype="error_during_execution",
                        duration_ms=0,
                        is_error=True,
                    )))
                    await self._set_state(ctx, "idle")
                    return

                # Emit the authoritative user echo immediately before sdk.query(),
                # after any slow resume/reload. Its timestamp now matches the
                # transcript's user record closely enough to reconcile optimistic ids
                # without confusing repeated prompts such as "继续".
                await self._emit(ctx, UserMsg(
                    msg_id=ctx.active_msg_id or uuid4().hex,
                    prompt=prompt,
                    images=images,
                    files=file_meta,
                ))
                if files or (is_codex and images):
                    temp_dir = tempfile.mkdtemp(prefix="cc-remote-turn-")
                    os.chmod(temp_dir, 0o700)
                if files:
                    prompt = self._stash_files(prompt, files, temp_dir, ctx.engine)
                if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                    await self._emit(ctx, TurnEnd(result=TurnResult(
                        subtype="error_during_execution",
                        duration_ms=0,
                        is_error=True,
                    )))
                    await self._set_state(ctx, "idle")
                    return
                if is_codex:
                    # codex: images -> private temp dir -> localImage items; files already
                    # referenced by path in the prompt text above.
                    img_paths = self._stash_images(images, temp_dir) if images else []
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
                msg = await next_turn_message()
                if notice_active:
                    # Any raw app-server frame is fresh activity, even when the
                    # translator intentionally skips it (reasoning/token usage).
                    # Clear the stale wait/retry label before handling the frame;
                    # a new retry notification below can immediately replace it.
                    await self._emit(ctx, StateEvent(
                        state=ctx.state,
                        phase=None,
                        detail=None,
                        msg_id=ctx.active_msg_id,
                    ))
                    notice_active = False
                if msg is None:
                    if reader_exc:
                        raise reader_exc[0]
                    raise RuntimeError("cc stream ended without a ResultMessage")

                if is_codex:
                    sid = codex_session_id(msg)
                    if sid and not ctx.session_id:
                        await self._capture_session_id(ctx, sid)
                    for ev in ctx.translator.feed(msg):
                        await emit_codex_event(ev)
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
            log.error("drain timeout — interrupt did not yield a ResultMessage",
                      prompt_length=len(prompt))
            await self._emit(ctx, Error(
                code=ERR_DRAIN_TIMEOUT,
                message="interrupt drain timed out; reconnecting cc",
                msg_id=ctx.active_msg_id))
            try:
                await ctx.sdk.force_reconnect(ctx.session_id, ctx.cwd)
            except Exception as e:
                log.exception("force reconnect failed", error=str(e))
                await self._emit(ctx, Error(
                    code=ERR_CC_CRASH,
                    message="agent reconnect failed; see wrapper logs",
                    msg_id=ctx.active_msg_id))
            await self._set_state(ctx, "idle")
        except Exception as e:
            log.exception("turn failed", error=str(e))
            await self._emit(ctx, Error(
                code=ERR_CC_CRASH,
                message="agent turn failed; see wrapper logs",
                msg_id=ctx.active_msg_id))
            await self._set_state(ctx, "idle")
        finally:
            ctx.translator = None
            ctx.turn_task = None
            ctx.active_msg_id = None
            ctx.interrupt_deadline = None
            ctx.interrupt_event.clear()
            # cc keeps flushing this turn's lines to the transcript for a moment after
            # the result; the watcher uses this stamp to attribute those bytes to US
            # instead of mistaking them for an external writer.
            ctx.last_turn_end = time.time()
            if temp_dir is not None:
                try:
                    shutil.rmtree(temp_dir)
                except FileNotFoundError:
                    pass
                except Exception as e:
                    log.warning("turn attachment cleanup failed", error=str(e))
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
