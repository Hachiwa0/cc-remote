"""Live bridge regressions for Codex goal/automatic continuation turns."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from cc_remote.protocol import (
    Delta, Error, GoalState, ProcessEvent, StateEvent, ToolDelta, ToolResult,
    ToolUse, TurnDiff, TurnEnd, TurnPlan, UserMsg,
)
from cc_remote.wrapper.codex_handle import (
    CodexHandle, CodexSpontaneousClosed, CodexSpontaneousOverflow,
)
from tests.test_multisession import _mk_ctx, _mk_machine


class _Cfg:
    cc_cwd = "/tmp"
    tool_result_max = 8_000
    turn_reader_queue_cap = 4
    ws_max_size_bytes = 16 * 1024 * 1024


def _notification(method: str, turn_id: str, **params):
    return {
        "method": method,
        "params": {
            "threadId": "thread-spontaneous",
            "turnId": turn_id,
            **params,
        },
    }


def _goal_notification(
    objective: str,
    *,
    turn_id: str | None,
    status: str = "active",
    created_at: int = 1,
    updated_at: int = 1,
):
    params = {
        "threadId": "thread-spontaneous",
        "goal": {
            "threadId": "thread-spontaneous",
            "objective": objective,
            "status": status,
            "tokensUsed": 0,
            "timeUsedSeconds": 0,
            "createdAt": created_at,
            "updatedAt": updated_at,
        },
    }
    if turn_id is not None:
        params["turnId"] = turn_id
    return {"method": "thread/goal/updated", "params": params}


def test_spontaneous_bridge_is_bounded_nonblocking_and_keeps_terminal_frame():
    async def run():
        lifecycle = []

        async def on_lifecycle(phase, turn_id):
            lifecycle.append((phase, turn_id))

        handle = CodexHandle(_Cfg(), turn_lifecycle_callback=on_lifecycle)
        handle.thread_id = "thread-spontaneous"
        await asyncio.wait_for(handle._dispatch(_notification(
            "turn/started", "auto-overflow",
            turn={"id": "auto-overflow"},
        )), timeout=0.1)

        # Charge one otherwise-small frame above the bridge's byte ceiling. The
        # stdout path must return immediately instead of waiting for a consumer.
        await asyncio.wait_for(handle._dispatch(_notification(
            "item/agentMessage/delta", "auto-overflow",
            itemId="answer", delta="not retained",
        ), raw_size=8 * 1024 * 1024), timeout=0.1)
        await asyncio.wait_for(handle._dispatch(_notification(
            "turn/completed", "auto-overflow",
            turn={"id": "auto-overflow", "status": "completed"},
        )), timeout=0.1)

        items = [item async for item in
                 handle.receive_spontaneous_response("auto-overflow")]
        assert isinstance(items[0], CodexSpontaneousOverflow)
        assert items[-1]["method"] == "turn/completed"
        assert all(not (isinstance(item, dict)
                        and item.get("method") == "item/agentMessage/delta")
                   for item in items)
        assert lifecycle == [
            ("started", "auto-overflow"),
            ("completed", "auto-overflow"),
        ]

    asyncio.run(run())


def test_spontaneous_bridge_keeps_streaming_complete_items_after_gap():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "auto-gap-recovery"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        queue = handle._spontaneous_q
        stream = handle.receive_spontaneous_response(turn_id)

        await asyncio.wait_for(handle._dispatch(_notification(
            "item/agentMessage/delta", turn_id,
            itemId="lost-answer", delta="lost",
        ), raw_size=queue.max_bytes + 1), timeout=0.1)

        # Unknown deltas remain unsafe after a gap, but a subsequent explicit
        # item lifecycle must be retained behind the still-unconsumed marker.
        # Buffered stdout commonly delivers this whole sequence before the
        # independent relay-facing consumer gets its first scheduling turn.
        await handle._dispatch(_notification(
            "item/agentMessage/delta", turn_id,
            itemId="lost-answer", delta="orphan",
        ))
        fresh = [
            _notification(
                "item/started", turn_id,
                item={"id": "fresh-answer", "type": "agentMessage"},
            ),
            _notification(
                "item/agentMessage/delta", turn_id,
                itemId="fresh-answer", delta="new live detail",
            ),
            _notification(
                "item/completed", turn_id,
                item={
                    "id": "fresh-answer",
                    "type": "agentMessage",
                    "text": "new live detail",
                },
            ),
        ]
        for message in fresh:
            await handle._dispatch(message)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        rest = [item async for item in stream]
        assert isinstance(rest[0], CodexSpontaneousOverflow)
        assert rest[1:-1] == fresh
        assert rest[-1]["method"] == "turn/completed"
        assert all(
            (item.get("params") or {}).get("delta") != "orphan"
            for item in rest
            if isinstance(item, dict)
        )

    asyncio.run(run())


def test_spontaneous_bridge_completion_snapshot_and_terminal_win_after_gap():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "auto-completion-race"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        queue = handle._spontaneous_q

        await handle._dispatch(_notification(
            "item/agentMessage/delta", turn_id,
            itemId="lost", delta="lost",
        ), raw_size=queue.max_bytes + 1)

        completion = _notification(
            "item/completed", turn_id,
            item={
                "id": "completion-only-answer",
                "type": "agentMessage",
                "text": "final snapshot",
            },
        )
        await handle._dispatch(completion)
        # EOF/close may race the final app-server frame. The terminal must replace
        # that provisional close without erasing the retained gap or completion.
        handle._close_spontaneous_stream(turn_id)
        terminal = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(terminal, raw_size=queue.max_bytes + 1)
        # Duplicate terminal and a later close must not add another end frame.
        await handle._dispatch(terminal, raw_size=queue.max_bytes + 1)
        handle._close_spontaneous_stream(turn_id)

        frames = [
            item async for item in
            handle.receive_spontaneous_response(turn_id)
        ]
        assert isinstance(frames[0], CodexSpontaneousOverflow)
        assert frames[1] == completion
        assert frames[2]["method"] == "turn/completed"
        assert frames[2]["params"]["turn"]["status"] == "interrupted"
        assert len(frames) == 3
        assert not any(
            isinstance(item, CodexSpontaneousClosed) for item in frames)

    asyncio.run(run())


def test_compaction_interrupted_boundary_keeps_same_turn_stream_open():
    async def run():
        lifecycle = []

        async def on_lifecycle(phase, turn_id):
            lifecycle.append((phase, turn_id))

        handle = CodexHandle(_Cfg(), turn_lifecycle_callback=on_lifecycle)
        handle.thread_id = "thread-spontaneous"
        turn_id = "compact-continuation"
        started = _notification(
            "turn/started", turn_id, turn={"id": turn_id},
        )
        await handle._dispatch(started)
        compacted = _notification("thread/compacted", turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))

        assert handle.turn_active is True
        assert handle.turn_id == turn_id
        assert handle._spontaneous_turn_id == turn_id
        assert lifecycle == [("started", turn_id)]

        continuation = [
            _notification(
                "item/started", turn_id,
                item={"id": "after-compact", "type": "agentMessage"},
            ),
            _notification(
                "item/agentMessage/delta", turn_id,
                itemId="after-compact", delta="continued",
            ),
            _notification(
                "item/completed", turn_id,
                item={
                    "id": "after-compact",
                    "type": "agentMessage",
                    "text": "continued",
                },
            ),
        ]
        for message in continuation:
            await handle._dispatch(message)
        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        )
        await handle._dispatch(final)

        frames = [
            item async for item in
            handle.receive_spontaneous_response(turn_id)
        ]
        assert frames == [started, compacted, *continuation, final]
        assert lifecycle == [
            ("started", turn_id),
            ("completed", turn_id),
        ]
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_managed_compaction_interrupted_boundary_keeps_response_open():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        turn_id = "managed-compact-continuation"
        handle.turn_id = turn_id
        handle.turn_active = True
        handle._open_managed_stream()

        compacted = _notification("thread/compacted", turn_id)
        await handle._dispatch(compacted)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        ))
        assert handle.turn_active is True
        assert handle.turn_id == turn_id

        continuation = _notification(
            "item/completed", turn_id,
            item={
                "id": "managed-after-compact",
                "type": "agentMessage",
                "text": "continued",
            },
        )
        await handle._dispatch(continuation)
        final = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(final)

        frames = [item async for item in handle.receive_response()]
        assert frames == [compacted, continuation, final]
        assert frames[-1]["params"]["turn"]["status"] == "interrupted"
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_plain_interrupted_boundary_remains_terminal():
    async def run():
        lifecycle = []

        async def on_lifecycle(phase, turn_id):
            lifecycle.append((phase, turn_id))

        handle = CodexHandle(_Cfg(), turn_lifecycle_callback=on_lifecycle)
        handle.thread_id = "thread-spontaneous"
        turn_id = "plain-interrupted"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        terminal = _notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "interrupted"},
        )
        await handle._dispatch(terminal)

        frames = [
            item async for item in
            handle.receive_spontaneous_response(turn_id)
        ]
        assert frames[-1] == terminal
        assert lifecycle == [
            ("started", turn_id),
            ("completed", turn_id),
        ]
        assert handle.turn_active is False
        assert handle.turn_id is None

    asyncio.run(run())


def test_stdout_reader_drains_burst_when_spontaneous_consumer_is_stalled():
    async def run():
        turn_id = "auto-burst"
        messages = [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            *[
                _notification("item/agentMessage/delta", turn_id,
                              itemId="answer", delta=str(index))
                for index in range(96)
            ],
            _notification("turn/completed", turn_id,
                          turn={"id": turn_id, "status": "completed"}),
        ]
        lines = [json.dumps(message).encode() + b"\n" for message in messages]
        lines.append(b"")

        class Stdout:
            reads = 0

            async def readline(self):
                self.reads += 1
                return lines.pop(0)

        stdout = Stdout()
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        await asyncio.wait_for(handle._read_loop(
            SimpleNamespace(stdout=stdout), handle._generation), timeout=0.5)
        assert stdout.reads == len(messages) + 1

        items = [item async for item in
                 handle.receive_spontaneous_response(turn_id)]
        assert isinstance(items[0], CodexSpontaneousOverflow)
        assert items[-1]["method"] == "turn/completed"

    asyncio.run(run())


def test_spontaneous_bridge_closes_on_disconnect_without_raw_error_data():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        await handle._dispatch(_notification(
            "turn/started", "auto-closed", turn={"id": "auto-closed"},
        ))
        await handle.disconnect()
        items = [item async for item in
                 handle.receive_spontaneous_response("auto-closed")]
        assert items[0]["method"] == "turn/started"
        assert isinstance(items[-1], CodexSpontaneousClosed)

    asyncio.run(run())


def test_managed_turn_never_double_routes_into_spontaneous_bridge():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.turn_active = True
        handle._turn_q = asyncio.Queue()
        await handle._dispatch(_notification(
            "turn/started", "managed-turn", turn={"id": "managed-turn"},
        ))
        delta = _notification(
            "item/agentMessage/delta", "managed-turn",
            itemId="answer", delta="managed",
        )
        await handle._dispatch(delta)
        assert handle._spontaneous_q is None
        assert handle._spontaneous_turn_id is None
        assert (await handle._turn_q.get())["method"] == "turn/started"
        assert await handle._turn_q.get() == delta

    asyncio.run(run())


def test_old_managed_consumer_cannot_clear_new_spontaneous_active_flag():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        old_queue = asyncio.Queue()
        handle._turn_q = old_queue
        handle.turn_active = False

        async def consume_old_queue():
            return [message async for message in handle.receive_response()]

        consumer = asyncio.create_task(consume_old_queue())
        await asyncio.sleep(0)
        await handle._dispatch(_notification(
            "turn/started", "auto-after-managed",
            turn={"id": "auto-after-managed"},
        ))
        old_queue.put_nowait(None)
        assert await consumer == []
        assert handle.turn_active is True
        assert handle._spontaneous_turn_id == "auto-after-managed"

    asyncio.run(run())


def test_machine_streams_rich_spontaneous_turn_and_unlocks_on_matching_terminal():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "auto-rich"
        messages = [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            _notification("item/reasoning/summaryPartAdded", turn_id,
                          itemId="reasoning-1", summaryIndex=0),
            _notification("item/reasoning/summaryTextDelta", turn_id,
                          itemId="reasoning-1", summaryIndex=0,
                          delta="公开思考摘要"),
            _notification("turn/plan/updated", turn_id,
                          explanation="执行计划",
                          plan=[{"step": "检查", "status": "inProgress"}]),
            _notification("item/started", turn_id, item={
                "type": "commandExecution", "id": "command-1",
                "command": "pwd", "cwd": "/repo", "status": "inProgress",
                "commandActions": [],
            }),
            _notification("item/commandExecution/outputDelta", turn_id,
                          itemId="command-1", delta="/repo\n"),
            _notification("item/completed", turn_id, item={
                "type": "commandExecution", "id": "command-1",
                "command": "pwd", "cwd": "/repo", "status": "completed",
                "commandActions": [], "aggregatedOutput": "/repo\n",
                "exitCode": 0, "durationMs": 4,
            }),
            _notification("turn/diff/updated", turn_id,
                          diff="@@ -1 +1 @@\n-old\n+new"),
            _notification("item/started", turn_id, item={
                "type": "mcpToolCall", "id": "mcp-1", "server": "docs",
                "tool": "lookup", "status": "inProgress",
                "arguments": {"query": "sdk"},
            }),
            _notification("item/mcpToolCall/progress", turn_id,
                          itemId="mcp-1", message="50%"),
            _notification("item/completed", turn_id, item={
                "type": "mcpToolCall", "id": "mcp-1", "server": "docs",
                "tool": "lookup", "status": "completed",
                "arguments": {"query": "sdk"},
                "result": {"content": [{"type": "text", "text": "ok"}]},
                "durationMs": 5,
            }),
            _notification("item/started", turn_id, item={
                "type": "collabAgentToolCall", "id": "agent-1",
                "tool": "spawnAgent", "status": "inProgress",
                "senderThreadId": "thread-spontaneous",
                "receiverThreadIds": ["child-1"], "prompt": "inspect",
            }),
            _notification("hook/completed", turn_id, run={
                "id": "hook-1", "eventName": "preToolUse",
                "handlerType": "command", "status": "completed",
                "durationMs": 2,
            }),
            _notification("item/completed", turn_id, item={
                "type": "agentMessage", "id": "answer-1",
                "text": "最终答案", "phase": "final_answer",
            }),
            _notification("turn/completed", turn_id, turn={
                "id": turn_id, "status": "completed", "durationMs": 25,
            }),
        ]
        for message in messages:
            await handle._dispatch(message)

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        assert ctx.state == "idle"
        assert ctx.codex_spontaneous_turn_id is None
        anchors = [event for event in transport.sent if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in anchors] == [(turn_id, "")]
        assert [event.state for event in transport.sent
                if isinstance(event, StateEvent)] == ["running", "idle"]
        assert any(isinstance(event, Delta) and event.text == "最终答案"
                   for event in transport.sent)
        assert any(isinstance(event, TurnPlan) for event in transport.sent)
        assert any(isinstance(event, TurnDiff) for event in transport.sent)
        assert {event.category for event in transport.sent
                if isinstance(event, ToolUse)} == {"command", "mcp"}
        assert any(isinstance(event, ToolDelta) for event in transport.sent)
        assert len([event for event in transport.sent
                    if isinstance(event, ToolResult)]) == 2
        assert {event.kind for event in transport.sent
                if isinstance(event, ProcessEvent)} >= {"reasoning", "agent", "hook"}
        terminal = [event for event in transport.sent if isinstance(event, TurnEnd)]
        assert len(terminal) == 1
        assert terminal[0].turn_id == turn_id
        assert terminal[0].result.subtype == "success"

    asyncio.run(run())


def test_goal_objective_before_turn_start_becomes_the_user_prompt_once():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.goal_callback = lambda goal: machine._on_codex_goal(ctx, goal)
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "goal-before-start"
        goal = _goal_notification("证明泰勒展开", turn_id=turn_id)
        await handle._dispatch(goal)
        # A retried native notification must not enqueue the objective twice.
        await handle._dispatch(goal)
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        anchors = [
            event for event in transport.sent
            if isinstance(event, UserMsg) and event.prompt
        ]
        assert [(event.msg_id, event.prompt) for event in anchors] == [
            (turn_id, "证明泰勒展开"),
        ]
        assert any(isinstance(event, GoalState) for event in transport.sent)

    asyncio.run(run())


def test_goal_objective_after_turn_start_patches_the_empty_anchor_once():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.goal_callback = lambda goal: machine._on_codex_goal(ctx, goal)
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "goal-after-start"
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        await asyncio.sleep(0)
        goal = _goal_notification("证明余项收敛", turn_id=turn_id)
        await handle._dispatch(goal)
        await handle._dispatch(goal)
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        anchors = [
            event for event in transport.sent
            if isinstance(event, UserMsg) and event.prompt
        ]
        assert [(event.msg_id, event.prompt) for event in anchors] == [
            (turn_id, "证明余项收敛"),
        ]

    asyncio.run(run())


def test_goal_status_only_continuation_keeps_an_empty_anchor():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "既有目标"
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.goal_callback = lambda goal: machine._on_codex_goal(ctx, goal)
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "goal-resume"
        await handle._dispatch(_goal_notification(
            "既有目标", turn_id=turn_id, created_at=1, updated_at=2,
        ))
        await handle._dispatch(_notification(
            "turn/started", turn_id, turn={"id": turn_id},
        ))
        await handle._dispatch(_notification(
            "turn/completed", turn_id,
            turn={"id": turn_id, "status": "completed"},
        ))

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        anchors = [event for event in transport.sent
                   if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in anchors] == [
            (turn_id, ""),
        ]

    asyncio.run(run())


def test_goal_modify_after_reconnect_primes_baseline_before_set():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        requests = []

        async def request(method, params=None):
            requests.append((method, params))
            if method == "thread/goal/get":
                return {"goal": {
                    "threadId": handle.thread_id,
                    "objective": "旧目标",
                    "status": "paused",
                    "tokensUsed": 10,
                    "timeUsedSeconds": 20,
                    "createdAt": 1,
                    "updatedAt": 2,
                }}
            if method == "thread/goal/set":
                goal = {
                    "threadId": handle.thread_id,
                    "objective": "新目标",
                    "status": "active",
                    "tokensUsed": 10,
                    "timeUsedSeconds": 20,
                    "createdAt": 1,
                    "updatedAt": 3,
                }
                await handle._dispatch(_goal_notification(
                    "新目标", turn_id=None, created_at=1, updated_at=3,
                ))
                await handle._dispatch(_notification(
                    "turn/started", "goal-modified",
                    turn={"id": "goal-modified"},
                ))
                return {"goal": goal}
            raise AssertionError(method)

        handle._request = request
        await handle.set_goal(objective="新目标", status="active")

        assert requests == [
            ("thread/goal/get", {"threadId": handle.thread_id}),
            ("thread/goal/set", {
                "threadId": handle.thread_id,
                "objective": "新目标",
                "status": "active",
            }),
        ]
        assert handle.take_goal_prompt("goal-modified") == "新目标"

    asyncio.run(run())


def test_unbound_goal_prompt_never_binds_a_managed_user_turn():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        handle._goal_objective_baseline = "旧目标"
        handle._goal_baseline_loaded = True

        await handle._dispatch(_goal_notification(
            "新目标", turn_id=None, created_at=1, updated_at=2,
        ))
        assert handle._goal_prompt_unbound == "新目标"

        # query() claims turn_active before the authoritative notification.  A
        # stale Goal candidate must be discarded instead of stealing this turn.
        handle.turn_active = True
        await handle._dispatch(_notification(
            "turn/started", "ordinary-turn",
            turn={"id": "ordinary-turn"},
        ))
        assert handle.take_goal_prompt("ordinary-turn") is None
        assert handle._goal_prompt_unbound is None

    asyncio.run(run())


def test_spontaneous_overflow_preserves_authoritative_success_terminal():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-overflow", "thread-overflow")
        ctx.engine = "codex"
        ctx.state = "running"
        turn_id = "auto-overflow-success"
        ctx.codex_spontaneous_turn_id = turn_id

        class OverflowSdk:
            async def receive_spontaneous_response(self, requested_turn_id):
                assert requested_turn_id == turn_id
                yield CodexSpontaneousOverflow(turn_id)
                yield _notification(
                    "item/started", turn_id,
                    item={"id": "after-gap", "type": "agentMessage"},
                )
                yield _notification(
                    "item/agentMessage/delta", turn_id,
                    itemId="after-gap", delta="still streaming",
                )
                yield _notification(
                    "item/completed", turn_id,
                    item={
                        "id": "after-gap",
                        "type": "agentMessage",
                        "text": "still streaming",
                    },
                )
                yield _notification(
                    "turn/completed", turn_id,
                    turn={"id": turn_id, "status": "completed"},
                )

        ctx.sdk = OverflowSdk()
        machine.sessions[ctx.key] = ctx
        repairs = []

        async def failed_repair(_sid):
            repairs.append(_sid)
            raise RuntimeError("projection unavailable")

        machine._push_mirrored_history = failed_repair
        await machine._run_codex_spontaneous_turn(
            ctx, turn_id, announce_running=False)

        assert not [event for event in transport.sent
                    if isinstance(event, Error)]
        assert any(
            isinstance(event, Delta) and event.text == "still streaming"
            for event in transport.sent
        )
        terminal = [event for event in transport.sent
                    if isinstance(event, TurnEnd)]
        assert len(terminal) == 1
        assert terminal[0].turn_id == turn_id
        assert terminal[0].result.subtype == "success"
        assert terminal[0].result.is_error is False
        assert repairs == [ctx.session_id]

    asyncio.run(run())
