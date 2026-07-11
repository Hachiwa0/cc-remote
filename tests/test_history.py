"""Zero-token unit tests for on-demand bulk history (GetHistory/History) and the
cursor-aware hello (fresh snapshots, delta replay on reconnect). No relay/wrapper/
cc/model — these exercise the wrapper handlers directly with a stub transport.

Run: ./.venv/bin/python -m pytest tests/test_history.py -q
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cc_remote.protocol import (
    serialize, deserialize,
    GetHistory, History, UserMsg, AssistantMsgStart, Delta, TurnEnd, TurnResult,
)
from cc_remote.wrapper import machine as mm
from cc_remote.wrapper.stream import translate_history
from tests.test_multisession import _mk_machine, _mk_ctx


def test_protocol_v4_get_history_and_history_roundtrip():
    gh = GetHistory(session_id="s1", client_id="c1", limit=50)
    assert deserialize(serialize(gh)) == gh
    h = History(session_id="s1", events=[{"type": "user_msg", "msg_id": "u1"}],
                has_more=True, oldest_id="u1", newest_id="u9",
                in_progress=True)
    got = deserialize(serialize(h))
    assert got.type == "history" and got.session_id == "s1" and got.has_more is True
    assert got.events[0]["type"] == "user_msg"
    assert got.in_progress is True


def test_hello_sends_only_snapshots_no_replay_flood():
    """The core fix: hello sends ONE snapshot per resident session and NO buffer
    replay — even though each session's buffer holds narrative events."""
    async def go():
        m, tr = _mk_machine()
        for key in ("s1", "s2"):
            ctx = _mk_ctx(key, key)
            for i in range(5):
                ev = UserMsg(msg_id=f"{key}-{i}", prompt="x")
                ev.seq = ctx.next_seq(); ev.sid = key
                ctx.buffer.append(ev)
            m.sessions[key] = ctx
        await m._handle_client_hello(SimpleNamespace(client_id="c1"))
        types = [msg.type for msg in tr.sent]
        assert types == ["snapshot", "snapshot"]          # one per session, nothing else
        assert "replay_start" not in types and "user_msg" not in types
        assert all(msg.to == "c1" for msg in tr.sent)     # routed to the requesting client
    asyncio.run(go())


def test_hello_with_cursor_replays_only_missing_tail():
    async def go():
        m, tr = _mk_machine()
        ctx = _mk_ctx("s1", "s1")
        for i in range(1, 4):
            ev = UserMsg(msg_id=f"m{i}", prompt="x")
            ev.seq = i
            ctx.seq = i
            ev.sid = "s1"
            ctx.buffer.append(ev)
        m.sessions["s1"] = ctx

        await m._handle_client_hello(SimpleNamespace(
            client_id="c1", cursors={"s1": 2},
            generations={"s1": m.instance_id}, last_seq=None))

        assert [msg.type for msg in tr.sent] == [
            "replay_start", "user_msg", "replay_end"]
        assert tr.sent[1].msg_id == "m3"
        assert all(msg.to == "c1" for msg in tr.sent)

    asyncio.run(go())


def test_fresh_hello_replays_only_current_inflight_turn_after_snapshot():
    async def go():
        m, tr = _mk_machine()
        ctx = _mk_ctx("s1", "s1")
        ctx.state = "running"
        for i, prompt in enumerate(("old", "current"), 1):
            ev = UserMsg(msg_id=f"m{i}", prompt=prompt)
            ev.seq = ctx.next_seq()
            ctx.buffer.append(ev)
            delta = Delta(message_id=f"a{i}", text=prompt)
            delta.seq = ctx.next_seq()
            ctx.buffer.append(delta)
        m.sessions["s1"] = ctx

        await m._handle_client_hello(SimpleNamespace(
            client_id="c1", cursors=None, generations=None, last_seq=None))

        assert [msg.type for msg in tr.sent] == [
            "snapshot", "replay_start", "user_msg", "delta", "replay_end"]
        assert tr.sent[2].prompt == "current"
        assert all(msg.to == "c1" for msg in tr.sent)

    asyncio.run(go())


