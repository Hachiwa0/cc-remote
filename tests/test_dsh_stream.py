from __future__ import annotations

import pytest

from cc_remote.wrapper.dsh_stream import (
    DshEventError,
    DshStreamTranslator,
    decode_dsh_model,
    encode_dsh_model,
    parse_dsh_fork_point,
)


def event(seq: int, kind: str, data: dict, **extra) -> dict:
    return {
        "type": kind,
        "seq": seq,
        "time": 1_700_000_000_000 + seq,
        "data": data,
        **extra,
    }


def test_model_route_round_trip_handles_provider_and_model_punctuation():
    encoded = encode_dsh_model("gateway/local", "deepseek v4/flash")
    assert encoded == "dsh://gateway%2Flocal/deepseek%20v4%2Fflash"
    assert decode_dsh_model(encoded) == (
        "gateway/local", "deepseek v4/flash"
    )


def test_fork_point_leaves_room_for_dsh_before_seq_increment():
    assert parse_dsh_fork_point("dsh-seq-9007199254740990") == 9_007_199_254_740_990
    with pytest.raises(ValueError, match="invalid DSH fork point"):
        parse_dsh_fork_point("dsh-seq-9007199254740991")


def test_translates_direct_user_and_preserves_rpc_alias():
    translator = DshStreamTranslator()
    rows = translator.feed(event(3, "user/message", {
        "id": "native-message",
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
        "source": {"kind": "user", "rpcId": "remote-msg-1"},
    }, surfaceOp="append"))
    assert len(rows) == 1
    assert rows[0].type == "user_msg"
    assert rows[0].msg_id == "dsh-msg-3"
    assert rows[0].client_msg_id == "remote-msg-1"
    assert rows[0].prompt == "hello"


def test_claimed_next_step_user_message_is_a_steering_boundary():
    translator = DshStreamTranslator()
    translator.feed(event(1, "turn/start", {"turn": 7}))
    assert translator.feed(event(2, "agent/inbox/spliced", {
        "target": "next-step",
        "start": 0,
        "inserted": [{
            "id": "native-steer",
            "role": "user",
            "content": [{"type": "text", "text": "change direction"}],
            "source": {"kind": "user", "rpcId": "remote-steer-1"},
        }],
    })) == []
    assert translator.feed(event(3, "agent/inbox/spliced", {
        "target": "next-step",
        "start": 0,
        "removedCount": 1,
        "inserted": [],
    })) == []

    rows = translator.feed(event(5, "user/message", {
        "id": "native-steer",
        "role": "user",
        "content": [{"type": "text", "text": "change direction"}],
        "source": {"kind": "user", "rpcId": "remote-steer-1"},
    }, surfaceOp="append"))

    assert len(rows) == 1
    assert rows[0].type == "turn_steered"
    assert rows[0].msg_id == "dsh-msg-5"
    assert rows[0].client_msg_id == "remote-steer-1"
    assert rows[0].turn_id == "dsh-turn-7"
    assert rows[0].prompt == "change direction"
    assert translator.next_step_claimed == set()


def test_cancelled_next_step_message_is_not_classified_as_steering():
    translator = DshStreamTranslator()
    translator.feed(event(1, "agent/inbox/spliced", {
        "target": "next-step",
        "start": 0,
        "inserted": [{
            "id": "cancelled",
            "role": "user",
            "content": [{"type": "text", "text": "cancel me"}],
            "source": {"kind": "user"},
        }],
    }))
    translator.feed(event(2, "agent/inbox/spliced", {
        "target": "next-step",
        "start": 0,
        "removedCount": 1,
        "inserted": [],
        "outcome": "canceled",
    }))
    rows = translator.feed(event(3, "user/message", {
        "id": "cancelled",
        "role": "user",
        "content": [{"type": "text", "text": "external append"}],
        "source": {"kind": "user"},
    }, surfaceOp="append"))
    assert rows[0].type == "user_msg"


def test_replacement_surface_message_does_not_erase_human_history():
    translator = DshStreamTranslator(strict_history=True)
    assert translator.feed(event(9, "user/message", {
        "id": "compaction",
        "role": "user",
        "content": [{"type": "text", "text": "summary"}],
        "source": {"kind": "plugin", "plugin": "compaction"},
    }, surfaceOp={"op": "replace", "start": 1, "end": 8})) == []


