"""Zero-token tests for the official Codex persisted-history projection."""
from __future__ import annotations

import asyncio
import json

import pytest

import cc_remote.wrapper.codex_stream as codex_stream_module
from cc_remote.wrapper.codex_history import (
    CodexHistoryCursorError,
    CodexHistoryInvalidResponse,
    CodexHistoryUnsupported,
    CodexOfficialHistory,
)
from cc_remote.wrapper.codex_rpc import (
    CodexRpcRejected,
    CodexRpcResponseTooLarge,
)
from cc_remote.wrapper.codex_stream import (
    codex_history_image_views,
    codex_history_turn_user,
    codex_translate_history,
)
from cc_remote.protocol import UserMsg


_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


def _user(
    item_id: str,
    text: str,
    *,
    client_id: str | None = None,
) -> dict:
    return {
        "type": "userMessage",
        "id": item_id,
        "clientId": client_id,
        "content": [{"type": "text", "text": text}],
    }


def _agent(item_id: str, text: str, *, phase: str = "final") -> dict:
    return {
        "type": "agentMessage",
        "id": item_id,
        "text": text,
        "phase": phase,
    }


def _turn(
    turn_id: str,
    items: list[dict],
    *,
    status: str = "completed",
    items_view: str = "summary",
    started_at: int = 100,
    completed_at: int | None = 102,
    duration_ms: int | None = 2000,
    error: dict | None = None,
) -> dict:
    return {
        "id": turn_id,
        "status": status,
        "itemsView": items_view,
        "items": items,
        "startedAt": started_at,
        "completedAt": completed_at,
        "durationMs": duration_ms,
        "error": (
            error or {"message": "provider detail"}
            if status == "failed"
            else None
        ),
    }


def test_rollout_image_view_supplement_is_turn_bound_and_binary_free(
    monkeypatch, tmp_path,
):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {
            "timestamp": "2026-07-30T06:40:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-1"},
        },
        {
            "timestamp": "2026-07-30T06:40:01Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-1",
                "message": "inspect",
            },
        },
        {
            "timestamp": "2026-07-30T06:40:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc-image-1",
                "name": "view_image",
                "arguments": json.dumps({
                    "path": "/tmp/chart.png",
                    "detail": "original",
                }),
                "call_id": "call-image-1",
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "native-1",
                },
            },
        },
        {
            "timestamp": "2026-07-30T06:40:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-image-1",
                "output": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                    "detail": "original",
                }],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "native-1",
                },
            },
        },
        {
            "timestamp": "2026-07-30T06:40:04Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "turn_id": "native-1",
                "message": "now inspect the second image",
            },
        },
        {
            "timestamp": "2026-07-30T06:40:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc-image-steer",
                "name": "view_image",
                "arguments": '{"path":"/tmp/steered.png"}',
                "call_id": "call-image-steer",
            },
        },
        {
            "timestamp": "2026-07-30T06:40:06Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-image-steer",
                "output": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                }],
            },
        },
        {
            "timestamp": "2026-07-30T06:41:00Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-1"},
        },
        {
            "timestamp": "2026-07-30T06:42:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-2"},
        },
        {
            "timestamp": "2026-07-30T06:42:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "fc-image-2",
                "name": "view_image",
                "arguments": '{"path":"/tmp/other.png"}',
                "call_id": "call-image-2",
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codex_stream_module,
        "_reverse_jsonl_records",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("exact turn lookup must not walk every JSONL row")
        ),
    )

    views = codex_history_image_views(
        str(path), "native-1", segment_index=0)

    assert len(views) == 1
    view = views[0]
    assert view.call_id == "call-image-1"
    assert view.event.item_id == "fc-image-1"
    assert view.event.tool == "view_image"
    assert view.event.input is not None
    assert view.event.input["file_path"] == "/tmp/chart.png"
    assert view.event.input["history_image"]["image_id"].startswith("img-")
    assert view.media_type == "image/png"
    assert view.width == 1 and view.height == 1
    assert view.data is not None
    wire = view.event.model_dump_json()
    assert _PNG_1X1 not in wire
    assert "steered.png" not in wire
    assert "other.png" not in wire
    steered = codex_history_image_views(
        str(path), "native-1", segment_index=1)
    assert len(steered) == 1
    assert steered[0].event.input is not None
    assert steered[0].event.input["file_path"] == "/tmp/steered.png"
    assert "chart.png" not in steered[0].event.model_dump_json()
    assert codex_history_image_views(
        str(path), "native-1", segment_index=2) == ()
    translated, _model = codex_translate_history(str(path), 64 * 1024)
    translated_wire = "\n".join(
        event.model_dump_json() for event in translated)
    assert _PNG_1X1 not in translated_wire
    assert "图片已读取" in translated_wire


