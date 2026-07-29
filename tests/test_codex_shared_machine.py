"""Zero-token regressions for shared Codex daemon ownership semantics."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from cc_remote.codex_daemon_restart import (
    CodexDaemonRestartState,
    write_restart_state,
)
from cc_remote.protocol import (
    Delta, Effort, Error, GetStatus, Model, Query, SessionActivity,
    SessionControl, StatusReport, Takeover, TurnEnd, UserMsg,
)
from cc_remote.wrapper.codex_external import HolderScan, ProcessIdentity
from tests.test_codex_external import _CodexSdk, _record_async, _watch
from tests.test_multisession import _mk_ctx, _mk_machine


def _event(kind: str, turn_id: str) -> bytes:
    return (json.dumps({
        "type": "event_msg",
        "payload": {"type": kind, "turn_id": turn_id},
    }) + "\n").encode()


def _user_event(message: str) -> bytes:
    return (json.dumps({
        "type": "event_msg",
        "payload": {"type": "user_message", "message": message},
    }) + "\n").encode()


class _SharedSdk(_CodexSdk):
    using_daemon_proxy = True
    model = "gpt-test"
    effort = "high"
    applied_effort = "high"
    service_tier = None
    collaboration_mode = "default"

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[tuple[str, list[str] | None]] = []
        self.reconnects = 0

    async def query(self, prompt: str, images=None) -> None:
        self.queries.append((prompt, images))

    async def receive_response(self):
        yield {
            "method": "turn/completed",
            "params": {
                "threadId": "sid",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }

    async def force_reconnect(self, *_args, **_kwargs) -> None:
        self.reconnects += 1

    async def get_status(self) -> dict:
        return {
            "thread": {
                "thread_id": "sid",
                "status": "idle",
                "active_flags": [],
            },
            "runtime": {},
            "context": {},
            "account": None,
            "rate_limits": [],
            "usage": None,
            "component_errors": [],
        }


class _InterruptedSharedSdk(_SharedSdk):
    shared_daemon_affinity = True

    def __init__(self) -> None:
        super().__init__()
        self.live = False

    @property
    def using_daemon_proxy(self) -> bool:
        return self.live

    async def force_reconnect(self, *_args, **_kwargs) -> None:
        self.reconnects += 1
        self.live = True


class _EvictedDuringReconnectSdk(_InterruptedSharedSdk):
    def __init__(self) -> None:
        super().__init__()
        self.reconnect_started = asyncio.Event()
        self.release_reconnect = asyncio.Event()
        self.disconnects = 0

    async def force_reconnect(self, *_args, **_kwargs) -> None:
        self.reconnects += 1
        self.reconnect_started.set()
        await self.release_reconnect.wait()
        self.live = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.live = False


class _AccountSwitchSharedSdk(_SharedSdk):
    shared_daemon_affinity = True

    def __init__(self) -> None:
        super().__init__()
        self.reader_started = asyncio.Event()
        self.old_turn_interrupted = asyncio.Event()
        self.continuation_started = asyncio.Event()
        self.finish_continuation = asyncio.Event()
        self.interrupts = 0
        self.readers = 0
        self.restart_path = None

    async def query(self, prompt: str, images=None) -> str:
        self.queries.append((prompt, images))
        return "turn-old" if len(self.queries) == 1 else "turn-new"

    async def receive_response(self):
        self.readers += 1
        if self.readers == 1:
            assert self.restart_path is not None
            write_restart_state(
                self.restart_path,
                epoch="9" * 32,
                phase="restarting",
            )
            self.reader_started.set()
            await self.old_turn_interrupted.wait()
            write_restart_state(
                self.restart_path,
                epoch="9" * 32,
                phase="ready",
            )
            yield {
                "method": "turn/completed",
                "params": {
                    "threadId": "sid",
                    "turn": {"id": "turn-old", "status": "interrupted"},
                },
            }
            return
        self.continuation_started.set()
        await self.finish_continuation.wait()
        yield {
            "method": "item/completed",
            "params": {
                "threadId": "sid",
                "turnId": "turn-new",
                "item": {
                    "type": "agentMessage",
                    "id": "answer-new",
                    "text": "continued on new account",
                    "phase": "final_answer",
                },
            },
        }
        yield {
            "method": "turn/completed",
            "params": {
                "threadId": "sid",
                "turn": {"id": "turn-new", "status": "completed"},
            },
        }

    async def interrupt(self) -> None:
        self.interrupts += 1
        self.old_turn_interrupted.set()

    async def get_goal(self):
        return None


class _GoalAccountSwitchSharedSdk(_AccountSwitchSharedSdk):
    def __init__(self) -> None:
        super().__init__()
        self.goal_resumed = asyncio.Event()
        self.finish_goal = asyncio.Event()
        self.on_goal_resumed = None

    async def get_goal(self):
        return {"status": "usageLimited"}

    async def set_goal(self, **kwargs):
        assert kwargs == {"status": "active"}
        self.goal_resumed.set()
        assert self.on_goal_resumed is not None
        await self.on_goal_resumed()
        return {"status": "active"}

    async def receive_spontaneous_response(self, turn_id: str):
        assert turn_id == "goal-turn"
        await self.finish_goal.wait()
        yield {
            "method": "item/completed",
            "params": {
                "threadId": "sid",
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "id": "goal-answer",
                    "text": "goal continued",
                    "phase": "final_answer",
                },
            },
        }
        yield {
            "method": "turn/completed",
            "params": {
                "threadId": "sid",
                "turn": {"id": turn_id, "status": "completed"},
            },
        }


class _SpontaneousAccountSwitchSharedSdk(_SharedSdk):
    """Goal turn whose replacement daemon launches after a controlled delay."""

    shared_daemon_affinity = True

    def __init__(self) -> None:
        super().__init__()
        self.restart_path = None
        self.spontaneous_started = asyncio.Event()
        self.old_turn_interrupted = asyncio.Event()
        self.continuation_started = asyncio.Event()
        self.finish_continuation = asyncio.Event()
        self.goal_activated = asyncio.Event()
        self.interrupts = 0
        self.goal_activations = 0
        self.on_goal_resumed = None

    async def receive_spontaneous_response(self, turn_id: str):
        if turn_id == "goal-old":
            self.spontaneous_started.set()
            await self.old_turn_interrupted.wait()
            # The watcher should cancel this old-generation stream. Keep a
            # terminal fallback if cancellation loses the race.
            yield {
                "method": "turn/completed",
                "params": {
                    "threadId": "sid",
                    "turn": {"id": turn_id, "status": "interrupted"},
                },
            }
            return
        assert turn_id == "goal-new"
        self.continuation_started.set()
        await self.finish_continuation.wait()
        yield {
            "method": "item/completed",
            "params": {
                "threadId": "sid",
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "id": "goal-answer-new",
                    "text": "goal resumed on new account",
                    "phase": "final_answer",
                },
            },
        }
        yield {
            "method": "turn/completed",
            "params": {
                "threadId": "sid",
                "turn": {"id": turn_id, "status": "completed"},
            },
        }

    async def interrupt(self) -> None:
        self.interrupts += 1
        assert self.restart_path is not None
        write_restart_state(
            self.restart_path,
            epoch="9" * 32,
            phase="ready",
        )
        self.old_turn_interrupted.set()

    async def get_goal(self):
        return {"status": "usageLimited"}

    async def set_goal(self, **kwargs):
        assert kwargs == {"status": "active"}
        self.goal_activations += 1
        self.goal_activated.set()
        if self.on_goal_resumed is not None:
            asyncio.create_task(self.on_goal_resumed())
        return {"status": "active"}


class _RecoveredOwnedTurnSdk(_SharedSdk):
    shared_daemon_affinity = True

    def __init__(self) -> None:
        super().__init__()
        self.machine = None
        self.ctx = None
        self.interrupted = asyncio.Event()
        self.interrupts = 0

    async def recover_owned_turn(self, turn_id: str) -> bool:
        assert self.machine is not None and self.ctx is not None
        self.remember_owned_turn_id(turn_id)
        await self.machine._on_codex_turn_lifecycle(
            self.ctx, "started", turn_id)
        return True

    async def receive_spontaneous_response(self, turn_id: str):
        await self.interrupted.wait()
        yield {
            "method": "turn/completed",
            "params": {
                "threadId": "sid",
                "turn": {"id": turn_id, "status": "interrupted"},
            },
        }

    async def interrupt(self) -> None:
        self.interrupts += 1
        self.interrupted.set()


class _RecoveredAccountSwitchSdk(_SpontaneousAccountSwitchSharedSdk):
    """Replacement wrapper sees the old leased turn already aborted."""

    async def query(self, prompt: str, images=None) -> str:
        self.queries.append((prompt, images))
        return "goal-new"

    async def receive_response(self):
        async for message in (
            _SpontaneousAccountSwitchSharedSdk
            .receive_spontaneous_response(self, "goal-new")
        ):
            yield message

    async def receive_spontaneous_response(self, turn_id: str):
        # The old stream is never consumed: recovery enters the persisted
        # account handoff before polling it.
        await asyncio.Future()
        yield  # pragma: no cover

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def get_goal(self):
        return None


class _RecoveredActiveAccountSwitchSdk(_RecoveredAccountSwitchSdk):
    def __init__(self) -> None:
        super().__init__()
        self.machine = None
        self.ctx = None

    async def recover_owned_turn(self, turn_id: str) -> bool:
        assert self.machine is not None and self.ctx is not None
        await self.machine._on_codex_turn_lifecycle(
            self.ctx, "started", turn_id)
        return True


class _RecoveredOwnedGoalSdk(_RecoveredOwnedTurnSdk):
    async def get_goal(self):
        return {"status": "active"}


class _RecoveredGoalAccountSwitchSdk(_SpontaneousAccountSwitchSharedSdk):
    async def receive_spontaneous_response(self, turn_id: str):
        if turn_id == "goal-new":
            async for message in (
                _SpontaneousAccountSwitchSharedSdk
                .receive_spontaneous_response(self, turn_id)
            ):
                yield message
            return
        await asyncio.Future()
        yield  # pragma: no cover


def test_restarted_wrapper_reclaims_only_leased_active_daemon_turn(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_bytes(_event("task_started", "owned-turn"))
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_rollout_path",
            lambda _sid: str(rollout),
        )
        machine._codex_turn_leases.claim(
            "sid", "owned-turn", "logical-message")

        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _RecoveredOwnedTurnSdk()
        ctx.sdk.machine = machine
        ctx.sdk.ctx = ctx
        machine.sessions[ctx.key] = ctx

        assert await machine._recover_codex_owned_turn(ctx, "sid") is True
        await asyncio.sleep(0)

        assert ctx.state == "running"
        assert ctx.codex_spontaneous_turn_id == "owned-turn"
        assert ctx.active_msg_id == "logical-message"
        assert ctx.codex_owned_turn_id == "owned-turn"
        assert ctx.codex_spontaneous_task is not None
        lease = machine._codex_turn_leases.get("sid")
        assert lease is not None
        assert lease.automatic is False
        assert not [
            event for event in transport.sent if isinstance(event, UserMsg)
        ]

        await machine._handle_interrupt(SimpleNamespace(sid="sid"))
        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1.0)

        assert ctx.sdk.interrupts == 1
        assert ctx.state == "idle"
        assert ctx.codex_owned_turn_id is None
        assert machine._codex_turn_leases.get("sid") is None

    asyncio.run(go())


def test_restarted_wrapper_continues_leased_turn_aborted_by_account_switch(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_bytes(
            _event("task_started", "owned-turn")
            + _event("turn_aborted", "owned-turn")
        )
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_rollout_path",
            lambda _sid: str(rollout),
        )
        machine._codex_turn_leases.claim(
            "sid",
            "owned-turn",
            "logical-message",
            daemon_epoch="8" * 32,
        )
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="9" * 32,
            phase="ready",
        )

        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _RecoveredAccountSwitchSdk()
        ctx.codex_checkpoint = False
        machine.sessions[ctx.key] = ctx

        assert await machine._recover_codex_owned_turn(ctx, "sid") is True
        await asyncio.wait_for(
            ctx.sdk.continuation_started.wait(), timeout=5.0)

        assert ctx.state == "running"
        assert ctx.codex_spontaneous_turn_id == "goal-new"
        assert ctx.active_msg_id == "logical-message"
        assert ctx.sdk.interrupts == 1
        assert ctx.sdk.reconnects == 1
        assert len(ctx.sdk.queries) == 1
        assert "<codex_internal_context" in ctx.sdk.queries[0][0]
        lease = machine._codex_turn_leases.get("sid")
        assert lease is not None
        assert lease.turn_id == "goal-new"
        assert lease.msg_id == "logical-message"
        assert lease.daemon_epoch == "9" * 32
        assert not [
            event for event in transport.sent
            if isinstance(event, (Error, TurnEnd))
        ]

        active = ctx.codex_spontaneous_task
        assert active is not None
        ctx.sdk.finish_continuation.set()
        await asyncio.wait_for(active, timeout=2.0)

        assert ctx.state == "idle"
        assert machine._codex_turn_leases.get("sid") is None

    asyncio.run(asyncio.wait_for(go(), timeout=10.0))


def test_restarted_wrapper_recovers_goal_with_distinct_rollout_turn_id(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_bytes(_event("task_started", "goal-task-id"))
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_rollout_path",
            lambda _sid: str(rollout),
        )
        machine._codex_turn_leases.claim(
            "sid",
            "native-turn-id",
            "logical-message",
            daemon_epoch="8" * 32,
            automatic=True,
        )
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="8" * 32,
            phase="ready",
        )

        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _RecoveredOwnedGoalSdk()
        ctx.sdk.machine = machine
        ctx.sdk.ctx = ctx
        machine.sessions[ctx.key] = ctx

        assert await machine._recover_codex_owned_turn(ctx, "sid") is True
        await asyncio.sleep(0)
        assert ctx.state == "running"
        assert ctx.codex_spontaneous_turn_id == "native-turn-id"
        assert ctx.active_msg_id == "logical-message"
        lease = machine._codex_turn_leases.get("sid")
        assert lease is not None
        assert lease.automatic is True

        await machine._handle_interrupt(SimpleNamespace(sid="sid"))
        active = ctx.codex_spontaneous_task
        assert active is not None
        await asyncio.wait_for(active, timeout=2.0)
        assert ctx.state == "idle"

    asyncio.run(asyncio.wait_for(go(), timeout=5.0))


def test_restarted_wrapper_continues_aborted_goal_with_distinct_rollout_id(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_bytes(
            _event("task_started", "goal-task-id")
            + _event("turn_aborted", "goal-task-id")
        )
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_rollout_path",
            lambda _sid: str(rollout),
        )
        machine._codex_turn_leases.claim(
            "sid",
            "native-turn-id",
            "logical-message",
            daemon_epoch="8" * 32,
            automatic=True,
        )
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="9" * 32,
            phase="ready",
        )

        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _RecoveredGoalAccountSwitchSdk()
        ctx.sdk.restart_path = machine._codex_daemon_restart_path
        ctx.codex_checkpoint = False
        machine.sessions[ctx.key] = ctx

        async def start_goal_continuation() -> None:
            await machine._on_codex_turn_lifecycle(
                ctx, "started", "goal-new")

        ctx.sdk.on_goal_resumed = start_goal_continuation

        assert await machine._recover_codex_owned_turn(ctx, "sid") is True
        await asyncio.wait_for(
            ctx.sdk.continuation_started.wait(), timeout=5.0)
        assert ctx.state == "running"
        assert ctx.codex_spontaneous_turn_id == "goal-new"
        assert ctx.sdk.interrupts == 1
        assert ctx.sdk.reconnects == 1

        active = ctx.codex_spontaneous_task
        assert active is not None
        ctx.sdk.finish_continuation.set()
        await asyncio.wait_for(active, timeout=2.0)
        assert ctx.state == "idle"

    asyncio.run(asyncio.wait_for(go(), timeout=10.0))


def test_restarted_wrapper_hands_active_old_generation_to_new_daemon(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, _transport = _mk_machine()
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_bytes(_event("task_started", "owned-turn"))
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_rollout_path",
            lambda _sid: str(rollout),
        )
        machine._codex_turn_leases.claim(
            "sid",
            "owned-turn",
            "logical-message",
            daemon_epoch="8" * 32,
        )
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="9" * 32,
            phase="ready",
        )

        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _RecoveredActiveAccountSwitchSdk()
        ctx.sdk.machine = machine
        ctx.sdk.ctx = ctx
        ctx.codex_checkpoint = False
        # _spawn stamps the newest ready generation before consulting the old
        # lease. Recovery must restore the old generation for the watcher.
        ctx.codex_daemon_epoch = "9" * 32
        machine.sessions[ctx.key] = ctx

        assert await machine._recover_codex_owned_turn(ctx, "sid") is True
        await asyncio.wait_for(
            ctx.sdk.continuation_started.wait(), timeout=5.0)

        assert ctx.state == "running"
        assert ctx.codex_daemon_epoch == "9" * 32
        assert ctx.codex_spontaneous_turn_id == "goal-new"
        assert ctx.sdk.interrupts == 1
        assert ctx.sdk.reconnects == 1

        active = ctx.codex_spontaneous_task
        assert active is not None
        ctx.sdk.finish_continuation.set()
        await asyncio.wait_for(active, timeout=2.0)
        assert ctx.state == "idle"

    asyncio.run(asyncio.wait_for(go(), timeout=10.0))


def test_wrapper_startup_restores_background_owned_turns():
    async def go() -> None:
        machine, _transport = _mk_machine()
        machine.cfg.max_concurrent_sessions = 1
        idle = _mk_ctx("idle-bootstrap", "idle-bootstrap")
        idle.state = "idle"
        machine.sessions[idle.key] = idle
        machine._codex_turn_leases.claim("sid", "turn", "message")
        spawned = []

        async def spawn(**kwargs):
            spawned.append(kwargs)
            ctx = _mk_ctx("sid", "sid")
            ctx.engine = "codex"
            ctx.state = "running"
            ctx.codex_owned_turn_id = "turn"
            machine.sessions[ctx.key] = ctx
            return ctx

        machine._spawn = spawn
        await machine._restore_codex_owned_turns()

        assert spawned == [{
            "resume_id": "sid",
            "engine": "codex",
            "space": "code",
            "bootstrap": True,
        }]
        assert machine.sessions["sid"].state == "running"
        # Recovery is never permanently skipped behind an idle bootstrap
        # resident; startup now invokes it before creating that resident.
        assert len(machine.sessions) == 2

    asyncio.run(go())


def test_shared_code_watcher_mirrors_growth_without_legacy_lock(tmp_path):
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        # A stale legacy bit from an earlier stdio generation must be retired by
        # the first authoritative shared-daemon poll.
        ctx.needs_reload = True
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        watch.update({
            "external": True,
            "active_external_turns": {"old-terminal-turn": 1.0},
            "pending_wrapper_turns": {
                "unattributed-turn": {"seen_at": 1.0},
            },
        })
        machine._watch["sid"] = watch
        mirrored: list[str] = []
        refreshed: list[str] = []

        async def mirror(sid: str):
            mirrored.append(sid)

        async def refresh(session_ctx):
            refreshed.append(session_ctx.session_id)

        machine._push_mirrored_history = mirror
        machine._refresh_codex_collaboration_mode = refresh
        holder = ProcessIdentity(101, 1001)
        path.write_bytes(
            _event("task_started", "terminal-visible")
            + _event("task_complete", "terminal-visible")
        )

        await machine._poll_codex_watch(
            "sid", watch, {holder}, 1000.0, writers={holder}
        )

        assert mirrored == ["sid"]
        assert refreshed == ["sid"]
        assert watch["external"] is False
        assert watch["takeover_pending"] is None
        assert watch["active_external_turns"] == {}
        assert watch["pending_wrapper_turns"] == {}
        assert ctx.needs_reload is False
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.terminal_attached is True
        assert ctx.control_can_takeover is False

    asyncio.run(go())


def test_shared_code_distinguishes_private_app_turn_from_shared_cli(tmp_path):
    async def go() -> None:
        machine, transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch

        with path.open("ab") as stream:
            stream.write(_event("task_started", "app-turn"))
        await machine._poll_codex_watch(
            "sid", watch, set(), 1000.0, writers=set()
        )

        assert watch["active_external_turns"] == {"app-turn": 1000.0}
        assert watch["desktop_active"] is True
        assert watch["external"] is True
        assert ctx.control_mode == "desktop"
        assert ctx.write_state == "read_only"
        assert ctx.control_can_takeover is False
        assert any(
            isinstance(event, SessionActivity)
            and event.session_id == "sid"
            and event.state == "running"
            for event in transport.sent
        )

        with path.open("ab") as stream:
            stream.write(_event("task_complete", "app-turn"))
        await machine._poll_codex_watch(
            "sid", watch, set(), 1001.0, writers=set()
        )

        assert watch["active_external_turns"] == {}
        assert watch["desktop_active"] is False
        assert watch["external"] is False
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert any(
            isinstance(event, SessionActivity)
            and event.session_id == "sid"
            and event.state == "idle"
            for event in transport.sent
        )

        with path.open("ab") as stream:
            stream.write(_event("task_started", "cli-turn"))
        holder = ProcessIdentity(101, 1001)
        await machine._poll_codex_watch(
            "sid", watch, {holder}, 1002.0, writers={holder}
        )

        assert watch["active_external_turns"] == {"cli-turn": 1002.0}
        assert watch["desktop_active"] is False
        assert watch["external"] is False
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.terminal_attached is True

    asyncio.run(go())


def test_shared_context_attach_preserves_running_private_app_turn(tmp_path):
    """Opening Remote mid-App turn must not manufacture an idle/writable state."""
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(_event("task_started", "app-turn"))
        watch = _watch(path)
        watch.update({
            "external": True,
            "desktop_active": True,
            "active_external_turns": {"app-turn": 1000.0},
        })
        machine._watch["sid"] = watch

        # The sidebar watcher discovered the native App turn before Remote
        # focused the session. Focusing creates a shared-daemon context, but
        # that new passive connection does not own or finish the App turn.
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        private = ProcessIdentity(109, 1009)

        await machine._poll_codex_watch(
            "sid",
            watch,
            set(),
            1001.0,
            writers={private},
            private_holders={private},
        )

        assert watch["active_external_turns"] == {"app-turn": 1000.0}
        assert watch["desktop_active"] is True
        assert watch["external"] is True
        assert ctx.control_mode == "desktop"
        assert ctx.write_state == "read_only"
        assert ctx.terminal_attached is True

    asyncio.run(go())


def test_shared_cli_user_message_refreshes_after_earlier_task_start(tmp_path):
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        mirrored: list[str] = []
        machine._push_mirrored_history = lambda sid: _record_async(
            mirrored, sid)
        holder = ProcessIdentity(101, 1001)

        # The real CLI flushes task_started first. That first refresh cannot
        # contain the prompt yet.
        with path.open("ab") as stream:
            stream.write(_event("task_started", "cli-turn"))
        await machine._poll_codex_watch(
            "sid", watch, {holder}, 1000.0, writers={holder})
        assert mirrored == ["sid"]

        # user_message arrives in a separate append and must independently
        # refresh the open browser instead of waiting for a session switch.
        with path.open("ab") as stream:
            stream.write(_user_event("在？测试测试"))
        await machine._poll_codex_watch(
            "sid", watch, {holder}, 1000.1, writers={holder})

        assert mirrored == ["sid", "sid"]
        assert watch["external"] is False
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"

    asyncio.run(go())


def test_shared_rollout_tail_cannot_revert_live_app_server_settings(
    monkeypatch,
):
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.sdk.model = "gpt-live"
        ctx.sdk.effort = "ultra"
        ctx.sdk.applied_effort = "ultra"
        ctx.announced_model = "gpt-live"
        ctx.announced_effort = "ultra"
        machine.sessions[ctx.key] = ctx

        # A model switch updates the shared app-server immediately, but Codex
        # does not append the new turn_context until the next turn starts.  The
        # bounded rollout tail therefore still contains the previous model.
        monkeypatch.setattr(
            "cc_remote.wrapper.machine.codex_session_settings",
            lambda *_args, **_kwargs: {
                "model": "gpt-stale-rollout",
                "effort": "low",
            },
        )

        await machine._refresh_codex_collaboration_mode(ctx)

        assert ctx.sdk.model == "gpt-live"
        assert ctx.sdk.effort == "ultra"
        assert not [
            event for event in transport.sent
            if isinstance(event, (Model, Effort))
        ]

        # If Web was behind, publish the live app-server values rather than the
        # stale rollout values.
        ctx.announced_model = "gpt-old-ui"
        ctx.announced_effort = "low"
        await machine._refresh_codex_collaboration_mode(ctx)

        assert ctx.announced_model == "gpt-live"
        assert ctx.announced_effort == "ultra"
        assert [
            (event.type, getattr(event, "model", None),
             getattr(event, "effort", None))
            for event in transport.sent
            if isinstance(event, (Model, Effort))
        ] == [
            ("model", "gpt-live", None),
            ("effort", None, "ultra"),
        ]

    asyncio.run(go())


def test_shared_headless_backends_do_not_report_terminal_attached(tmp_path):
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[ctx.session_id] = watch
        daemon = ProcessIdentity(102, 1002)
        stdio_proxy = ProcessIdentity(103, 1003)
        passive = {daemon, stdio_proxy}
        scan = HolderScan(
            {ctx.session_id: passive},
            True,
            {ctx.session_id: passive},
        )

        holders, writers, private_holders = machine._codex_holder_sets(
            watch, scan, ctx.session_id)
        await machine._poll_codex_watch(
            ctx.session_id,
            watch,
            holders,
            1000.0,
            writers=writers,
            private_holders=private_holders,
        )

        assert holders == set()
        assert writers == passive
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.terminal_attached is False

    asyncio.run(go())


def test_shared_private_app_holder_is_informational_while_idle(tmp_path):
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[ctx.session_id] = watch
        managed = ProcessIdentity(105, 1005)
        private = ProcessIdentity(106, 1006)
        scan = HolderScan(
            {ctx.session_id: {managed, private}},
            True,
            {ctx.session_id: {managed, private}},
            {},
            {ctx.session_id: {private}},
        )

        holders, writers, private_holders = machine._codex_holder_sets(
            watch, scan, ctx.session_id)
        await machine._poll_codex_watch(
            ctx.session_id,
            watch,
            holders,
            1000.0,
            writers=writers,
            private_holders=private_holders,
        )

        assert holders == set()
        assert writers == {managed, private}
        assert watch["private_app_loaded"] is True
        assert watch["desktop_active"] is False
        assert watch["external"] is False
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.needs_reload is False

        await machine._poll_codex_watch(
            ctx.session_id,
            watch,
            set(),
            1001.0,
            writers={managed},
            private_holders=set(),
        )

        assert watch["private_app_loaded"] is False
        assert watch["external"] is False
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.needs_reload is False

    asyncio.run(go())


def test_stdio_private_app_holder_is_informational_while_idle(tmp_path):
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _CodexSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[ctx.session_id] = watch
        private = ProcessIdentity(107, 1007)

        await machine._poll_codex_watch(
            ctx.session_id,
            watch,
            set(),
            1000.0,
            writers={private},
            private_holders={private},
        )

        assert watch["private_app_loaded"] is True
        assert watch["desktop_active"] is False
        assert watch["external"] is False
        assert ctx.control_mode == "remote"
        assert ctx.write_state == "writable"
        assert ctx.control_can_takeover is False

    asyncio.run(go())


def test_private_app_idle_client_does_not_block_remote_query(
        tmp_path, monkeypatch):
    async def go() -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        watch = _watch(path)
        watch.update({"external": False, "private_app_loaded": True})
        machine._watch[ctx.session_id] = watch
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def refresh_idle_app(_sid: str) -> bool:
            return False

        ran: list[tuple[str, str]] = []

        async def fake_turn(session_ctx, prompt, _images=None, _files=None):
            ran.append((session_ctx.session_id, prompt))
            await machine._set_state(session_ctx, "idle")

        monkeypatch.setattr(
            machine, "_prime_codex_ownership", refresh_idle_app)
        monkeypatch.setattr(machine, "_run_turn", fake_turn)

        result = await machine._handle_query(Query(
            sid="sid", prompt="remote-owned", msg_id="private-app-query"
        ))

        assert result is None
        assert ctx.turn_task is not None
        await ctx.turn_task
        assert ran == [("sid", "remote-owned")]
        assert ctx.state == "idle"

    asyncio.run(go())


def test_remote_owned_turn_mirrored_into_private_app_stays_writable(tmp_path):
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.sdk.remember_owned_turn_id("remote-turn")
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[ctx.session_id] = watch
        private = ProcessIdentity(108, 1008)

        with path.open("ab") as stream:
            stream.write(_event("task_started", "remote-turn"))
        await machine._poll_codex_watch(
            ctx.session_id,
            watch,
            set(),
            1000.0,
            writers={private},
            private_holders={private},
        )

        assert watch["private_app_loaded"] is True
        assert watch["active_external_turns"] == {}
        assert watch["desktop_active"] is False
        assert watch["external"] is False
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"

    asyncio.run(go())


def test_private_app_foreign_active_turn_is_read_only(tmp_path):
    async def go() -> None:
        machine, _transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[ctx.session_id] = watch
        private = ProcessIdentity(109, 1009)

        with path.open("ab") as stream:
            stream.write(_event("task_started", "app-turn"))
        await machine._poll_codex_watch(
            ctx.session_id,
            watch,
            set(),
            1000.0,
            writers={private},
            private_holders={private},
        )

        assert watch["active_external_turns"] == {"app-turn": 1000.0}
        assert watch["desktop_active"] is True
        assert watch["external"] is True
        assert ctx.control_mode == "desktop"
        assert ctx.write_state == "read_only"
        assert ctx.control_can_takeover is False

    asyncio.run(go())


def test_shared_terminal_exit_clears_attachment_on_next_complete_scan(tmp_path):
    async def go() -> None:
        machine, transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[ctx.session_id] = watch
        tui = ProcessIdentity(104, 1004)

        await machine._poll_codex_watch(
            ctx.session_id, watch, {tui}, 1000.0, writers={tui},
            ownership_scan_complete=True,
        )
        attached_revision = ctx.control_revision
        assert ctx.terminal_attached is True

        # The next complete /proc scan is authoritative. The shared daemon and
        # this wrapper's proxy may remain alive, but an exited TUI must not leave
        # a sticky terminal badge or require a transcript append to clear it.
        await machine._poll_codex_watch(
            ctx.session_id, watch, set(), 1001.5, writers=set(),
            ownership_scan_complete=True,
        )

        assert watch["holders"] == set()
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.terminal_attached is False
        assert ctx.control_revision == attached_revision + 1
        controls = [
            event for event in transport.sent
            if isinstance(event, SessionControl)
        ]
        assert [event.terminal_attached for event in controls[-2:]] == [
            True, False,
        ]

    asyncio.run(go())


def test_shared_code_query_refreshes_activity_without_locking_cli(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {ProcessIdentity(111, 1101)},
            "writers": {ProcessIdentity(111, 1101)},
        })
        machine._watch["sid"] = watch
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        activity_probes: list[str] = []

        async def refresh_activity(probe_sid: str) -> bool:
            activity_probes.append(probe_sid)
            return False

        ran: list[tuple[str, str]] = []

        async def fake_turn(session_ctx, prompt, _images=None, _files=None):
            ran.append((session_ctx.session_id, prompt))
            await machine._set_state(session_ctx, "idle")

        monkeypatch.setattr(
            machine, "_prime_codex_ownership", refresh_activity
        )
        monkeypatch.setattr(machine, "_run_turn", fake_turn)

        result = await machine._handle_query(Query(
            sid="sid", prompt="hello", msg_id="shared-query"
        ))
        assert result is None
        assert ctx.turn_task is not None
        await ctx.turn_task
        assert activity_probes == ["sid"]
        assert ran == [("sid", "hello")]
        assert ctx.state == "idle"

    asyncio.run(go())


def test_interrupted_shared_query_reconnects_and_refreshes_activity(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _InterruptedSharedSdk()
        machine.sessions[ctx.key] = ctx
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {ProcessIdentity(111, 1101)},
            "writers": {ProcessIdentity(111, 1101)},
        })
        machine._watch["sid"] = watch
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        activity_probes: list[str] = []

        async def refresh_activity(probe_sid: str) -> bool:
            activity_probes.append(probe_sid)
            return False

        ran: list[tuple[str, str]] = []

        async def fake_turn(session_ctx, prompt, _images=None, _files=None):
            ran.append((session_ctx.session_id, prompt))
            await machine._set_state(session_ctx, "idle")

        monkeypatch.setattr(
            machine, "_prime_codex_ownership", refresh_activity
        )
        monkeypatch.setattr(machine, "_run_turn", fake_turn)

        result = await machine._handle_query(Query(
            sid="sid", prompt="hello", msg_id="shared-reconnect-query"
        ))
        assert result is None
        assert ctx.turn_task is not None
        await ctx.turn_task
        assert ctx.sdk.reconnects == 1
        assert activity_probes == ["sid"]
        assert ctx.sdk.using_daemon_proxy is True
        assert ran == [("sid", "hello")]
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.control_can_takeover is False

    asyncio.run(go())


def test_intentional_restart_reconnects_live_proxy_before_query(monkeypatch):
    async def go() -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.codex_daemon_epoch = "1" * 32
        machine.sessions[ctx.key] = ctx
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="2" * 32,
            phase="ready",
        )
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def no_external(_sid):
            return False

        monkeypatch.setattr(machine, "_prime_codex_ownership", no_external)
        ran = []

        async def fake_turn(session_ctx, prompt, _images=None, _files=None):
            ran.append((session_ctx.session_id, prompt))
            await machine._set_state(session_ctx, "idle")

        monkeypatch.setattr(machine, "_run_turn", fake_turn)

        result = await machine._handle_query(Query(
            sid="sid", prompt="hello", msg_id="restart-query"))
        assert result is None
        await ctx.turn_task
        assert ctx.sdk.reconnects == 1
        assert ctx.codex_daemon_epoch == "2" * 32
        assert ran == [("sid", "hello")]

    asyncio.run(go())


def test_same_restart_epoch_does_not_reconnect_live_proxy(monkeypatch):
    async def go() -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.codex_daemon_epoch = "3" * 32
        machine.sessions[ctx.key] = ctx
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="3" * 32,
            phase="ready",
        )
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def no_external(_sid):
            return False

        monkeypatch.setattr(machine, "_prime_codex_ownership", no_external)

        async def fake_turn(session_ctx, *_args):
            await machine._set_state(session_ctx, "idle")

        monkeypatch.setattr(machine, "_run_turn", fake_turn)
        await machine._handle_query(Query(
            sid="sid", prompt="hello", msg_id="same-epoch-query"))
        await ctx.turn_task
        assert ctx.sdk.reconnects == 0

    asyncio.run(go())


def test_idle_status_reconnects_changed_daemon_generation(monkeypatch):
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.state = "idle"
        ctx.codex_daemon_epoch = "a" * 32
        machine.sessions[ctx.key] = ctx
        ready = CodexDaemonRestartState(
            epoch="b" * 32,
            phase="ready",
            updated_at=1.0,
            deadline_at=2.0,
        )
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def restart_state(*, wait, interrupt_event):
            assert wait is True
            assert interrupt_event is ctx.interrupt_event
            return ready

        async def no_external(_sid):
            return False

        monkeypatch.setattr(machine, "_codex_restart_state", restart_state)
        monkeypatch.setattr(machine, "_prime_codex_ownership", no_external)

        result = await asyncio.wait_for(
            machine._handle_get_status(GetStatus(
                sid="sid",
                cmd_id="usage-refresh",
                client_id="browser",
            )),
            timeout=1.0,
        )

        assert isinstance(result, StatusReport)
        assert ctx.sdk.reconnects == 1
        assert ctx.codex_daemon_epoch == "b" * 32
        assert transport.sent[-1].to == "browser"
        assert transport.sent[-1].request_id == "usage-refresh"

    asyncio.run(go())


def test_generation_reconnect_does_not_revive_evicted_context(monkeypatch):
    async def go() -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        sdk = _EvictedDuringReconnectSdk()
        ctx.sdk = sdk
        ctx.codex_daemon_epoch = "a" * 32
        machine.sessions[ctx.key] = ctx
        ready = CodexDaemonRestartState(
            epoch="b" * 32,
            phase="ready",
            updated_at=1.0,
            deadline_at=2.0,
        )

        async def restart_state(*, wait, interrupt_event):
            assert wait is True
            assert interrupt_event is ctx.interrupt_event
            return ready

        monkeypatch.setattr(machine, "_codex_restart_state", restart_state)
        reconnect = asyncio.create_task(machine._ensure_codex_daemon_generation(
            ctx, reason="background status refresh"))
        await asyncio.wait_for(sdk.reconnect_started.wait(), timeout=1.0)

        # Model the eviction path: it removes the route and closes the old
        # proxy while a reconnect is still awaiting app-server startup.
        assert machine.sessions.pop(ctx.key) is ctx
        await sdk.disconnect()
        sdk.release_reconnect.set()

        assert await asyncio.wait_for(reconnect, timeout=1.0) is False
        assert sdk.reconnects == 1
        assert sdk.disconnects == 2
        assert sdk.live is False

    asyncio.run(go())


def test_status_read_does_not_block_serial_commands():
    async def go() -> None:
        machine, _transport = _mk_machine()
        status_started = asyncio.Event()
        release_status = asyncio.Event()
        command_seen = asyncio.Event()
        status_calls = 0

        async def process(command):
            nonlocal status_calls
            if command.type == "get_status":
                status_calls += 1
                status_started.set()
                await release_status.wait()
            else:
                command_seen.set()

        machine._process_command = process
        status = SimpleNamespace(
            type="get_status",
            client_id="browser",
            cmd_id="usage-refresh",
        )
        machine._start_status_command(status)
        await asyncio.wait_for(status_started.wait(), timeout=1.0)

        # A reconnect retry coalesces, while a user command remains immediately
        # serviceable by the serial lane.
        machine._start_status_command(status)
        await machine._process_command_safely(SimpleNamespace(type="query"))
        assert command_seen.is_set()
        assert status_calls == 1

        tasks = list(machine._status_command_tasks.values())
        release_status.set()
        await asyncio.gather(*tasks)

    asyncio.run(go())


def test_status_failure_keeps_request_correlation(monkeypatch):
    class FailedStatusSdk(_SharedSdk):
        async def get_status(self) -> dict:
            raise RuntimeError("status failed")

    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = FailedStatusSdk()
        ctx.state = "idle"
        machine.sessions[ctx.key] = ctx

        async def generation_ready(_ctx, *, reason):
            return True

        monkeypatch.setattr(
            machine, "_ensure_codex_daemon_generation", generation_ready)
        result = await machine._handle_get_status(GetStatus(
            sid="sid",
            cmd_id="usage-refresh",
            client_id="browser",
        ))

        assert isinstance(result, Error)
        assert result.request_id == "usage-refresh"
        assert result.to == "browser"
        assert transport.sent[-1] is result

    asyncio.run(go())


def test_idle_status_is_emitted_after_running_generation_read(monkeypatch):
    class OrderedStatusSdk(_SharedSdk):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.status_calls = 0

        async def get_status(self) -> dict:
            self.status_calls += 1
            call = self.status_calls
            if call == 1:
                self.first_started.set()
                await self.release_first.wait()
            report = await super().get_status()
            report["runtime"] = {"app_server_version": f"read-{call}"}
            return report

    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        sdk = OrderedStatusSdk()
        ctx.sdk = sdk
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        async def generation_ready(_ctx, *, reason):
            return True

        monkeypatch.setattr(
            machine, "_ensure_codex_daemon_generation", generation_ready)
        old_read = asyncio.create_task(machine._handle_get_status(GetStatus(
            sid="sid", cmd_id="old-read", client_id="browser",
        )))
        await asyncio.wait_for(sdk.first_started.wait(), timeout=1.0)

        ctx.state = "idle"
        new_read = asyncio.create_task(machine._handle_get_status(GetStatus(
            sid="sid", cmd_id="new-read", client_id="browser",
        )))
        await asyncio.sleep(0)
        assert sdk.status_calls == 1

        sdk.release_first.set()
        await asyncio.gather(old_read, new_read)
        reports = [
            event for event in transport.sent
            if isinstance(event, StatusReport)
        ]
        assert [
            report.runtime.app_server_version for report in reports
        ] == ["read-1", "read-2"]
        assert [report.request_id for report in reports] == ["old-read", "new-read"]

    asyncio.run(go())


def test_query_waits_for_restart_ready_before_reconnecting(monkeypatch):
    async def go() -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.codex_daemon_epoch = "6" * 32
        machine.sessions[ctx.key] = ctx
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="7" * 32,
            phase="restarting",
        )
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def no_external(_sid):
            return False

        async def finish_restart():
            await asyncio.sleep(0.02)
            write_restart_state(
                machine._codex_daemon_restart_path,
                epoch="7" * 32,
                phase="ready",
            )

        async def fake_turn(session_ctx, *_args):
            await machine._set_state(session_ctx, "idle")

        monkeypatch.setattr(machine, "_prime_codex_ownership", no_external)
        monkeypatch.setattr(machine, "_run_turn", fake_turn)
        finisher = asyncio.create_task(finish_restart())

        result = await machine._handle_query(Query(
            sid="sid", prompt="after-ready", msg_id="wait-ready-query"))
        await finisher
        assert result is None
        await ctx.turn_task
        assert ctx.sdk.reconnects == 1
        assert ctx.codex_daemon_epoch == "7" * 32

    asyncio.run(go())


def test_failed_restart_barrier_rejects_query_without_model_send(monkeypatch):
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.codex_daemon_epoch = "4" * 32
        machine.sessions[ctx.key] = ctx
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="5" * 32,
            phase="failed",
        )

        result = await machine._handle_query(Query(
            sid="sid", prompt="must-not-send", msg_id="failed-restart"))

        assert isinstance(result, Error)
        assert result.code == "not_running"
        assert ctx.sdk.reconnects == 0
        assert ctx.sdk.queries == []
        assert ctx.state == "idle"
        assert transport.sent[-1].msg_id == "failed-restart"

    asyncio.run(go())


def test_stale_restart_barriers_restore_shared_daemon_operations():
    async def go(phase: str) -> None:
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.codex_daemon_epoch = "4" * 32
        machine.sessions[ctx.key] = ctx
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="5" * 32,
            phase=phase,
            # write_restart_state clamps this to its current wall clock, so
            # the marker is already expired when it is read below.
            deadline_at=0.0,
        )

        assert await machine._ensure_codex_daemon_generation(
            ctx, reason=f"recover stale {phase} barrier",
        )
        assert ctx.sdk.reconnects == 0
        assert ctx.codex_daemon_epoch == "4" * 32

    for phase in ("restarting", "failed"):
        asyncio.run(go(phase))


def test_restart_outcome_wait_is_interruptible():
    async def go() -> None:
        machine, _transport = _mk_machine()
        interrupt = asyncio.Event()
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="6" * 32,
            phase="restarting",
            # Keep this barrier live without adding a wall-clock dependency.
            deadline_at=10**12,
        )

        async def stop_waiting() -> None:
            await asyncio.sleep(0.01)
            interrupt.set()

        stopper = asyncio.create_task(stop_waiting())
        started = asyncio.get_running_loop().time()
        state = await machine._codex_restart_state(
            wait=True,
            interrupt_event=interrupt,
        )
        await stopper

        assert state is not None and state.phase == "restarting"
        assert asyncio.get_running_loop().time() - started < 0.5

    asyncio.run(go())


def test_account_switch_continues_running_turn_before_queue_can_drain():
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _AccountSwitchSharedSdk()
        ctx.state = "running"
        ctx.active_msg_id = "logical-turn-a"
        ctx.codex_checkpoint = False
        machine.sessions[ctx.key] = ctx
        ctx.sdk.restart_path = machine._codex_daemon_restart_path
        assert not machine._codex_daemon_restart_path.exists()
        await machine._stamp_codex_daemon_epoch(ctx)
        assert ctx.codex_daemon_epoch == "unmarked"

        ctx.turn_task = asyncio.create_task(
            machine._run_turn(ctx, "task A"))
        await asyncio.wait_for(
            ctx.sdk.continuation_started.wait(), timeout=5.0)

        # The old native turn ended, but logical task A still owns the runtime.
        # runtime-drain.ts therefore cannot release any queued B message yet.
        assert ctx.state == "running"
        assert ctx.turn_task is not None and not ctx.turn_task.done()
        assert ctx.sdk.interrupts == 1
        assert ctx.sdk.reconnects == 1
        assert not [event for event in transport.sent
                    if isinstance(event, Error)]
        assert not [event for event in transport.sent
                    if isinstance(event, TurnEnd)]
        assert len([event for event in transport.sent
                    if isinstance(event, UserMsg)]) == 1
        assert ctx.sdk.queries[0] == ("task A", [])
        assert "<codex_internal_context" in ctx.sdk.queries[1][0]

        ctx.sdk.finish_continuation.set()
        await asyncio.wait_for(ctx.turn_task, timeout=1.0)

        assert ctx.state == "idle"
        terminal = next(
            event for event in transport.sent if isinstance(event, TurnEnd))
        assert terminal.result.subtype == "success"
        assert terminal.turn_id == "turn-new"
        assert any(
            isinstance(event, Delta)
            and event.channel == "final"
            and event.text == "continued on new account"
            for event in transport.sent
        )

    asyncio.run(asyncio.wait_for(go(), timeout=15.0))


def test_account_switch_resumes_usage_limited_goal_without_competing_query():
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _GoalAccountSwitchSharedSdk()
        ctx.state = "running"
        ctx.active_msg_id = "goal-logical-turn"
        ctx.codex_checkpoint = False
        ctx.codex_daemon_epoch = "8" * 32
        machine.sessions[ctx.key] = ctx
        ctx.sdk.restart_path = machine._codex_daemon_restart_path
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="8" * 32,
            phase="ready",
        )

        async def start_goal_turn() -> None:
            await machine._on_codex_turn_lifecycle(
                ctx, "started", "goal-turn")

        ctx.sdk.on_goal_resumed = start_goal_turn
        checkpoints_finished: list[str] = []

        async def finish_checkpoint(session_ctx) -> None:
            checkpoints_finished.append(session_ctx.active_msg_id)

        machine._finish_codex_checkpoint = finish_checkpoint
        managed = asyncio.create_task(machine._run_turn(ctx, "goal task A"))
        ctx.turn_task = managed
        await asyncio.wait_for(ctx.sdk.goal_resumed.wait(), timeout=5.0)
        await asyncio.wait_for(managed, timeout=2.0)

        assert ctx.state == "running"
        assert ctx.codex_spontaneous_turn_id == "goal-turn"
        assert len(ctx.sdk.queries) == 1
        assert checkpoints_finished == ["goal-logical-turn"]
        assert not [event for event in transport.sent
                    if isinstance(event, (Error, TurnEnd))]

        spontaneous = ctx.codex_spontaneous_task
        assert spontaneous is not None
        ctx.sdk.finish_goal.set()
        await asyncio.wait_for(spontaneous, timeout=2.0)

        assert ctx.state == "idle"
        terminal = next(
            event for event in transport.sent if isinstance(event, TurnEnd))
        assert terminal.turn_id == "goal-turn"
        assert terminal.result.subtype == "success"

    asyncio.run(asyncio.wait_for(go(), timeout=15.0))


def test_account_switch_resumes_already_spontaneous_goal_on_new_daemon():
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SpontaneousAccountSwitchSharedSdk()
        ctx.codex_checkpoint = False
        ctx.codex_daemon_epoch = "8" * 32
        machine.sessions[ctx.key] = ctx
        ctx.sdk.restart_path = machine._codex_daemon_restart_path
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="8" * 32,
            phase="ready",
        )
        allow_goal_start = asyncio.Event()

        async def start_delayed_goal_turn() -> None:
            await allow_goal_start.wait()
            await machine._on_codex_turn_lifecycle(
                ctx, "started", "goal-new")

        ctx.sdk.on_goal_resumed = start_delayed_goal_turn

        await machine._on_codex_turn_lifecycle(
            ctx, "started", "goal-old")
        await asyncio.wait_for(
            ctx.sdk.spontaneous_started.wait(), timeout=2.0)
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="9" * 32,
            phase="restarting",
        )
        await asyncio.wait_for(ctx.sdk.goal_activated.wait(), timeout=5.0)
        # The official Goal lifecycle may arrive later than the old two-second
        # heuristic. No ordinary user turn may be submitted in that gap.
        await asyncio.sleep(2.05)
        assert ctx.sdk.queries == []
        assert ctx.state == "running"
        assert not ctx.sdk.continuation_started.is_set()

        allow_goal_start.set()
        await asyncio.wait_for(
            ctx.sdk.continuation_started.wait(), timeout=5.0)

        # Goal A remains the sole owner while the old daemon is replaced. A
        # queued browser message therefore cannot drain into the new daemon.
        assert ctx.state == "running"
        assert ctx.codex_spontaneous_task is not None
        assert not ctx.codex_spontaneous_task.done()
        assert ctx.codex_spontaneous_turn_id == "goal-new"
        assert ctx.sdk.interrupts == 1
        assert ctx.sdk.reconnects == 1
        assert ctx.sdk.goal_activations == 1
        assert ctx.sdk.queries == []
        assert not [
            event for event in transport.sent
            if isinstance(event, (Error, TurnEnd))
        ]

        active = ctx.codex_spontaneous_task
        ctx.sdk.finish_continuation.set()
        await asyncio.wait_for(active, timeout=2.0)

        assert ctx.state == "idle"
        terminal = next(
            event for event in transport.sent if isinstance(event, TurnEnd))
        assert terminal.turn_id == "goal-new"
        assert terminal.result.subtype == "success"
        assert any(
            isinstance(event, Delta)
            and event.channel == "final"
            and event.text == "goal resumed on new account"
            for event in transport.sent
        )

    asyncio.run(asyncio.wait_for(go(), timeout=15.0))


def test_interrupt_during_spontaneous_account_handoff_never_starts_fallback():
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SpontaneousAccountSwitchSharedSdk()
        ctx.codex_checkpoint = False
        ctx.codex_daemon_epoch = "8" * 32
        machine.sessions[ctx.key] = ctx
        ctx.sdk.restart_path = machine._codex_daemon_restart_path
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="8" * 32,
            phase="ready",
        )

        await machine._on_codex_turn_lifecycle(
            ctx, "started", "goal-old")
        active = ctx.codex_spontaneous_task
        await asyncio.wait_for(
            ctx.sdk.spontaneous_started.wait(), timeout=2.0)
        write_restart_state(
            machine._codex_daemon_restart_path,
            epoch="9" * 32,
            phase="restarting",
        )
        await asyncio.wait_for(
            ctx.sdk.goal_activated.wait(), timeout=3.0)
        # Handoff deliberately releases the old id while waiting for app-server
        # to auto-launch. Interrupt must still stop the logical task rather than
        # allowing the hidden fallback query to start afterward.
        await machine._handle_interrupt(SimpleNamespace(sid="sid"))
        assert active is not None
        await asyncio.wait_for(active, timeout=2.0)

        assert ctx.state == "idle"
        assert ctx.codex_spontaneous_turn_id is None
        assert ctx.active_msg_id is None
        assert not ctx.interrupt_event.is_set()
        assert ctx.sdk.queries == []
        terminal = [
            event for event in transport.sent if isinstance(event, TurnEnd)
        ]
        assert len(terminal) == 1
        assert terminal[0].turn_id == "goal-old"
        assert terminal[0].result.subtype == "error_during_execution"

    asyncio.run(asyncio.wait_for(go(), timeout=15.0))


def test_shared_code_final_launch_never_calls_legacy_ownership_probe(
    monkeypatch,
):
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        ctx.state = "running"
        ctx.active_msg_id = "shared-turn"
        ctx.needs_reload = True
        # Keep this unit test independent of Git checkpoint availability.
        ctx.codex_checkpoint = False
        machine.sessions[ctx.key] = ctx

        async def legacy_probe_must_not_run(_sid: str) -> bool:
            raise AssertionError("shared Code final launch probed legacy ownership")

        monkeypatch.setattr(
            machine, "_prime_codex_ownership", legacy_probe_must_not_run
        )

        await asyncio.wait_for(machine._run_turn(ctx, "hello"), timeout=0.5)

        assert ctx.sdk.queries == [("hello", [])]
        assert ctx.sdk.reconnects == 0
        assert ctx.needs_reload is False
        assert ctx.state == "idle"
        assert not [
            event for event in transport.sent
            if isinstance(event, Error) and event.code == "busy"
        ]

    asyncio.run(go())


def test_shared_takeover_is_an_idempotent_noop(monkeypatch, tmp_path):
    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = _SharedSdk()
        machine.sessions[ctx.key] = ctx
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        holder = ProcessIdentity(303, 3003)
        watch = _watch(path)
        watch["holders"] = {holder}
        watch["writers"] = {holder}
        machine._watch["sid"] = watch

        def legacy_watch_must_not_run(_sid: str) -> None:
            raise AssertionError("shared takeover entered legacy watcher")

        monkeypatch.setattr(machine, "_watch_session", legacy_watch_must_not_run)

        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="stale-shared-takeover"
        ))

        assert result is None
        assert ctx.needs_reload is False
        assert watch["takeover_holders"] == set()
        assert watch["takeover_interactive_holders"] == set()
        assert ctx.control_mode == "codex_shared"
        assert ctx.write_state == "writable"
        assert ctx.terminal_attached is True
        state = transport.sent[-1]
        assert state.type == "takeover_state"
        assert state.pending is False
        assert "无需迁移或接管" in state.message

    asyncio.run(go())


def test_final_preflight_never_downgrades_interrupted_shared_proxy(monkeypatch):
    class ProxyFallsBackSdk(_SharedSdk):
        shared_daemon_affinity = True

        @property
        def using_daemon_proxy(self) -> bool:
            return False

    async def go() -> None:
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "code"
        ctx.sdk = ProxyFallsBackSdk()
        ctx.state = "running"
        ctx.active_msg_id = "fallback-turn"
        ctx.codex_checkpoint = False
        machine.sessions[ctx.key] = ctx
        probes: list[str] = []

        async def occupied(sid: str) -> bool:
            probes.append(sid)
            return True

        monkeypatch.setattr(machine, "_prime_codex_ownership", occupied)

        await asyncio.wait_for(machine._run_turn(ctx, "must not send"), timeout=0.5)

        assert probes == []
        assert ctx.sdk.reconnects == 1
        assert ctx.sdk.queries == []
        assert ctx.state == "idle"
        disconnected = [
            event for event in transport.sent
            if isinstance(event, Error) and event.code == "not_running"
        ]
        assert len(disconnected) == 1
        assert disconnected[0].msg_id == "fallback-turn"

    asyncio.run(go())


def test_stdio_work_keeps_legacy_terminal_mutex_and_read_only_state(
    tmp_path, monkeypatch,
):
    async def go() -> None:
        machine, transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.space = "work"
        ctx.sdk = _CodexSdk()
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        holder = ProcessIdentity(202, 2002)
        machine._push_mirrored_history = lambda sid: _record_async(sid)
        path.write_bytes(
            _event("task_started", "work-terminal")
            + _event("task_complete", "work-terminal")
        )

        await machine._poll_codex_watch(
            "sid", watch, {holder}, 1000.0, writers={holder}
        )
        assert watch["external"] is True
        assert ctx.needs_reload is True
        assert ctx.control_mode == "external_cli"
        assert ctx.write_state == "read_only"
        assert ctx.terminal_attached is True
        assert ctx.control_can_takeover is True

        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def occupied(_sid: str) -> bool:
            return True

        monkeypatch.setattr(machine, "_prime_codex_ownership", occupied)
        result = await machine._handle_query(Query(
            sid="sid", prompt="must stay blocked", msg_id="work-query"
        ))
        assert isinstance(result, Error)
        assert result.code == "busy"
        assert result.msg_id == "work-query"
        assert ctx.state == "idle"
        assert ctx.turn_task is None
        assert transport.sent[-1] is result

    async def _record_async(_sid: str) -> None:
        return None

    asyncio.run(go())