def test_streamed_text_is_promoted_to_final_by_assembled_message():
    translator = DshStreamTranslator()
    translator.feed(event(1, "turn/start", {"turn": 0}))
    started = translator.feed(event(2, "assistant/chunk", {
        "turn": 0,
        "step": 0,
        "chunk": {"type": "block-start", "index": 0, "blockType": "text"},
    }))
    delta = translator.feed(event(3, "assistant/chunk", {
        "turn": 0,
        "step": 0,
        "chunk": {"type": "text-delta", "index": 0, "text": "done"},
    }))
    assembled = translator.feed(event(4, "assistant/message", {
        "turn": 0,
        "step": 0,
        "message": {
            "id": "assistant-1",
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "source": {"kind": "model", "provider": "deepseek", "model": "v4"},
        },
    }, surfaceOp="append"))
    assert [row.type for row in started + delta + assembled] == [
        "user_msg", "assistant_msg_start", "delta", "assistant_msg_end"
    ]
    assert started[0].msg_id == "dsh-auto-1"
    assert started[1].channel == "unknown"
    assert assembled[-1].channel == "final"
    assert (0, 0, 0) not in translator.blocks


def test_streamed_text_retains_only_a_digest_before_assembly():
    translator = DshStreamTranslator()
    translator.feed(event(1, "assistant/chunk", {
        "turn": 0,
        "step": 0,
        "chunk": {"type": "text-delta", "index": 0, "text": "a" * 4096},
    }))
    block = translator.blocks[(0, 0, 0)]
    assert block.text_chars == 4096
    assert not hasattr(block, "text")


def test_non_text_chunks_do_not_allocate_active_blocks():
    translator = DshStreamTranslator()
    assert translator.feed(event(1, "assistant/chunk", {
        "turn": 0,
        "step": 0,
        "chunk": {"type": "block-start", "index": 9, "blockType": "tool-call"},
    })) == []
    assert translator.feed(event(2, "assistant/chunk", {
        "turn": 0,
        "step": 0,
        "chunk": {
            "type": "block-end",
            "index": 9,
            "block": {"type": "tool-call", "id": "call"},
        },
    })) == []
    assert translator.blocks == {}


def test_text_before_tool_call_becomes_commentary():
    translator = DshStreamTranslator()
    rows = translator.feed(event(4, "assistant/message", {
        "turn": 2,
        "step": 1,
        "message": {
            "id": "assistant-1",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will inspect it."},
                {"type": "tool-call", "id": "call-1", "name": "read", "arguments": "{}"},
            ],
            "source": {"kind": "model", "provider": "deepseek", "model": "v4"},
        },
    }, surfaceOp="append"))
    assert [row.type for row in rows] == [
        "assistant_msg_start", "delta", "assistant_msg_end"
    ]
    assert all(row.channel == "commentary" for row in rows)


def test_tool_call_and_result_use_stable_sanitized_identity():
    translator = DshStreamTranslator()
    call = translator.feed(event(5, "tool/call", {
        "turn": 2,
        "step": 1,
        "callId": "provider id with spaces",
        "name": "shell",
        "arguments": '{"cmd":"pwd"}',
    }))[0]
    result = translator.feed(event(6, "tool/result", {
        "turn": 2,
        "step": 1,
        "message": {
            "id": "result-1",
            "role": "user",
            "source": {"kind": "tool", "callId": "provider id with spaces"},
            "content": [{
                "type": "tool-result",
                "toolCallId": "provider id with spaces",
                "content": [{"type": "text", "text": "/tmp"}],
                "isError": False,
            }],
        },
    }, surfaceOp="append"))[0]
    assert call.tool_use_id == result.tool_use_id
    assert call.input == {"cmd": "pwd"}
    assert result.content == "/tmp"


def test_turn_end_is_an_exact_dsh_fork_point():
    translator = DshStreamTranslator()
    translator.feed(event(10, "turn/start", {"turn": 4}))
    rows = translator.feed(event(20, "turn/end", {
        "turn": 4,
        "reason": {"kind": "completed"},
    }))
    assert rows[0].msg_id == "dsh-auto-10"
    terminal = rows[-1]
    assert terminal.turn_id == "dsh-seq-20"
    assert terminal.presentation_id == "dsh-auto-10"
    assert parse_dsh_fork_point(terminal.turn_id) == 20
    assert terminal.result.is_error is False


def test_turn_end_targets_the_exact_visible_user_row():
    translator = DshStreamTranslator()
    translator.feed(event(10, "turn/start", {"turn": 4}))
    translator.feed(event(11, "user/message", {
        "source": {"kind": "user", "rpcId": "remote-message"},
        "content": [{"type": "text", "text": "hello"}],
    }, surfaceOp="append"))

    rows = translator.feed(event(20, "turn/end", {
        "turn": 4,
        "reason": {"kind": "completed"},
    }))

    assert [row.type for row in rows] == ["turn_end"]
    assert rows[0].turn_id == "dsh-seq-20"
    assert rows[0].presentation_id == "dsh-msg-11"