def test_summary_page_is_chronological_and_preserves_native_identity():
    calls: list[tuple[str, dict]] = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        return {
            "data": [
                _turn(
                    "native-new",
                    [_user("user-new", "new", client_id="client-new"),
                     _agent("answer-new", "new answer")],
                    status="interrupted",
                    started_at=200,
                    completed_at=203,
                    duration_ms=3000,
                ),
                _turn(
                    "native-old",
                    [_user("user-old", "old", client_id="client-old"),
                     _agent("answer-old", "old answer")],
                ),
            ],
            "nextCursor": "older-page",
            "backwardsCursor": "newer-page",
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-1", before=None, limit=2)

        assert calls == [("thread/turns/list", {
            "threadId": "thread-1",
            "cursor": None,
            "limit": 2,
            "sortDirection": "desc",
            "itemsView": "summary",
        })]
        assert [turn["id"] for turn in page.turns] == [
            "user-old", "user-new"]
        assert [turn["clientMsgId"] for turn in page.turns] == [
            "client-old", "client-new"]
        assert page.turns[0]["forkPointId"] == "native-old"
        assert page.turns[0]["durationMs"] == 2000
        assert page.turns[0]["ts"] == 100_000
        assert page.turns[0]["doneTs"] == 102_000
        assert page.turns[1]["interrupted"] is True
        assert page.oldest_id == "user-old"
        assert page.newest_id == "user-new"
        assert page.has_more is True
        assert page.turns[0]["blocks"][-1]["text"] == "old answer"
        assert page.turns[0]["detailEventCount"] >= 1

    asyncio.run(run())


def test_failed_network_summary_keeps_specific_safe_product_copy():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [_turn(
                "native-network",
                [_user("user-network", "continue"),
                 _agent("answer-network", "partial answer")],
                status="failed",
                error={
                    "message": "stream disconnected before completion: "
                               "error sending request for url "
                               "https://chatgpt.com/backend-api/codex/responses",
                    "codexErrorInfo": "other",
                },
            )],
            "nextCursor": None,
        }

    async def run():
        page = await CodexOfficialHistory(
            64 * 1024, rpc=rpc,
        ).summary_page("thread-network", before=None, limit=1)

        assert page.turns[0]["error"] == (
            "网络连接异常，请检查网络后重试。"
        )
        assert "chatgpt.com" not in page.turns[0]["error"]

    asyncio.run(run())


def test_summary_supports_assistant_only_and_multiple_steer_segments():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [
                _turn(
                    "native-multi",
                    [
                        _user("user-first", "first"),
                        _agent("answer-first", "first answer"),
                        _user("user-steer", "steer", client_id="client-steer"),
                        _agent("answer-last", "last answer"),
                    ],
                ),
                _turn(
                    "native-auto",
                    [_agent("auto-answer", "continued automatically")],
                ),
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-1", before=None, limit=2)

        assert [turn["id"] for turn in page.turns] == [
            "native-auto", "user-first", "user-steer"]
        auto, first, steer = page.turns
        assert auto["prompt"] == ""
        assert auto["forkPointId"] == "native-auto"
        assert first["done"] is True
        assert "forkPointId" not in first
        assert steer["forkPointId"] == "native-multi"
        assert steer["clientMsgId"] == "client-steer"

    asyncio.run(run())


def test_active_summary_hydrates_exact_full_turn_and_preserves_steers():
    calls = []
    summary = _turn(
        "native-active",
        [
            _user("user-first", "first"),
            _agent("answer-last", "latest only"),
        ],
        status="interrupted",
        completed_at=None,
        duration_ms=None,
    )
    full = _turn(
        "native-active",
        [
            _user("user-first", "first"),
            _agent("answer-first", "first answer", phase="commentary"),
            _user("user-steer", "continue", client_id="client-steer"),
            _agent("answer-after-steer", "still working", phase="commentary"),
        ],
        status="interrupted",
        items_view="full",
        completed_at=None,
        duration_ms=None,
    )

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        if len(calls) == 1:
            return {"data": [summary], "nextCursor": None}
        assert params["itemsView"] == "full"
        return {"data": [full], "nextCursor": None}

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )

        assert [turn["id"] for turn in page.turns] == [
            "user-first", "user-steer"]
        assert page.turns[0]["done"] is True
        assert page.turns[1]["done"] is False
        assert page.turns[1]["clientMsgId"] == "client-steer"
        assert page.turns[1]["forkPointId"] == "native-active"
        assert [params["itemsView"] for _method, params in calls] == [
            "summary", "full"]

    asyncio.run(run())


