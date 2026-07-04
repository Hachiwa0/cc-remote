"""History sync test: a new client (last_seq=null) must see the FULL
conversation that happened before it connected — including the user's prompts
(user_msg) and the assistant replies — not just a tail snapshot.

Flow: client A sends a query; client B then connects fresh and must replay
A's user_msg + assistant deltas + turn_end.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import cc_remote.config  # noqa: F401
from cc_remote.protocol import Hello, Query, deserialize, serialize
from websockets.asyncio.client import connect

URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765/ws")
TOKEN = os.environ.get("CLIENT_TOKEN", "change-me-client")


async def recv_until(ws, want, timeout=90):
    evs = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return evs, None
        m = deserialize(raw)
        evs.append(m)
        if m.type in want:
            return evs, m


async def client_collect(cid, prompt, want, results):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with connect(URL, additional_headers=headers) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        if prompt:
            # drain the initial (empty-buffer) snapshot/replay before sending
            await recv_until(ws, {"replay_end", "snapshot"}, 10)
            await ws.send(serialize(Query(prompt=prompt, msg_id=uuid.uuid4().hex)))
        evs, _ = await recv_until(ws, want, 90)
        results[cid] = evs


async def main():
    ra: dict = {}
    await client_collect("A", "只说：同步测试OK", {"turn_end"}, ra)
    a = ra["A"]
    a_user = [e for e in a if e.type == "user_msg"]
    a_text = "".join(e.text for e in a if e.type == "delta")
    print(f"A: user_msgs={len(a_user)} text={a_text!r}")
    assert a_user, "A should see its own broadcast user_msg"
    assert "同步测试OK" in a_text, f"A missing assistant text: {a_text!r}"

    rb: dict = {}
    await client_collect("B", None, {"replay_end"}, rb)
    b = rb["B"]
    b_replay = [e for e in b if e.type == "replay_start"]
    b_user = [e for e in b if e.type == "user_msg"]
    b_text = "".join(e.text for e in b if e.type == "delta")
    b_turn_end = [e for e in b if e.type == "turn_end"]
    print(f"B: replay_start={len(b_replay)} user_msgs={len(b_user)} "
          f"turn_end={len(b_turn_end)} text={b_text!r}")
    assert b_replay, "B (fresh client) should get replay_start with full history"
    assert b_user, "B should see A's user_msg (the prompt) in replay"
    assert b_user[0].prompt == "只说：同步测试OK", f"wrong prompt: {b_user[0].prompt!r}"
    assert b_turn_end, "B should see A's turn_end in replay"
    assert "同步测试OK" in b_text, f"B missing A's assistant text: {b_text!r}"
    print("HISTORY SYNC OK — new client sees full conversation incl. prompts")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nHISTORY SYNC FAILED: {e}", file=sys.stderr)
        sys.exit(1)
