"""Zero-token regressions for wrapper-owned follow-up queries."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    CancelQueuedQuery,
    CommandAck,
    Hello,
    Query,
    QueryQueueState,
    QueuedQueryInfo,
    deserialize,
    is_downstream,
    serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def _deferred(
    msg_id: str,
    *,
    delivery: str = "queue",
    cmd_id: str | None = None,
) -> Query:
    return Query(
        sid="session-queue",
        prompt=msg_id,
        msg_id=msg_id,
        delivery=delivery,
        cmd_id=cmd_id or f"command-{msg_id}",
        client_id="client-queue",
    )


def test_protocol_v25_deferred_query_and_projection_roundtrip():
    queued = _deferred("queued-1")
    assert deserialize(serialize(queued)) == queued
    with pytest.raises(ValidationError):
        Query(
            sid="session-queue",
            prompt="missing reliable identity",
            msg_id="queued-invalid",
            delivery="queue",
        )

    cancel = CancelQueuedQuery(
        sid="session-queue",
        msg_id="queued-1",
        cmd_id="cancel-1",
        client_id="client-queue",
    )
    assert deserialize(serialize(cancel)) == cancel

    state = QueryQueueState(
        sid="session-queue",
        items=[
            QueuedQueryInfo(
                msg_id="queued-1",
                kind="queue",
                prompt_preview="follow up",
                image_count=1,
                file_count=0,
            )
        ],
    )
    assert deserialize(serialize(state)) == state
    assert is_downstream(state) is True


def test_wrapper_starts_queued_query_after_turn_without_browser_callback():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        finish_active = asyncio.Event()

        async def active_turn():
            await finish_active.wait()
            # Match the real drain boundary: idle is published from the turn
            # task, then its finally block/task completion follows.
            await machine._set_state(ctx, "idle")

        ctx.turn_task = asyncio.create_task(active_turn())
        launched: list[Query] = []
        launched_event = asyncio.Event()

        async def launch_without_engine(_ctx, command):
            launched.append(command)
            _ctx.state = "running"
            launched_event.set()

        machine._handle_immediate_query = launch_without_engine

        await machine._process_command(_deferred("queued-after-sleep"))
        assert launched == []
        assert [command.msg_id for command in ctx.queued_queries] == [
            "queued-after-sleep"
        ]
        assert any(
            isinstance(message, CommandAck)
            and message.cmd_id == "command-queued-after-sleep"
            for message in transport.sent
        )

        # No Hello, idle event, or any other browser-originated command follows.
        # The resident wrapper observes the old task's terminal boundary itself.
        finish_active.set()
        await asyncio.wait_for(launched_event.wait(), timeout=1)
        assert [command.msg_id for command in launched] == [
            "queued-after-sleep"
        ]
        assert launched[0].delivery == "immediate"
        assert ctx.queued_queries == []
        projections = [
            message for message in transport.sent
            if isinstance(message, QueryQueueState)
        ]
        assert [item.msg_id for item in projections[0].items] == [
            "queued-after-sleep"
        ]
        assert projections[-1].items == []

        await ctx.turn_task
        if ctx.queued_query_drain_task is not None:
            await ctx.queued_query_drain_task

    asyncio.run(run())


def test_replacement_priority_cancel_and_hello_projection_are_authoritative():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-queue", "session-queue")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        keep_running = asyncio.Event()

        async def active_turn():
            await keep_running.wait()

        ctx.turn_task = asyncio.create_task(active_turn())

        for command in (
            _deferred("append-1"),
            _deferred("append-2"),
            _deferred("replace-1", delivery="replace"),
            _deferred("replace-2", delivery="replace"),
        ):
            await machine._process_command(command)

        assert [
            (command.msg_id, command.delivery)
            for command in ctx.queued_queries
        ] == [
            ("replace-2", "replace"),
            ("append-1", "queue"),
            ("append-2", "queue"),
        ]

        await machine._process_command(CancelQueuedQuery(
            sid=ctx.key,
            msg_id="append-1",
            cmd_id="cancel-append-1",
            client_id="client-queue",
        ))
        assert [command.msg_id for command in ctx.queued_queries] == [
            "replace-2",
            "append-2",
        ]

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client",
            client_id="reconnected-browser",
            cursors={ctx.key: ctx.buffer.tail_seq},
            generations={ctx.key: machine.instance_id},
            route_id="route-new",
        ))
        projection = next(
            message for message in transport.sent
            if isinstance(message, QueryQueueState)
        )
        assert [item.msg_id for item in projection.items] == [
            "replace-2",
            "append-2",
        ]
        assert projection.to == "reconnected-browser"
        assert projection.route_id == "route-new"

        await machine._discard_query_queue(ctx)
        keep_running.set()
        await ctx.turn_task
        assert machine._queued_query_count == 0
        assert machine._queued_query_bytes == 0

    asyncio.run(run())