def test_active_full_shape_survives_completed_summary_collapse():
    responses = [
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"), _agent("last", "latest")],
                status="interrupted",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent("answer-first", "first answer"),
                    _user("user-steer", "continue"),
                    _agent("answer-final", "final answer"),
                ],
                status="interrupted",
                items_view="full",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        active = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )
        completed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
        )

        assert [turn["id"] for turn in active.turns] == [
            "user-first", "user-steer"]
        assert [turn["id"] for turn in completed.turns] == [
            "user-first", "user-steer"]
        assert completed.turns[-1]["done"] is True
        assert completed.turns[-1]["durationMs"] == 9000

    asyncio.run(run())


def test_completed_summary_refreshes_stale_active_full_before_projection():
    calls = []
    responses = [
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-working", "working", phase="commentary")],
                status="interrupted",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent(
                        "answer-working",
                        "working",
                        phase="commentary",
                    ),
                    _user("user-steer", "continue"),
                ],
                status="interrupted",
                items_view="full",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent(
                        "answer-working",
                        "working",
                        phase="commentary",
                    ),
                    _user("user-steer", "continue"),
                    _agent("answer-final", "final answer"),
                ],
                status="completed",
                items_view="full",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )
        completed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
        )

        assert [params["itemsView"] for _method, params in calls] == [
            "summary", "full", "summary", "full"]
        assert [turn["id"] for turn in completed.turns] == [
            "user-first", "user-steer"]
        assert completed.turns[-1]["blocks"][-1]["text"] == "final answer"
        assert completed.turns[-1]["done"] is True
        assert responses == []

    asyncio.run(run())


