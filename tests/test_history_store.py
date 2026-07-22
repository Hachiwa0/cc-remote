from __future__ import annotations

import os

from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    MaterializedHistoryPage,
    materialize_history_turns,
)


def _page(label: str, *, more: bool = False) -> MaterializedHistoryPage:
    return MaterializedHistoryPage(
        events=({"type": "user_msg", "msg_id": label, "prompt": label},),
        has_more=more,
        oldest_id=label,
        newest_id=label,
    )


def test_history_index_roundtrip_is_bound_to_exact_source_snapshot(tmp_path):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text('{"type":"first"}\n')
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")

    assert store.get_page(
        "session-1", "codex", source, before=None, limit=4) is None
    assert store.put_page(
        "session-1", "codex", source, before=None, limit=4,
        page=_page("first", more=True),
    ) is True
    assert store.get_page(
        "session-1", "codex", source, before=None, limit=4,
    ) == _page("first", more=True)

    # An append changes the source identity; stale narrative is never returned.
    with source_path.open("a") as stream:
        stream.write('{"type":"second"}\n')
    changed = HistorySourceFingerprint.capture(source_path)
    assert changed.token != source.token
    assert store.get_page(
        "session-1", "codex", changed, before=None, limit=4) is None


def test_turn_detail_survives_append_but_not_destructive_invalidation(tmp_path):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text('{"type":"first"}\n')
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    events = (
        {"type": "user_msg", "msg_id": "message-1", "prompt": "inspect"},
        {"type": "tool_use", "tool_use_id": "tool-1", "tool": "Read",
         "input": {"file_path": "/tmp/example"}},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok",
         "is_error": False},
        {"type": "turn_end", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    )
    page = MaterializedHistoryPage(
        events=events, has_more=False,
        oldest_id="message-1", newest_id="message-1",
        turns=materialize_history_turns(events),
    )
    assert store.put_page(
        "session-1", "codex", source, before=None, limit=4, page=page)
    assert store.get_turn_detail(
        "session-1", "codex", source, "message-1") == events

    # A later append changes the page fingerprint but cannot change an already
    # completed turn, so detail painted from the previous snapshot remains valid.
    with source_path.open("a") as stream:
        stream.write('{"type":"second"}\n')
    changed = HistorySourceFingerprint.capture(source_path)
    assert store.get_turn_detail(
        "session-1", "codex", changed, "message-1") == events

    store.invalidate_session("session-1")
    assert store.get_turn_detail(
        "session-1", "codex", changed, "message-1") is None


def test_history_index_separates_cursor_limit_engine_and_session(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    store.put_page(
        "session-1", "claude", source, before="turn-5", limit=12,
        page=_page("older"),
    )

    assert store.get_page(
        "session-1", "claude", source, before="turn-5", limit=12,
    ) == _page("older")
    assert store.get_page(
        "session-1", "claude", source, before=None, limit=12) is None
    assert store.get_page(
        "session-1", "claude", source, before="turn-5", limit=4) is None
    assert store.get_page(
        "session-1", "codex", source, before="turn-5", limit=12) is None
    assert store.get_page(
        "session-2", "claude", source, before="turn-5", limit=12) is None


def test_history_index_is_bounded_and_invalidatable(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(
        tmp_path / "state", max_entries=2, max_bytes=16 * 1024)

    for index in range(3):
        store.put_page(
            f"session-{index}", "claude", source,
            before=None, limit=4, page=_page(f"page-{index}"),
        )
    hits = [
        store.get_page(
            f"session-{index}", "claude", source, before=None, limit=4)
        for index in range(3)
    ]
    assert sum(page is not None for page in hits) == 2

    store.invalidate_session("session-2")
    assert store.get_page(
        "session-2", "claude", source, before=None, limit=4) is None
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


def test_history_index_rejects_one_page_larger_than_total_budget(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state", max_bytes=1024)
    page = MaterializedHistoryPage(
        events=({"type": "delta", "text": "x" * 2048},),
        has_more=False,
        oldest_id="turn-1",
        newest_id="turn-1",
    )

    assert store.put_page(
        "session-1", "claude", source, before=None, limit=4, page=page,
    ) is False
    assert store.get_page(
        "session-1", "claude", source, before=None, limit=4) is None


def test_materialized_turn_keeps_final_answer_and_defers_process_detail():
    events = [
        {"type": "model", "model": "gpt-test"},
        {"type": "user_msg", "msg_id": "message-1", "prompt": "inspect",
         "ts": 10.0},
        {"type": "assistant_msg_start", "message_id": "commentary-1",
         "channel": "commentary", "ts": 11.0},
        {"type": "delta", "message_id": "commentary-1",
         "channel": "commentary", "text": "working"},
        {"type": "tool_use", "message_id": "tool-message",
         "tool_use_id": "tool-1", "tool": "exec_command", "input": {}},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "x" * 1000,
         "is_error": False},
        {"type": "assistant_msg_start", "message_id": "final-1",
         "channel": "final"},
        {"type": "delta", "message_id": "final-1", "channel": "final",
         "text": "done"},
        {"type": "assistant_msg_end", "message_id": "final-1",
         "channel": "final"},
        {"type": "turn_end", "turn_id": "turn-1", "ts": 12.0,
         "result": {"subtype": "success", "duration_ms": 2000,
                    "is_error": False}},
    ]

    turns = materialize_history_turns(events)

    assert turns == ({
        "id": "message-1",
        "prompt": "inspect",
        "blocks": [{
            "kind": "text", "message_id": "final-1", "text": "done",
            "done": True, "channel": "final",
        }],
        "done": True,
        "detailEventCount": 2,
        "detailLoaded": False,
        "forkPointId": "turn-1",
        "ts": 10_000,
        "doneTs": 12_000,
        "durationMs": 2000,
    },)


def test_materialized_turn_bounds_initial_final_text_and_advertises_detail():
    huge = "x" * (300 * 1024)
    turns = materialize_history_turns([
        {"type": "user_msg", "msg_id": "message-1", "prompt": "long"},
        {"type": "assistant_msg_start", "message_id": "final-1",
         "channel": "final"},
        {"type": "delta", "message_id": "final-1", "channel": "final",
         "text": huge},
        {"type": "turn_end", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    ])

    assert len(turns[0]["blocks"][0]["text"]) <= 256 * 1024
    assert "完整内容请展开" in turns[0]["blocks"][0]["text"]
    assert turns[0]["detailEventCount"] == 1


def test_materialized_turn_defers_images_and_bounds_large_prompt():
    turns = materialize_history_turns([
        {"type": "user_msg", "msg_id": "message-1",
         "prompt": "p" * (160 * 1024),
         "images": [{"media_type": "image/png", "data": "x" * 500_000}]},
        {"type": "turn_end", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    ])

    assert len(turns[0]["prompt"]) <= 128 * 1024
    assert "完整问题请展开" in turns[0]["prompt"]
    assert "images" not in turns[0]
    assert turns[0]["detailEventCount"] == 2
