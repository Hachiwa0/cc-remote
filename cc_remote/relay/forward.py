"""Per-client connection with a bounded send queue and load shedding.

A slow/mobile client must not block the wrapper. Each client has its own async
sender task draining a bounded queue. When the queue fills, intermediate
`delta` frames are shed (the oldest delta in the queue is dropped to make
room) while state/tool_use/tool_result/turn_end and control frames are always
kept — so a slow client sees every marker and the latest text, just not every
token.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from cc_remote.log import logger
from cc_remote.protocol import serialize

log = logger("cc_remote.relay.forward")

# Frame types we never shed when load-shedding (markers + control).
_KEEP_TYPES = frozenset({
    "state", "tool_use", "tool_result", "assistant_msg_start", "assistant_msg_end",
    "turn_end", "replay_start", "replay_end", "snapshot", "error",
    "wrapper_disconnected", "wrapper_reconnected", "pong",
})


class ClientConn:
    def __init__(self, ws, cap: int, client_id: str):
        self.ws = ws
        self.cap = cap
        self.client_id = client_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=cap)
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
        """Enqueue a frame to this client. Sheds an intermediate delta if full."""
        if self._closed:
            return
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            if self._shed_one_delta():
                try:
                    self.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    log.warning("client queue still full after shed, dropping frame",
                                client_id=self.client_id, type=msg.type)
            else:
                # nothing sheddable; if the new frame is a delta, drop it; else force-room
                if getattr(msg, "type", None) == "delta":
                    self._shed_count += 1
                else:
                    self._shed_one_delta(force=True)
                    try:
                        self.queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        log.warning("client queue full, dropping non-delta frame",
                                    client_id=self.client_id, type=msg.type)

    def _shed_one_delta(self, force: bool = False) -> bool:
        """Drop the oldest delta currently in the queue. Returns True if dropped."""
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
            try:
                self.queue.put_nowait(it)
            except asyncio.QueueFull:
                # requeue ran out of room; keep the rest discarded (they're deltas)
                break
        if dropped:
            log.debug("shed intermediate delta", client_id=self.client_id,
                      shed_total=self._shed_count)
        return dropped

    async def _run(self) -> None:
        try:
            while True:
                msg = await self.queue.get()
                await self.ws.send_text(serialize(msg))
        except Exception as e:
            log.debug("client sender ended", client_id=self.client_id, error=str(e))
