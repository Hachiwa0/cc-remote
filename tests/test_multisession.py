"""Zero-token unit tests for the multi-session wrapper logic.

No relay/wrapper/cc — these exercise the pure pieces directly:
- protocol: SessionRekey round-trips and is a control frame (not seq'd).
- ringbuffer: rebuild replay wraps the whole buffer in ReplayStart(rebuild=True).
- machine._emit_locked: routes by ctx.key (real sid once known, else temp key)
  so a pre-capture new session never leaks into the focused runtime.
- machine._capture_session_id: re-keys the pool, follows focus ONLY when the
  captured session was the focused one (the focus-steal fix), and emits
  SessionRekey (NOT SessionFocus).

Run: ./.venv/bin/python -m pytest tests/test_multisession.py -q
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    serialize, deserialize, is_downstream,
    SessionRekey, UserMsg, ReplayStart, ReplayEnd,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.wrapper.machine import WrapperMachine


class _StubTransport:
    """Captures everything the machine tries to send."""
    def __init__(self):
        self.sent: list = []
        self.on_connected = None

    async def send(self, msg):
        self.sent.append(msg)


def _mk_machine():
    cfg = WrapperConfig()
    cfg.state_dir = Path(tempfile.mkdtemp(prefix="cc-remote-test-"))  # don't touch real state
    tr = _StubTransport()
    return WrapperMachine(cfg, tr), tr


def _mk_ctx(key: str, session_id=None) -> SessionContext:
    # Built inside the running loop so emit_lock binds to the right loop.
    return SessionContext(
        session_id=session_id,
        sdk=object(),                       # unused in these tests
        buffer=RingBuffer(1000, 10_000_000),
        cwd="/tmp/cc-remote-test-cwd",
        key=key,
    )


# ---- protocol ----

def test_session_rekey_roundtrips_and_is_control_frame():
    m = SessionRekey(old_key="tmp-abc", session_id="real-123", cwd="/tmp/x")
    back = deserialize(serialize(m))
    assert back.type == "session_rekey"
    assert back.old_key == "tmp-abc"
    assert back.session_id == "real-123"
    assert back.cwd == "/tmp/x"
    # control frame → never assigned a seq / buffered
    assert is_downstream(m) is False


# ---- ringbuffer rebuild ----

def test_ringbuffer_rebuild_wraps_full_buffer():
    rb = RingBuffer(1000, 10_000_000)
    for i in range(1, 4):
        u = UserMsg(msg_id=f"m{i}", prompt="hi")
        u.seq = i
        rb.append(u)
    frames = rb.replay_from(0, cc_session_id="s", state="idle", rebuild=True)
    assert isinstance(frames[0], ReplayStart)
    assert frames[0].rebuild is True and frames[0].truncated is False
    assert isinstance(frames[-1], ReplayEnd)
    body = [f for f in frames if getattr(f, "type", None) == "user_msg"]
    assert [f.msg_id for f in body] == ["m1", "m2", "m3"]


def test_ringbuffer_rebuild_on_empty_buffer_still_brackets():
    rb = RingBuffer(1000, 10_000_000)
    frames = rb.replay_from(0, cc_session_id="s", state="idle", rebuild=True)
    assert isinstance(frames[0], ReplayStart) and frames[0].rebuild is True
    assert isinstance(frames[-1], ReplayEnd)
    assert len(frames) == 2


# ---- emit routing (sid = ctx.session_id or ctx.key) ----

def test_emit_routes_by_temp_key_before_capture_then_real_sid():
    async def run():
        m, tr = _mk_machine()
        ctx = _mk_ctx(key="tmp-xyz", session_id=None)
        await m._emit_locked(ctx, UserMsg(msg_id="m1", prompt="hi"))
        assert tr.sent[-1].sid == "tmp-xyz"     # routed by temp key, NOT None
        ctx.session_id = "real-1"
        await m._emit_locked(ctx, UserMsg(msg_id="m2", prompt="hi"))
        assert tr.sent[-1].sid == "real-1"      # routed by real sid once known
    asyncio.run(run())


# ---- focus-steal fix ----

def test_capture_follows_focus_when_captured_session_is_focused():
    async def run():
        m, tr = _mk_machine()
        ctx = _mk_ctx(key="tmp-1", session_id=None)
        m.sessions["tmp-1"] = ctx
        m.focused_sid = "tmp-1"                  # user is viewing this new session
        await m._capture_session_id(ctx, "real-1")
        assert "tmp-1" not in m.sessions and m.sessions["real-1"] is ctx
        assert ctx.key == "real-1" and ctx.session_id == "real-1"
        assert m.focused_sid == "real-1"         # focus followed the re-key
        rekeys = [s for s in tr.sent if getattr(s, "type", None) == "session_rekey"]
        assert rekeys and rekeys[-1].old_key == "tmp-1" and rekeys[-1].session_id == "real-1"
        # never a focus frame for a re-key
        assert not [s for s in tr.sent if getattr(s, "type", None) == "session_focus"]
    asyncio.run(run())


def test_capture_does_not_steal_focus_from_background_session():
    async def run():
        m, tr = _mk_machine()
        bg = _mk_ctx(key="tmp-bg", session_id=None)
        other = _mk_ctx(key="real-other", session_id="real-other")
        m.sessions["tmp-bg"] = bg
        m.sessions["real-other"] = other
        m.focused_sid = "real-other"             # user is viewing a DIFFERENT session
        await m._capture_session_id(bg, "real-bg")
        assert m.sessions["real-bg"] is bg and "tmp-bg" not in m.sessions
        assert bg.key == "real-bg"
        assert m.focused_sid == "real-other"     # focus NOT stolen by the background capture
        rekeys = [s for s in tr.sent if getattr(s, "type", None) == "session_rekey"]
        assert rekeys and rekeys[-1].old_key == "tmp-bg"
    asyncio.run(run())
