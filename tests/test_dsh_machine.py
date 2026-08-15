"""Machine-level regressions for DSH mutation reconciliation."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from cc_remote.protocol import (
    ERR_FORK_RECONCILING,
    ERR_NOT_STEERABLE,
    ERR_PROTOCOL,
    ERR_STEER_UNKNOWN,
    CommandAck,
    ContextReport,
    ConversationTurn,
    Error,
    ForkSession,
    GetContext,
    GetEngineCapabilities,
    GetModels,
    Interrupt,
    ListSessions,
    Models,
    ProcessEvent,
    Query,
    RenameSession,
    SetEffort,
    Steer,
    SwitchSession,
    TurnEnd,
    UserMsg,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.dsh_client import DshSessionHandle, DshUnavailable
from cc_remote.wrapper.dsh_forks import DshForkJournalError
from cc_remote.wrapper.dsh_history import DshHistoryPage
from cc_remote.wrapper.dsh_stream import DshStreamTranslator
from tests.test_multisession import _mk_ctx, _mk_machine


class _DshClient:
    def __init__(
        self,
        *,
        running: bool = True,
        prompt_error: Exception | None = None,
        fork_child: str | None = None,
        fork_error: Exception | None = None,
        cancel_error: Exception | None = None,
        models_value: dict | None = None,
    ) -> None:
        self.running = running
        self.prompt_error = prompt_error
        self.fork_child = fork_child
        self.fork_error = fork_error
        self.cancel_error = cancel_error
        self.models_value = models_value
        self.calls: list[tuple[str, dict, str | None]] = []
        self.response_errors: list[tuple[str, str]] = []

    async def call(
        self,
        method: str,
        payload: dict | None = None,
        *,
        rpc_id: str | None = None,
        no_timeout: bool = False,
    ):
        assert no_timeout is False
        body = dict(payload or {})
        self.calls.append((method, body, rpc_id))
        if method == "session.prompt":
            if self.prompt_error is not None:
                raise self.prompt_error
            return {"queued": True}
        if method == "session.cancel":
            if self.cancel_error is not None:
                raise self.cancel_error
            return {"accepted": True}
        if method == "session.models" and self.models_value is not None:
            return self.models_value
        if method == "session.list":
            return {
                "items": [{
                    "sessionId": body.get("sessionId", "native-session"),
                    "running": self.running,
                }]
            }
        if method == "skill.list":
            return {"skills": [{
                "name": "repo-skill",
                "description": "Use the repository skill",
                "modelInvocable": True,
            }]}
        if method == "session.fork":
            if self.fork_error is not None:
                raise self.fork_error
            if self.fork_child is not None:
                return {"sessionId": self.fork_child}
        raise AssertionError(f"unexpected DSH method: {method}")

    async def respond_error(
        self,
        rpc_id: str,
        *,
        code: str = "cancelled",
        message: str = "Remote interaction was cancelled",
    ) -> bool:
        assert code == "cancelled"
        self.response_errors.append((rpc_id, message))
        return True


class _DshHistory:
    def __init__(self, page_factory: Callable[[], DshHistoryPage]) -> None:
        self.page_factory = page_factory
        self.calls: list[tuple[str, dict]] = []

    async def page(self, session_id: str, **kwargs) -> DshHistoryPage:
        self.calls.append((session_id, dict(kwargs)))
        return self.page_factory()


def _history_page(*client_message_ids: str) -> DshHistoryPage:
    return DshHistoryPage(
        events=(),
        turns=tuple(
            ConversationTurn(
                id=f"dsh-msg-{index}",
                clientMsgId=message_id,
                prompt="steer",
            )
            for index, message_id in enumerate(client_message_ids, start=1)
        ),
        has_more=False,
        oldest_id=("dsh-msg-1" if client_message_ids else None),
        newest_id=(f"dsh-msg-{len(client_message_ids)}"
                   if client_message_ids else None),
        last_seq=len(client_message_ids),
        projections={},
        translator=DshStreamTranslator(),
    )


async def _install_dsh(machine, client: _DshClient, history: _DshHistory) -> None:
    original = machine._dsh_client
    if original is not None:
        await original.stop()
    machine._dsh_client = client
    machine._dsh_history = history
    machine._dsh_available = True
    machine.DSH_RECEIPT_RECONCILE_DELAYS = (0.0,)


@pytest.mark.asyncio
async def test_dsh_sidebar_hides_only_cold_blank_sessions():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    resident = _mk_ctx("dsh@resident-blank", "resident-blank")
    resident.engine = "dsh"
    machine.sessions[resident.key] = resident

    async def catalog():
        return ([
            {
                "sessionId": "cold-blank",
                "updatedAt": 1,
                "running": False,
                "blank": True,
                "cwd": "/tmp/cold",
            },
            {
                "sessionId": "resident-blank",
                "updatedAt": 2,
                "running": False,
                "blank": True,
                "cwd": "/tmp/resident",
            },
            {
                "sessionId": "cold-materialized",
                "updatedAt": 3,
                "running": False,
                "blank": False,
                "cwd": "/tmp/materialized",
            },
        ], set())

    machine._read_dsh_catalog = catalog
    result = await machine._list_dsh_sessions(ListSessions(
        engine="dsh", space="code", client_id="browser",
    ))

    assert [row.session_id for row in result.sessions] == [
        "dsh@resident-blank",
        "dsh@cold-materialized",
    ]
    assert transport.sent[-1] is result


class _CommandClient(_DshClient):
    def __init__(self, *, execution_error: Exception | None = None) -> None:
        super().__init__()
        self.execution_error = execution_error
        self.command_started = asyncio.Event()
        self.release_command = asyncio.Event()

    async def call(
        self,
        method: str,
        payload: dict | None = None,
        *,
        rpc_id: str | None = None,
        no_timeout: bool = False,
    ):
        body = dict(payload or {})
        if method == "commands/list":
            assert no_timeout is False
            self.calls.append((method, body, rpc_id))
            return [{
                "name": "compact",
                "description": "Compact context",
                "input": {"hint": "optional focus"},
            }]
        if method == "commands/execute":
            assert no_timeout is True
            self.calls.append((method, body, rpc_id))
            self.command_started.set()
            await self.release_command.wait()
            if self.execution_error is not None:
                raise self.execution_error
            return {
                "commandId": "native-command-1",
                "result": {"kind": "success", "text": "Compacted"},
            }
        return await super().call(
            method,
            body,
            rpc_id=rpc_id,
            no_timeout=no_timeout,
        )


def _mux_command_event(
    event_type: str,
    seq: int,
    data: dict,
) -> dict:
    return {
        "rpcId": f"command-event-{seq}",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": event_type,
                "seq": seq,
                "time": 1_700_000_000_000 + seq * 100,
                "data": data,
            },
        },
    }


def _install_running_session(machine, client: _DshClient, cwd: str = "/tmp"):
    ctx = _mk_ctx("dsh@native-session", "native-session")
    ctx.engine = "dsh"
    ctx.cwd = cwd
    ctx.state = "running"
    ctx.active_msg_id = "initial-message"
    ctx.sdk = DshSessionHandle(client, "native-session", cwd)
    machine.sessions[ctx.key] = ctx
    return ctx


@pytest.mark.asyncio
async def test_dsh_registered_command_runs_off_lane_and_reconciles_mux_once():
    machine, transport = _mk_machine()
    client = _CommandClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    ctx.active_msg_id = None

    result = await machine._handle_immediate_query(ctx, Query(
        sid=ctx.key,
        prompt="/compact now",
        msg_id="remote-command-message",
        cmd_id="query-command",
        client_id="client-1",
    ))
    await asyncio.wait_for(client.command_started.wait(), timeout=1)
    command_task = ctx.turn_task

    assert result is None
    assert command_task is not None and not command_task.done()
    assert ctx.state == "running"
    assert ctx.dsh_pending_command_msg_id == "remote-command-message"

    # A different DSH surface may issue a command while Remote's handler is
    # running.  It must remain visible; only the exact matching lifecycle is
    # folded into Remote's optimistic row.
    await machine._on_dsh_mux(_mux_command_event(
        "command/run",
        1,
        {
            "commandId": "external-command",
            "name": "feedback",
            "args": " hello",
            "source": {"kind": "user"},
        },
    ))
    await machine._on_dsh_mux(_mux_command_event(
        "command/done",
        2,
        {
            "commandId": "external-command",
            "kind": "success",
            "text": "Recorded",
        },
    ))
    assert any(
        isinstance(message, UserMsg) and message.prompt == "/feedback hello"
        for message in transport.sent
    )

    await machine._on_dsh_mux(_mux_command_event(
        "command/run",
        3,
        {
            "commandId": "native-command-1",
            "name": "compact",
            "args": " now",
            "source": {"kind": "user"},
        },
    ))
    await machine._on_dsh_mux(_mux_command_event(
        "command/done",
        4,
        {
            "commandId": "native-command-1",
            "kind": "success",
            "text": "Compacted",
        },
    ))
    client.release_command.set()
    await asyncio.wait_for(command_task, timeout=1)
    await asyncio.sleep(0)

    remote_users = [
        message for message in transport.sent
        if isinstance(message, UserMsg)
        and message.client_msg_id == "remote-command-message"
    ]
    remote_processes = [
        message for message in transport.sent
        if isinstance(message, ProcessEvent)
        and message.command == "/compact now"
    ]
    remote_terminals = [
        message for message in transport.sent
        if isinstance(message, TurnEnd)
        and message.presentation_id == "remote-command-message"
    ]
    assert len(remote_users) == 1
    assert [message.phase for message in remote_processes] == ["start", "end"]
    assert len(remote_terminals) == 1
    assert remote_terminals[0].turn_id is None
    assert ctx.dsh_command_aliases == {
        "native-command-1": "remote-command-message",
    }
    assert ctx.state == "idle"
    assert ctx.turn_task is None
    assert history.calls[-1][1]["command_aliases"] == {
        "native-command-1": "remote-command-message",
    }


@pytest.mark.asyncio
async def test_dsh_command_unknown_outcome_closes_every_client_projection():
    machine, transport = _mk_machine()
    client = _CommandClient(
        execution_error=DshUnavailable("receipt lost"),
    )
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    ctx.active_msg_id = None

    await machine._handle_immediate_query(ctx, Query(
        sid=ctx.key,
        prompt="/compact",
        msg_id="unknown-command-message",
        cmd_id="query-command",
        client_id="client-1",
    ))
    await asyncio.wait_for(client.command_started.wait(), timeout=1)
    task = ctx.turn_task
    assert task is not None
    client.release_command.set()
    await asyncio.wait_for(task, timeout=1)

    assert any(
        isinstance(message, UserMsg)
        and message.client_msg_id == "unknown-command-message"
        and message.prompt == "/compact"
        for message in transport.sent
    )
    assert any(
        isinstance(message, Error)
        and message.msg_id == "unknown-command-message"
        and "结果未知" in message.message
        for message in transport.sent
    )
    assert ctx.state == "idle"
    assert ctx.dsh_pending_command_msg_id is None
    assert ctx.dsh_pending_command_prompt is None


@pytest.mark.asyncio
async def test_dsh_shutdown_cancels_command_without_forging_user_interrupt():
    machine, transport = _mk_machine()
    client = _CommandClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"
    ctx.active_msg_id = None

    await machine._handle_immediate_query(ctx, Query(
        sid=ctx.key,
        prompt="/compact",
        msg_id="shutdown-command-message",
        cmd_id="query-command",
        client_id="client-1",
    ))
    await asyncio.wait_for(client.command_started.wait(), timeout=1)
    before = len(transport.sent)

    await machine._cancel_dsh_commands_for_shutdown()

    assert ctx.turn_task is None
    assert ctx.dsh_pending_command_msg_id is None
    assert ctx.dsh_pending_command_prompt is None
    assert not any(
        isinstance(message, (UserMsg, TurnEnd))
        for message in transport.sent[before:]
    )


def test_dsh_model_and_capability_reads_reject_cross_engine_scope():
    with pytest.raises(ValueError, match="only supported for DSH"):
        GetModels(engine="codex", session_id="dsh@native-session")
    with pytest.raises(ValueError, match="only supported for Codex"):
        GetModels(engine="dsh", codex_profile_id="default")
    with pytest.raises(ValueError, match="only supported in Code"):
        GetEngineCapabilities(engine="dsh", space="work")
    with pytest.raises(ValueError, match="only supported for Codex"):
        GetEngineCapabilities(
            engine="dsh",
            space="code",
            codex_profile_id="default",
        )
    with pytest.raises(ValueError, match="DSH session id"):
        GetModels(engine="dsh", session_id="native-session")
    with pytest.raises(ValueError, match="DSH engine"):
        SwitchSession(
            engine="claude", session_id="dsh@native-session"
        )
    with pytest.raises(ValueError, match="DSH engine"):
        RenameSession(
            engine="codex",
            session_id="dsh@native-session",
            title="wrong engine",
        )


@pytest.mark.asyncio
async def test_dsh_effort_requires_an_explicit_matching_engine_scope():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    _install_running_session(machine, client)

    result = await machine._handle_set_effort(SetEffort(
        sid="dsh@native-session",
        effort="high",
    ))

    assert isinstance(result, Error)
    assert result.code == ERR_PROTOCOL
    assert client.calls == []


@pytest.mark.asyncio
async def test_dsh_reconcile_reserves_history_sequence_before_source_io():
    machine, transport = _mk_machine()
    client = _DshClient()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    class OrderedHistory(_DshHistory):
        async def page(self, session_id: str, **kwargs) -> DshHistoryPage:
            self.calls.append((session_id, dict(kwargs)))
            if len(self.calls) == 1:
                first_started.set()
                await release_first.wait()
                return _history_page("older")
            second_started.set()
            await release_second.wait()
            return _history_page("newer")

    history = OrderedHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    reconcile = asyncio.create_task(machine._reconcile_dsh_resident(ctx))
    await first_started.wait()
    requested = asyncio.create_task(machine._build_dsh_history(
        ctx.key,
        before=None,
        limit=4,
        detail="summary",
    ))
    await second_started.wait()
    release_second.set()
    newest = await requested
    release_first.set()
    await reconcile

    emitted = [message for message in transport.sent if message.type == "history"]
    assert newest.build_seq == 2
    assert emitted[-1].build_seq == 1
    assert machine._history_build_sequences[ctx.key] == 2


@pytest.mark.asyncio
async def test_dsh_reconcile_does_not_treat_a_truncated_active_tail_as_idle():
    machine, transport = _mk_machine()
    client = _DshClient(running=True)
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.state = "idle"

    await machine._reconcile_dsh_resident(ctx)

    assert ctx.state == "running"
    pages = [event for event in transport.sent if event.type == "history"]
    assert pages[-1].in_progress is True
    assert any(
        method == "session.list" for method, _payload, _rpc in client.calls
    )


@pytest.mark.asyncio
async def test_dsh_live_acceptance_wins_over_an_older_idle_catalog_read():
    machine, transport = _mk_machine()

    class DelayedIdleClient(_DshClient):
        def __init__(self) -> None:
            super().__init__(running=False)
            self.list_started = asyncio.Event()
            self.release_list = asyncio.Event()

        async def call(self, method: str, payload=None, **kwargs):
            if method == "session.list":
                self.list_started.set()
                await self.release_list.wait()
            return await super().call(method, payload, **kwargs)

    client = DelayedIdleClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.dsh_uncertain_msg_id = "remote-message"

    reconciliation = asyncio.create_task(
        machine._reconcile_dsh_resident(
            ctx, expected_msg_id="remote-message",
        )
    )
    await client.list_started.wait()
    await machine._on_dsh_mux({
        "rpcId": "event-1",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": "user/message",
                "seq": 1,
                "time": 1_700_000_000_000,
                "surfaceOp": "append",
                "data": {
                    "id": "native-message",
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                    "source": {"kind": "user", "rpcId": "remote-message"},
                },
            },
        },
    })
    client.release_list.set()

    assert await reconciliation is True
    assert ctx.dsh_uncertain_msg_id is None
    assert ctx.state == "running"
    pages = [event for event in transport.sent if event.type == "history"]
    assert pages[-1].in_progress is True


@pytest.mark.asyncio
async def test_dsh_context_read_reports_unavailable_without_calling_sdk_api():
    machine, transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_get_context(GetContext(
        sid=ctx.key,
        cmd_id="context-command",
        client_id="client-1",
    ))

    assert isinstance(result, ContextReport)
    assert result.available is False
    assert result.total_tokens == 0
    assert result.max_tokens == 0
    assert result.percentage == 0.0
    assert client.calls == []
    assert transport.sent[-1] is result


@pytest.mark.asyncio
async def test_dsh_skill_catalog_is_bound_to_the_requested_resident_session():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_get_engine_capabilities(
        GetEngineCapabilities(
            engine="dsh",
            space="code",
            session_id=ctx.key,
            cwd=ctx.cwd,
            client_id="client-1",
        )
    )

    assert result.session_id == ctx.key
    assert [item.name for item in result.items] == ["repo-skill"]
    assert client.calls[-1] == (
        "skill.list", {"sessionId": "native-session"}, None
    )


@pytest.mark.asyncio
async def test_dsh_model_catalog_response_is_scoped_to_the_requested_session():
    machine, transport = _mk_machine()
    client = _DshClient(models_value={
        "current": {
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            "reasoningEffort": "high",
        },
        "groups": [{
            "id": "deepseek-official",
            "name": "DeepSeek",
            "models": [{
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "reasoning": {
                    "efforts": [{"id": "high"}],
                    "defaultEffort": "high",
                },
            }],
        }],
    })
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_get_dsh_models(GetModels(
        engine="dsh",
        session_id=ctx.key,
        cmd_id="models-command",
        client_id="client-1",
    ))

    assert isinstance(result, Models)
    assert result.session_id == ctx.key
    assert result.default_model == (
        "dsh://deepseek-official/deepseek-v4-flash"
    )
    assert result.default_effort == "high"
    assert ctx.sdk.model == result.default_model
    assert ctx.sdk.effort == "high"
    assert client.calls == [(
        "session.models", {"sessionId": "native-session"}, None,
    )]
    assert all(
        getattr(message, "sid", None) == ctx.key
        for message in transport.sent[-2:]
    )


@pytest.mark.asyncio
async def test_dsh_interaction_overflow_fails_closed_without_spawning_a_task():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    _install_running_session(machine, client)
    machine.DSH_INTERACTION_TASK_CAP = 0

    await machine._on_dsh_mux({
        "rpcId": "approval-rpc",
        "payload": {
            "type": "approval/requested",
            "sessionId": "native-session",
            "approvalId": "approval-1",
            "toolName": "bash",
        },
    })

    assert client.response_errors == [(
        "approval-rpc", "Remote interaction capacity was reached",
    )]
    assert machine._dsh_interaction_tasks == set()
    assert machine._dsh_pending_approvals == {}


@pytest.mark.asyncio
async def test_dsh_question_resolution_is_scoped_to_one_session():
    machine, _transport = _mk_machine()
    first = _mk_ctx("first", "dsh@first")
    second = _mk_ctx("second", "dsh@second")
    first_wait = asyncio.get_running_loop().create_future()
    second_wait = asyncio.get_running_loop().create_future()
    first.pending_asks["first-ask"] = first_wait
    second.pending_asks["second-ask"] = second_wait
    machine._dsh_pending_request_asks = {
        ("dsh@first", "same-rpc"): {"first-ask"},
        ("dsh@second", "same-rpc"): {"second-ask"},
    }

    machine._cancel_dsh_request_asks(first, "dsh@first", "same-rpc")

    assert first_wait.done()
    assert first_wait.exception() is not None
    assert not second_wait.done()
    assert ("dsh@second", "same-rpc") in machine._dsh_pending_request_asks
    second_wait.cancel()


@pytest.mark.asyncio
async def test_dsh_interrupt_receipt_loss_does_not_force_a_running_turn_idle():
    machine, _transport = _mk_machine()
    client = _DshClient(cancel_error=DshUnavailable("receipt lost"))
    translator = DshStreamTranslator()
    translator.feed({
        "type": "turn/start",
        "seq": 1,
        "time": 1_700_000_000_000,
        "data": {"turn": 0},
    })
    page = _history_page()
    active_page = DshHistoryPage(
        events=page.events,
        turns=page.turns,
        has_more=page.has_more,
        oldest_id=page.oldest_id,
        newest_id=page.newest_id,
        last_seq=1,
        projections=page.projections,
        translator=translator,
    )
    history = _DshHistory(lambda: active_page)
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    await machine._handle_interrupt(Interrupt(
        sid=ctx.key,
        cmd_id="interrupt-command",
        client_id="client-1",
    ))
    for _ in range(20):
        if ctx.state == "running":
            break
        await asyncio.sleep(0)

    assert ctx.state == "running"
    assert client.calls == [(
        "session.cancel", {"sessionId": "native-session"}, None,
    )]


@pytest.mark.asyncio
async def test_dsh_lost_steer_receipt_accepts_only_exact_history_identity():
    machine, _transport = _mk_machine()
    client = _DshClient(prompt_error=DshUnavailable("receipt lost"))
    history = _DshHistory(lambda: _history_page("steer-message"))
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command",
        client_id="client-1",
        prompt="continue",
        msg_id="steer-message",
    ))

    assert result is None
    assert ctx.dsh_uncertain_steer_id is None
    assert [call[0] for call in client.calls] == [
        "session.prompt", "session.list",
    ]


@pytest.mark.asyncio
async def test_dsh_lost_steer_receipt_rejects_when_idle_history_has_no_identity():
    machine, _transport = _mk_machine()
    client = _DshClient(
        running=False,
        prompt_error=DshUnavailable("receipt lost"),
    )
    history = _DshHistory(lambda: _history_page("another-message"))
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    result = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command",
        client_id="client-1",
        prompt="continue",
        msg_id="steer-message",
    ))

    assert isinstance(result, Error)
    assert result.code == ERR_NOT_STEERABLE
    assert ctx.dsh_uncertain_steer_id is None
    assert [call[0] for call in client.calls] == [
        "session.prompt", "session.list",
    ]


@pytest.mark.asyncio
async def test_dsh_unknown_steer_stays_fail_closed_and_blocks_a_second_submit():
    machine, _transport = _mk_machine()
    client = _DshClient(prompt_error=DshUnavailable("receipt lost"))
    history = _DshHistory(lambda: _history_page("another-message"))
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)

    first = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command",
        client_id="client-1",
        prompt="continue",
        msg_id="steer-message",
    ))
    second = await machine._handle_steer(Steer(
        sid=ctx.key,
        cmd_id="steer-command-2",
        client_id="client-1",
        prompt="do not duplicate",
        msg_id="steer-message-2",
    ))

    assert isinstance(first, Error) and first.code == ERR_STEER_UNKNOWN
    assert isinstance(second, Error) and second.code == ERR_STEER_UNKNOWN
    assert ctx.dsh_uncertain_steer_id == "steer-message"
    assert [call[0] for call in client.calls].count("session.prompt") == 1


@pytest.mark.asyncio
async def test_dsh_turn_end_does_not_guess_an_unknown_steer_was_accepted():
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    ctx = _install_running_session(machine, client)
    ctx.dsh_uncertain_steer_id = "steer-message"

    await machine._on_dsh_mux({
        "rpcId": "event-1",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": "turn/start", "seq": 1, "time": 1_700_000_000_000,
                "data": {"turn": 0},
            },
        },
    })
    await machine._on_dsh_mux({
        "rpcId": "event-2",
        "payload": {
            "type": "session/event",
            "sessionId": "native-session",
            "event": {
                "type": "turn/end", "seq": 2, "time": 1_700_000_001_000,
                "data": {"turn": 0, "reason": {"kind": "completed"}},
            },
        },
    })

    assert ctx.state == "idle"
    assert ctx.dsh_uncertain_steer_id == "steer-message"


@pytest.mark.asyncio
async def test_dsh_fork_is_not_acked_when_result_journal_fails_after_submit(
    monkeypatch,
    tmp_path,
):
    machine, transport = _mk_machine()
    client = _DshClient(fork_child="child-session")
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    machine.cfg.cc_cwd = str(tmp_path)

    async def catalog():
        return ([{
            "sessionId": "parent-session",
            "cwd": str(tmp_path),
            "origin": "user",
        }], set())

    machine._read_dsh_catalog = catalog
    journal = machine._dsh_forks
    assert journal is not None

    def fail_complete(_request_id: str, _session_id: str):
        raise DshForkJournalError("synthetic complete failure")

    monkeypatch.setattr(journal, "complete", fail_complete)
    command = ForkSession(
        session_id="dsh@parent-session",
        request_id="fork-request",
        last_turn_id="dsh-seq-20",
        cmd_id="fork-command",
        client_id="client-1",
    )

    with pytest.raises(machine_module._ForkOutcomeUncertain):
        await machine._process_command(command)

    entry = journal.get("fork-request")
    assert entry is not None and entry["status"] == "submitted"
    assert any(
        isinstance(message, Error) and message.code == ERR_FORK_RECONCILING
        for message in transport.sent
    )
    assert not any(isinstance(message, CommandAck) for message in transport.sent)
    assert [call[0] for call in client.calls] == ["session.fork"]


@pytest.mark.asyncio
async def test_dsh_fork_reconciliation_reports_unavailable_source_without_ack(
    monkeypatch,
    tmp_path,
):
    machine, transport = _mk_machine()
    client = _DshClient(fork_error=DshUnavailable("receipt lost"))
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)
    machine.cfg.cc_cwd = str(tmp_path)

    async def catalog():
        return ([{
            "sessionId": "parent-session",
            "cwd": str(tmp_path),
            "origin": "user",
        }], set())

    async def unavailable_candidate(_entry, *, attempts=1):
        assert attempts == 4
        raise DshUnavailable("catalog offline")

    machine._read_dsh_catalog = catalog
    monkeypatch.setattr(
        machine,
        "_dsh_fork_candidate",
        unavailable_candidate,
    )
    command = ForkSession(
        session_id="dsh@parent-session",
        request_id="fork-unavailable",
        last_turn_id="dsh-seq-20",
        cmd_id="fork-command",
        client_id="client-1",
    )

    with pytest.raises(machine_module._ForkOutcomeUncertain):
        await machine._process_command(command)

    journal = machine._dsh_forks
    assert journal is not None
    entry = journal.get("fork-unavailable")
    assert entry is not None and entry["status"] == "uncertain"
    assert any(
        isinstance(message, Error) and message.code == ERR_FORK_RECONCILING
        for message in transport.sent
    )
    assert not any(isinstance(message, CommandAck) for message in transport.sent)


@pytest.mark.asyncio
async def test_dsh_fork_reconciliation_reads_the_durable_cut_not_moving_tail(
    monkeypatch,
):
    machine, _transport = _mk_machine()
    client = _DshClient()
    history = _DshHistory(lambda: _history_page())
    await _install_dsh(machine, client, history)

    async def catalog():
        return ([{
            "sessionId": "child-session",
            "parentSessionId": "parent-session",
            "origin": "user",
        }], set())

    calls: list[tuple[str, dict]] = []

    async def call(method: str, payload=None, *, rpc_id=None):
        assert rpc_id is None
        body = dict(payload or {})
        calls.append((method, body))
        assert method == "session.history"
        return {
            "events": [{
                "event": {"type": "turn/end", "seq": 20},
            }],
            "hasMore": True,
        }

    machine._read_dsh_catalog = catalog
    monkeypatch.setattr(client, "call", call)

    child = await machine._dsh_fork_candidate({
        "baseline_session_ids": ["parent-session"],
        "native_parent_session_id": "parent-session",
        "at_seq": 20,
    })

    assert child == "child-session"
    assert calls == [("session.history", {
        "sessionId": "child-session",
        "beforeSeq": 21,
        "maxMessages": 1,
    })]
