"""Verifies the browser WebSocket auth path: ?token= query param, NO header.

Browsers can't set Authorization on a WebSocket, so the web client puts the
token in the query string. This connects the same way and runs one streaming
query to confirm that path works end-to-end through the relay.
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


async def main():
    cid = uuid.uuid4().hex
    url = f"{URL}?token={TOKEN}"  # browser path: token in query, no header
    async with connect(url) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=10))
            if m.type == "snapshot":
                break
        await ws.send(serialize(Query(prompt="用一句话说你好", msg_id=uuid.uuid4().hex)))
        deltas = 0
        text = ""
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=60))
            if m.type == "delta":
                deltas += 1
                text += m.text
            if m.type == "turn_end":
                assert m.result.subtype == "success", f"expected success, got {m.result.subtype}"
                break
        assert deltas > 0, "no streaming deltas"
        print(f"WEB-AUTH (?token=) OK: {deltas} deltas, text={text!r}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nWEB-AUTH FAILED: {e}", file=sys.stderr)
        sys.exit(1)
