"""Per-client connection with an unbounded send queue + soft load shedding.

A slow/mobile client must not block the wrapper, and a replay burst (the whole
ring buffer, potentially thousands of frames) must NOT lose frames — shedding
markers (tool_use / turn_end / replay_*) mid-replay corrupts history. So the
queue is unbounded (put never blocks, never raises), and we only shed the
oldest *delta* once qsize exceeds a soft cap. Markers are always kept. The soft
cap defaults well above the ring-buffer size, so a normal replay (<= 2000
frames) never triggers shedding; the cap is just a memory backstop for a
runaway/silent client.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from cc_remote.log import logger
from cc_remote.protocol import serialize

log = logger("cc_remote.relay.forward")


class ClientConn:
    def __init__(self, ws, cap: int, client_id: str):
        self.ws = ws
        self.soft_cap = cap
        self.client_id = client_id
        self.queue: asyncio.Queue = asyncio.Queue()  # unbounded — replay must not drop
        self._sender: Optional[asyncio.Task] = None
        self._closed = False
        self._shed_count = 0

    def start(self) -> None:
        self._sender = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closed = True
        if self._sender:
            self._sender.cancel()
            try:
                await self._sender
            except (asyncio.CancelledError, Exception):
                pass

    async def send(self, msg) -> None:
        """Enqueue a frame. Never drops markers; only sheds the oldest delta
        once qsize is over the soft cap (memory backstop for slow clients)."""
        if self._closed:
            return
        self.queue.put_nowait(msg)  # unbounded — always succeeds
        if self.queue.qsize() > self.soft_cap:
            self._shed_oldest_delta()

    def _shed_oldest_delta(self) -> None:
        """Drop the single oldest delta currently in the queue (markers kept)."""
        items: list = []
        while not self.queue.empty():
            try:
                items.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        dropped = False
        for it in items:
            if not dropped and getattr(it, "type", None) == "delta":
                dropped = True
                self._shed_count += 1
                continue
            self.queue.put_nowait(it)
        if dropped:
            log.debug("shed oldest delta", client_id=self.client_id, shed_total=self._shed_count)

    async def _run(self) -> None:
        try:
            while True:
                msg = await self.queue.get()
                await self.ws.send_text(serialize(msg))
        except Exception as e:
            log.debug("client sender ended", client_id=self.client_id, error=str(e))