def test_terminal_summary_keeps_final_when_full_refresh_is_oversized():
    calls = []
    responses = [
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first")],
                status="interrupted",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [
                    _user("user-first", "first"),
                    _agent(
                        "answer-working",
                        "working",
                        phase="commentary",
                    ),
                    _user("user-steer", "continue"),
                ],
                status="interrupted",
                items_view="full",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
        CodexRpcResponseTooLarge("terminal full turn is oversized"),
        {
            "data": [_turn(
                "native-active",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        assert cwd is None
        if responses:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        raise CodexRpcResponseTooLarge("terminal full turn is oversized")

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            active_turn_ids={"native-active"},
        )
        completed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
        )
        refreshed = await history.summary_page(
            "thread-active",
            before=None,
            limit=1,
            hydrate_recent=1,
        )

        assert [params["itemsView"] for _method, params in calls] == [
            "summary", "full", "summary", "full", "summary"]
        assert [turn["id"] for turn in completed.turns] == [
            "user-first", "user-steer"]
        assert completed.turns[-1]["blocks"][-1]["text"] == "final answer"
        assert completed.turns[-1]["done"] is True
        assert refreshed.turns == completed.turns
        assert responses == []

    asyncio.run(run())


def test_newest_completed_turn_hydration_restores_cold_steer_segments():
    responses = [
        {
            "data": [_turn(
                "native-completed",
                [_user("user-first", "first"),
                 _agent("answer-final", "final answer")],
                status="completed",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-completed",
                [
                    _user("user-first", "first"),
                    _agent("answer-first", "first answer"),
                    _user("user-steer", "continue"),
                    _agent("answer-final", "final answer"),
                ],
                status="completed",
                items_view="full",
                completed_at=109,
                duration_ms=9000,
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-completed",
            before=None,
            limit=1,
            hydrate_recent=1,
        )

        assert [turn["id"] for turn in page.turns] == [
            "user-first", "user-steer"]
        assert [turn["prompt"] for turn in page.turns] == [
            "first", "continue"]
        assert page.turns[-1]["done"] is True
        assert page.turns[-1]["durationMs"] == 9000

    asyncio.run(run())


def test_recent_hydration_restores_just_finished_previous_turn():
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if len(calls) == 1:
            return {
                "data": [
                    _turn(
                        "native-new",
                        [_user("user-new", "new"), _agent("answer-new", "new")],
                    ),
                    _turn(
                        "native-steered",
                        [_user("user-first", "first"),
                         _agent("answer-final", "final")],
                    ),
                ],
                "nextCursor": None,
            }
        assert params["itemsView"] == "full"
        assert params["limit"] == 2
        return {
            "data": [
                _turn(
                    "native-new",
                    [_user("user-new", "new"), _agent("answer-new", "new")],
                    items_view="full",
                ),
                _turn(
                    "native-steered",
                    [
                        _user("user-first", "first"),
                        _agent("answer-first", "first answer"),
                        _user("user-steer", "continue"),
                        _agent("answer-final", "final"),
                    ],
                    items_view="full",
                ),
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-recent",
            before=None,
            limit=2,
            hydrate_recent=2,
        )

        assert [turn["id"] for turn in page.turns] == [
            "user-first", "user-steer", "user-new"]
        assert [turn["prompt"] for turn in page.turns] == [
            "first", "continue", "new"]

    asyncio.run(run())


def test_active_turn_missing_from_official_head_requests_rollout_fallback():
    async def rpc(_method, _params, cwd=None):
        return {
            "data": [_turn(
                "native-old",
                [_user("user-old", "old"), _agent("answer-old", "done")],
            )],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(CodexHistoryUnsupported):
            await history.summary_page(
                "thread-active",
                before=None,
                limit=1,
                active_turn_ids={"native-new"},
            )

    asyncio.run(run())


def test_summary_recovers_expired_local_image_from_rollout():
    recovered_calls = []

    async def rpc(_method, _params, cwd=None):
        return {
            "data": [_turn(
                "native-image",
                [{
                    **_user("user-image", "inspect"),
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {"type": "localImage", "path": "/expired/image.png"},
                    ],
                }, _agent("answer-image", "done")],
            )],
            "nextCursor": None,
        }

    async def recover(
        thread_id, native_turn_id, visible_turn_id, user_index,
    ):
        recovered_calls.append(
            (thread_id, native_turn_id, visible_turn_id, user_index))
        return UserMsg(
            msg_id=visible_turn_id,
            prompt="inspect",
            images=[{"media_type": "image/png", "data": _PNG_1X1}],
        )

    async def run():
        history = CodexOfficialHistory(
            64 * 1024, rpc=rpc, recover_user=recover)
        page = await history.summary_page(
            "thread-image", before=None, limit=1)
        assert recovered_calls == [
            ("thread-image", "native-image", "user-image", 0)]
        refs = page.turns[0].get("imageRefs")
        assert refs is not None and len(refs) == 1
        assert refs[0]["width"] == 1 and refs[0]["height"] == 1
        rows = history.summary_events("thread-image", "user-image")
        assert rows is not None
        assert rows[0]["images"][0]["data"] == _PNG_1X1

    asyncio.run(run())


def test_rollout_user_recovery_is_bound_to_the_native_turn(tmp_path):
    rollout = tmp_path / "rollout-images.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "native-old"},
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-image"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                }],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "inspect"},
        },
    ]))

    recovered = codex_history_turn_user(
        str(rollout), "native-image", "user-image")
    assert recovered is not None
    assert recovered.msg_id == "user-image"
    assert recovered.prompt == "inspect"
    assert recovered.images == [{
        "media_type": "image/png",
        "data": _PNG_1X1,
    }]
    assert codex_history_turn_user(
        str(rollout), "missing-turn", "user-image") is None


def test_rollout_user_recovery_selects_later_steer_images(tmp_path):
    rollout = tmp_path / "rollout-steer-images.jsonl"
    rollout.write_text("".join(json.dumps(row) + "\n" for row in [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "native-steer"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG_1X1}",
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_PNG_1X1}",
                    },
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "first"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{_PNG_1X1}",
                }],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "steer"},
        },
    ]))

    recovered = codex_history_turn_user(
        str(rollout), "native-steer", "user-steer", 1)
    assert recovered is not None
    assert recovered.msg_id == "user-steer"
    assert recovered.prompt == "steer"
    assert recovered.images is not None
    assert len(recovered.images) == 1