def test_get_history_returns_one_bulk_frame(monkeypatch):
    canned = [
        UserMsg(msg_id="u1", prompt="hi"),
        AssistantMsgStart(message_id="a1"),
        Delta(message_id="a1", text="hello"),
        TurnEnd(result=TurnResult(subtype="success", duration_ms=0, is_error=False)),
    ]
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(mm, "translate_history", lambda msgs, mx, timestamps=None: [e.model_copy() for e in canned])
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: "claude-opus-4-8")

    async def go():
        m, tr = _mk_machine()
        ctx = _mk_ctx("sX", "sX")
        ctx.state = "running"
        m.sessions["sX"] = ctx
        await m._handle_get_history(SimpleNamespace(
            session_id="sX", client_id="c1", cwd="/tmp/x", type="get_history"))
        assert len(tr.sent) == 1                          # ONE bulk frame, not N tiny frames
        hist = tr.sent[0]
        assert hist.type == "history" and hist.session_id == "sX" and hist.to == "c1"
        assert hist.has_more is False
        assert hist.in_progress is True
        assert hist.oldest_id == "u1" and hist.newest_id == "u1"
        # Model prepended (restores the readout), then the 4 translated events
        assert hist.events[0]["type"] == "model"
        assert [e["type"] for e in hist.events[1:]] == [
            "user_msg", "assistant_msg_start", "delta", "turn_end"]
        # every event is stamped with the session id so the client routes them right
        assert all(e["sid"] == "sX" for e in hist.events)
    asyncio.run(go())


def test_oversized_single_turn_is_compacted_below_transport_cap(monkeypatch):
    canned = [
        UserMsg(msg_id="u1", prompt="hi"),
        AssistantMsgStart(message_id="a1"),
        Delta(message_id="a1", text="x" * 200_000),
        TurnEnd(result=TurnResult(
            subtype="success", duration_ms=1, is_error=False)),
    ]
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(
        mm, "translate_history",
        lambda msgs, mx, timestamps=None: [event.model_copy() for event in canned],
    )
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: None)

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.ws_max_size_bytes = 64 * 1024
        history = await machine._build_history("s1", limit=60)
        assert len(history.model_dump_json().encode()) < machine.cfg.ws_max_size_bytes
        assert any(row["type"] == "error" for row in history.events)

    asyncio.run(go())


def test_many_turn_history_shrinks_with_logarithmic_serializations(monkeypatch):
    canned = []
    for index in range(256):
        uid = f"u{index}"
        canned.extend([
            UserMsg(msg_id=uid, prompt="q"),
            Delta(message_id=f"a{index}", text="x" * 4096),
            TurnEnd(result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)),
        ])
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(
        mm, "translate_history",
        lambda msgs, mx, timestamps=None: [event.model_copy() for event in canned],
    )
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: None)

    calls = 0
    original = History.model_dump_json

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(History, "model_dump_json", counted)

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.ws_max_size_bytes = 64 * 1024
        history = await machine._build_history("s1")
        assert history.has_more is True
        assert history.newest_id == "u255"
        assert len(history.model_dump_json().encode()) < machine.cfg.ws_max_size_bytes

    asyncio.run(go())
    # 256 linear removals were the prior failure mode. Binary search plus final
    # assertions stays comfortably below this fixed ceiling.
    assert calls < 20


def test_oversized_transcript_is_rejected_before_full_parse(monkeypatch, tmp_path):
    source = tmp_path / "huge.jsonl"
    source.write_text("{}\n")
    parsed = False

    def should_not_parse(*args, **kwargs):
        nonlocal parsed
        parsed = True
        raise AssertionError("history parser should not run")

    monkeypatch.setattr(mm, "transcript_path", lambda sid: str(source))
    monkeypatch.setattr(mm.os.path, "getsize", lambda path: 100_000_000)
    monkeypatch.setattr(mm, "get_session_messages", should_not_parse)

    async def go():
        machine, _ = _mk_machine()
        machine.cfg.history_source_max_bytes = 64 * 1024 * 1024
        history = await machine._build_history("s1", limit=60)
        assert history.events[0]["type"] == "error"
        assert "HISTORY_SOURCE_MAX_BYTES" in history.events[0]["message"]

    asyncio.run(go())
    assert parsed is False