def test_user_append_before_turn_start_keeps_its_terminal_owner():
    translator = DshStreamTranslator()
    user = translator.feed(event(10, "user/message", {
        "source": {"kind": "user", "rpcId": "remote-message"},
        "content": [{"type": "text", "text": "hello"}],
    }, surfaceOp="append"))
    translator.feed(event(11, "turn/start", {"turn": 4}))
    terminal = translator.feed(event(20, "turn/end", {
        "turn": 4,
        "reason": {"kind": "completed"},
    }))

    assert user[0].msg_id == "dsh-msg-10"
    assert [row.type for row in terminal] == ["turn_end"]
    assert terminal[0].presentation_id == "dsh-msg-10"


@pytest.mark.parametrize("event_type", [
    "agent-preset/selected",
    "approval/policy",
    "permission/preset",
    "plan/mode",
    "sandbox/mode",
])
def test_session_configuration_does_not_create_process_rows(event_type):
    translator = DshStreamTranslator()

    assert translator.feed(event(2, event_type, {"value": "configured"})) == []


def test_standalone_command_is_a_complete_nonforkable_turn():
    translator = DshStreamTranslator()
    started = translator.feed(event(30, "command/run", {
        "commandId": "command-1",
        "name": "compact",
        "args": " now",
        "source": {"kind": "user"},
    }), command_client_id="remote-command")
    ended = translator.feed(event(31, "command/done", {
        "commandId": "command-1",
        "kind": "success",
        "text": "Compacted",
    }), command_client_id="remote-command")

    assert [row.type for row in started] == ["user_msg", "process"]
    assert started[0].msg_id == "dsh-msg-30"
    assert started[0].client_msg_id == "remote-command"
    assert started[0].prompt == "/compact now"
    assert started[1].phase == "start"
    assert [row.type for row in ended] == ["process", "turn_end"]
    assert ended[0].item_id == started[1].item_id
    assert ended[0].output == "Compacted"
    assert ended[1].turn_id is None
    assert ended[1].presentation_id == "dsh-msg-30"
    assert ended[1].result.subtype == "command_success"


def test_command_inside_a_model_turn_does_not_close_that_turn():
    translator = DshStreamTranslator()
    translator.feed(event(1, "turn/start", {"turn": 4}))
    translator.feed(event(2, "user/message", {
        "source": {"kind": "user"},
        "content": [{"type": "text", "text": "keep working"}],
    }, surfaceOp="append"))

    started = translator.feed(event(3, "command/run", {
        "commandId": "command-during-turn",
        "name": "feedback",
        "args": " note",
        "source": {"kind": "user"},
    }))
    ended = translator.feed(event(4, "command/done", {
        "commandId": "command-during-turn",
        "kind": "success",
    }))

    assert [row.type for row in started] == ["process"]
    assert [row.type for row in ended] == ["process"]
    assert translator.active_turn == 4


def test_strict_history_refuses_unknown_required_event():
    translator = DshStreamTranslator(strict_history=True)
    with pytest.raises(DshEventError, match="unsupported required"):
        translator.feed(event(2, "plugin/semantic-boundary", {"x": 1}))
    rows = translator.feed(event(
        3, "plugin/telemetry", {"x": 1}, ignorable=True
    ))
    assert len(rows) == 1
    assert rows[0].title == "plugin/telemetry"
    assert rows[0].summary == "DSH 插件事件（可安全忽略）"
    assert rows[0].detail is None


def test_live_projection_also_refuses_unknown_required_event():
    translator = DshStreamTranslator()
    with pytest.raises(DshEventError, match="required DSH live event"):
        translator.feed(event(2, "plugin/semantic-boundary", {"secret": "x"}))


def test_permission_command_is_a_control_projection_not_a_conversation_turn():
    translator = DshStreamTranslator()
    assert translator.feed(event(1, "command/run", {
        "commandId": "permission-command",
        "name": "permission",
        "args": " workspace-write",
        "source": {"kind": "user"},
    })) == []
    assert translator.feed(event(2, "command/done", {
        "commandId": "permission-command",
        "kind": "success",
        "sourceEventSeq": 1,
    })) == []


@pytest.mark.parametrize("event_type", [
    "hook/invoked",
    "hook/result",
    "tool-workflow/agent-start",
    "goal/change",
])
def test_known_plugin_lifecycle_never_proxies_opaque_payload(event_type):
    translator = DshStreamTranslator()
    rows = translator.feed(event(2, event_type, {
        "name": "display-safe-looking",
        "credential": "must-stay-local",
        "nested": {"token": "also-local"},
    }))

    assert len(rows) == 1
    assert rows[0].title == event_type
    assert rows[0].detail is None
    assert rows[0].summary is None


@pytest.mark.parametrize("timestamp", [True, False])
def test_dsh_event_timestamp_rejects_booleans(timestamp):
    translator = DshStreamTranslator()
    with pytest.raises(DshEventError, match="timestamp"):
        translator.feed({
            "type": "turn/start",
            "seq": 1,
            "time": timestamp,
            "data": {"turn": 0},
        })