def test_summary_recovers_images_for_each_visible_steer_segment():
    recovered_calls = []

    def image_user(item_id: str, text: str) -> dict:
        return {
            **_user(item_id, text),
            "content": [
                {"type": "text", "text": text},
                {"type": "localImage", "path": f"/expired/{item_id}.png"},
            ],
        }

    async def rpc(_method, _params, cwd=None):
        return {
            "data": [_turn(
                "native-steer",
                [
                    image_user("user-first", "first"),
                    _agent("answer-first", "first answer"),
                    image_user("user-steer", "steer"),
                    _agent("answer-steer", "steer answer"),
                ],
            )],
            "nextCursor": None,
        }

    async def recover(
        thread_id, native_turn_id, visible_turn_id, user_index,
    ):
        recovered_calls.append((
            thread_id, native_turn_id, visible_turn_id, user_index))
        return UserMsg(
            msg_id=visible_turn_id,
            prompt="recovered",
            images=[{"media_type": "image/png", "data": _PNG_1X1}],
        )

    async def run():
        history = CodexOfficialHistory(
            64 * 1024, rpc=rpc, recover_user=recover)
        page = await history.summary_page(
            "thread-steer", before=None, limit=1)
        assert recovered_calls == [
            ("thread-steer", "native-steer", "user-first", 0),
            ("thread-steer", "native-steer", "user-steer", 1),
        ]
        assert [len(turn.get("imageRefs") or []) for turn in page.turns] == [
            1, 1]

    asyncio.run(run())