def test_get_history_survives_transcript_read_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no transcript")
    monkeypatch.setattr(mm, "get_session_messages", boom)

    async def go():
        m, tr = _mk_machine()
        await m._handle_get_history(SimpleNamespace(
            session_id="sX", client_id="c1", cwd=None, type="get_history"))
        # still replies with an (empty) History frame — never crashes the wrapper
        assert len(tr.sent) == 1 and tr.sent[0].type == "history"
        assert tr.sent[0].events == []
    asyncio.run(go())


def test_get_history_paginates_by_turn(monkeypatch):
    """5 turns u1..u5; verify newest-page + load-older paging by turn boundary."""
    def turn(uid):
        return [UserMsg(msg_id=uid, prompt="q"),
                TurnEnd(result=TurnResult(subtype="success", duration_ms=0, is_error=False))]
    canned = [ev for uid in ("u1", "u2", "u3", "u4", "u5") for ev in turn(uid)]
    monkeypatch.setattr(mm, "get_session_messages", lambda sid, directory=None: ["m"])
    monkeypatch.setattr(mm, "translate_history", lambda msgs, mx, timestamps=None: [e.model_copy() for e in canned])
    monkeypatch.setattr(mm, "last_assistant_model", lambda msgs: None)

    async def go():
        m, tr = _mk_machine()

        async def fetch(before, limit):
            await m._handle_get_history(SimpleNamespace(
                session_id="sX", client_id="c1", cwd="/tmp", before=before, limit=limit))
            h = tr.sent[-1]
            return h, [e["msg_id"] for e in h.events if e["type"] == "user_msg"]

        # newest 2 turns
        h, uids = await fetch(None, 2)
        assert uids == ["u4", "u5"] and h.has_more is True
        assert h.oldest_id == "u4" and h.newest_id == "u5" and h.before is None
        # older page before u4
        h, uids = await fetch("u4", 2)
        assert uids == ["u2", "u3"] and h.has_more is True and h.before == "u4"
        # oldest page — u1 only, no more
        h, uids = await fetch("u2", 2)
        assert uids == ["u1"] and h.has_more is False and h.oldest_id == "u1"
    asyncio.run(go())


def test_translate_history_stamps_real_timestamps():
    """UserMsg gets the ask-time; TurnEnd gets the turn's last message time
    (answer-done) — NOT 'now'. This fixes the 'every message shows now' clock bug."""
    from cc_remote.wrapper.stream import translate_history
    msgs = [
        SimpleNamespace(uuid="u1", type="user",
                        message={"role": "user", "content": "hi"}),
        SimpleNamespace(uuid="a1", type="assistant",
                        message={"role": "assistant", "content": [{"type": "text", "text": "hello"}]}),
    ]
    events = translate_history(msgs, 10000, timestamps={"u1": 1000.0, "a1": 1005.0})
    um = next(e for e in events if e.type == "user_msg")
    te = next(e for e in events if e.type == "turn_end")
    assert um.ts == 1000.0        # question time
    assert te.ts == 1005.0        # answer-done = last (assistant) message time
    # missing timestamps must not crash (falls back to the _Base default)
    assert any(e.type == "user_msg" for e in translate_history(msgs, 10000))


def test_legacy_history_repairs_missing_message_and_tool_ids_stably():
    messages = [
        SimpleNamespace(
            type="user", uuid="",
            message={"role": "user", "content": "hello"},
        ),
        SimpleNamespace(
            type="assistant", uuid="",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
                {"type": "text", "text": "done"},
            ]},
        ),
        SimpleNamespace(
            type="user", uuid="user-tool-result",
            message={"role": "user", "content": [
                {"type": "tool_result", "content": "ok"},
            ]},
        ),
    ]

    first = translate_history(messages, 10_000)
    second = translate_history(messages, 10_000)
    first_ids = [
        getattr(event, name)
        for event in first
        for name in ("msg_id", "message_id", "tool_use_id")
        if getattr(event, name, None)
    ]
    second_ids = [
        getattr(event, name)
        for event in second
        for name in ("msg_id", "message_id", "tool_use_id")
        if getattr(event, name, None)
    ]
    assert first_ids and first_ids == second_ids
    assert all(identifier and len(identifier) <= 128 for identifier in first_ids)