def test_in_progress_assistant_only_turn_keeps_native_id_when_completed():
    responses = [
        {
            "data": [_turn(
                "native-auto",
                [_agent("agent-auto", "continuing")],
                status="inProgress",
                completed_at=None,
                duration_ms=None,
            )],
            "nextCursor": None,
        },
        {
            "data": [_turn(
                "native-auto",
                [_agent("agent-auto", "done")],
            )],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        active = await history.summary_page(
            "thread-auto", before=None, limit=1)
        completed = await history.summary_page(
            "thread-auto", before=None, limit=1)
        assert active.turns[0]["id"] == "native-auto"
        assert active.turns[0]["done"] is False
        assert completed.turns[0]["id"] == "native-auto"
        assert completed.turns[0]["done"] is True

    asyncio.run(run())


def test_summary_rejects_cursor_without_a_visible_page_boundary():
    async def rpc(_method, _params, cwd=None):
        return {"data": [], "nextCursor": "older"}

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(CodexHistoryInvalidResponse):
            await history.summary_page(
                "thread-empty", before=None, limit=1)

    asyncio.run(run())


def test_summary_failed_status_is_not_silently_presented_as_success():
    async def rpc(_method, _params, cwd=None):
        assert cwd is None
        return {
            "data": [
                _turn(
                    "native-failed",
                    [_user("user-failed", "run"),
                     _agent("partial", "partial", phase="commentary")],
                    status="failed",
                ),
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        page = await history.summary_page(
            "thread-1", before=None, limit=1)
        turn = page.turns[0]
        assert turn["done"] is True
        assert turn["error"] == "该轮未正常结束"
        assert turn["forkPointId"] == "native-failed"

    asyncio.run(run())


def test_summary_cursor_is_session_bound_and_unknown_cursor_is_rejected():
    responses = [
        {
            "data": [_turn(
                "native-2", [_user("user-2", "two"), _agent("a-2", "2")])],
            "nextCursor": "opaque-older",
        },
        {
            "data": [_turn(
                "native-1", [_user("user-1", "one"), _agent("a-1", "1")])],
            "nextCursor": None,
        },
    ]
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page("thread-a", before=None, limit=1)
        older = await history.summary_page(
            "thread-a", before="user-2", limit=1)
        assert older.oldest_id == "user-1"
        assert calls[-1][1]["cursor"] == "opaque-older"

        with pytest.raises(CodexHistoryCursorError):
            await history.summary_page(
                "thread-b", before="user-2", limit=1)
        with pytest.raises(CodexHistoryCursorError):
            await history.summary_page(
                "thread-a", before="unknown", limit=1)

    asyncio.run(run())


def test_summary_rejects_dirty_or_duplicate_official_data():
    responses = [
        {"data": "not-a-list", "nextCursor": None},
        {
            "data": [
                _turn(
                    "native-1",
                    [_user("same-item", "one"), _agent("same-item", "answer")],
                ),
            ],
            "nextCursor": None,
        },
    ]

    async def rpc(_method, _params, cwd=None):
        return responses.pop(0)

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        with pytest.raises(CodexHistoryInvalidResponse):
            await history.summary_page(
                "thread-1", before=None, limit=1)
        with pytest.raises(CodexHistoryInvalidResponse):
            await history.summary_page(
                "thread-1", before=None, limit=1)

    asyncio.run(run())


def test_detail_prefers_items_list_and_preserves_rich_official_items():
    summary = {
        "data": [
            _turn(
                "native-1",
                [_user("user-1", "inspect"), _agent("answer-1", "done")],
            ),
        ],
        "nextCursor": None,
    }
    full_items = [
        _user("user-1", "inspect"),
        {
            "type": "reasoning",
            "id": "reason-1",
            "summary": ["checking"],
            "content": [],
        },
        {
            "type": "commandExecution",
            "id": "command-1",
            "command": "pwd",
            "commandActions": [],
            "cwd": "/repo",
            "status": "completed",
            "aggregatedOutput": "/repo\n",
            "exitCode": 0,
            "durationMs": 12,
        },
        {"type": "contextCompaction", "id": "compact-1"},
        {
            "type": "subAgentActivity",
            "id": "agent-1",
            "agentPath": "worker",
            "agentThreadId": "child-thread",
            "kind": "interacted",
        },
        _agent("answer-1", "done"),
    ]
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if method == "thread/turns/list":
            return summary
        assert method == "thread/items/list"
        return {
            "data": [
                {"turnId": "native-1", "item": item}
                for item in full_items
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        events = await history.turn_events("thread-1", "user-1")
        types = [event["type"] for event in events]
        assert types[0] == "user_msg"
        assert {"tool_use", "tool_result", "process", "turn_end"} <= set(types)
        assert any(
            event.get("item_id") == "reason-1"
            and event.get("kind") == "reasoning"
            for event in events
        )
        assert any(
            event.get("item_id") == "compact-1"
            and event.get("kind") == "compaction"
            for event in events
        )
        assert any(
            event.get("item_id") == "agent-1"
            and event.get("parent_id") == "child-thread"
            for event in events
        )
        assert calls[-1] == ("thread/items/list", {
            "threadId": "thread-1",
            "turnId": "native-1",
            "cursor": None,
            "limit": 1024,
            "sortDirection": "asc",
        })
        call_count = len(calls)
        assert await history.turn_events("thread-1", "user-1") == events
        assert len(calls) == call_count

    asyncio.run(run())


def test_detail_falls_back_from_items_list_to_exact_full_turn():
    summary = {
        "data": [
            _turn(
                "native-new",
                [_user("user-new", "new"), _agent("a-new", "new")],
            ),
            _turn(
                "native-target",
                [_user("user-target", "target"), _agent("a-target", "done")],
            ),
        ],
        "nextCursor": "older",
    }
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if len(calls) == 1:
            return summary
        if method == "thread/items/list":
            raise CodexRpcRejected(
                "codex app-server error -32601: not supported yet",
                code=-32601,
            )
        if params["itemsView"] == "notLoaded":
            assert params["cursor"] is None
            return {
                "data": [_turn(
                    "native-new", [], items_view="notLoaded")],
                "nextCursor": "after-new",
            }
        assert params["itemsView"] == "full"
        assert params["cursor"] == "after-new"
        return {
            "data": [_turn(
                "native-target",
                [_user("user-target", "target"), _agent("a-target", "done")],
                items_view="full",
            )],
            "nextCursor": "after-target",
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=2)
        events = await history.turn_events(
            "thread-1", "user-target")
        assert events[0]["msg_id"] == "user-target"
        assert events[-1]["turn_id"] == "native-target"
        assert history.turn_detail_source(
            "thread-1", "user-target") == "full"
        assert [method for method, _params in calls] == [
            "thread/turns/list",
            "thread/items/list",
            "thread/turns/list",
            "thread/turns/list",
        ]

    asyncio.run(run())


def test_detail_items_list_is_recorded_as_complete_official_source():
    async def rpc(method, params, cwd=None):
        if method == "thread/turns/list":
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "inspect"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        assert method == "thread/items/list"
        return {
            "data": [
                {"turnId": "native-1", "item": item}
                for item in [
                    _user("user-1", "inspect"),
                    {
                        "type": "commandExecution",
                        "id": "command-1",
                        "command": "pwd",
                        "commandActions": [],
                        "cwd": "/repo",
                        "status": "completed",
                        "aggregatedOutput": "/repo\n",
                        "exitCode": 0,
                        "durationMs": 12,
                    },
                    _agent("a-1", "done"),
                ]
            ],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page("thread-1", before=None, limit=1)
        await history.turn_events("thread-1", "user-1")
        assert history.turn_detail_source("thread-1", "user-1") == "items"
        history.invalidate_thread("thread-1")
        assert history.turn_detail_source("thread-1", "user-1") is None

    asyncio.run(run())


def test_detail_oversized_item_page_uses_exact_full_turn():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        if method == "thread/items/list":
            raise CodexRpcResponseTooLarge("oversized item page")
        assert params["itemsView"] == "full"
        return {
            "data": [_turn(
                "native-1",
                [_user("user-1", "one"), _agent("a-1", "done")],
                items_view="full",
            )],
            "nextCursor": None,
        }

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        events = await history.turn_events("thread-1", "user-1")
        assert events[-1]["turn_id"] == "native-1"

    asyncio.run(run())


def test_detail_oversized_full_turn_requests_rollout_fallback():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        raise CodexRpcResponseTooLarge(
            f"oversized {method} response")

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        with pytest.raises(CodexHistoryUnsupported):
            await history.turn_events("thread-1", "user-1")

    asyncio.run(run())


def test_detail_falls_back_to_rollout_only_when_both_official_apis_unsupported():
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, params))
        if len(calls) == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        raise CodexRpcRejected(
            "codex app-server error -32601: unsupported",
            code=-32601,
        )

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        with pytest.raises(CodexHistoryUnsupported):
            await history.turn_events("thread-1", "user-1")
        fallback = history.rollout_fallback("thread-1", "user-1")
        assert fallback.before is None
        assert fallback.limit == 1
        assert fallback.native_turn_id == "native-1"

    asyncio.run(run())


def test_detail_auth_rejection_is_not_downgraded_to_rollout():
    async def rpc(method, params, cwd=None):
        if method == "thread/turns/list":
            return {
                "data": [_turn(
                    "native-1",
                    [_user("user-1", "one"), _agent("a-1", "done")],
                )],
                "nextCursor": None,
            }
        raise CodexRpcRejected(
            "codex app-server error -32001: unauthorized",
            code=-32001,
        )

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        with pytest.raises(CodexRpcRejected) as rejected:
            await history.turn_events("thread-1", "user-1")
        assert rejected.value.code == -32001

    asyncio.run(run())


def test_multi_user_detail_selects_the_requested_full_segment_after_summary_collapse():
    calls = 0

    async def rpc(method, params, cwd=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": [_turn(
                    "native-1",
                    [
                        _user("user-1", "first"),
                        _agent("a-2", "second"),
                    ],
                )],
                "nextCursor": None,
            }
        if method == "thread/items/list":
            return {
                "data": [
                    {"turnId": "native-1", "item": _user("user-1", "first")},
                    {"turnId": "native-1", "item": _agent("a-1", "first")},
                    {"turnId": "native-1", "item": _user("user-2", "second")},
                    {"turnId": "native-1", "item": _agent("a-2", "second")},
                ],
                "nextCursor": None,
            }
        raise AssertionError("unexpected RPC")

    async def run():
        history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        await history.summary_page(
            "thread-1", before=None, limit=1)
        events = await history.turn_events("thread-1", "user-1")
        assert events[0]["msg_id"] == "user-1"
        assert any(
            event.get("message_id") == "a-1"
            for event in events
        )
        assert not any(
            event.get("message_id") == "a-2"
            for event in events
        )

    asyncio.run(run())
