"""WebSocket client to the relay. Outbound-only (no inbound ports on the
company machine). Auto-reconnects with backoff; on each (re)connect it calls
`on_connected` so the wrapper can re-announce itself (hello).

Live sends are best-effort: while disconnected, `send` drops frames (the ring
buffer + client replay is the source of truth, so nothing is lost). The send
queue is drained on disconnect to avoid double-delivery on reconnect.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator, Awaitable, Callable, Optional

from websockets.asyncio.client import connect

from cc_remote.log import logger
from cc_remote.protocol import ProtocolError, deserialize, serialize

log = logger("cc_remote.wrapper.transport")


class WrapperTransport:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.on_connected: Optional[Callable[[], Awaitable[None]]] = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._send_q: asyncio.Queue = asyncio.Queue()
        self._connected = False
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        await self._inbox.put(None)  # break incoming()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def send(self, msg) -> None:
        if not self._connected:
            return  # dropped; ring buffer + client replay covers it
        await self._send_q.put(msg)

    async def incoming(self) -> AsyncIterator:
        while True:
            item = await self._inbox.get()
            if item is None:
                break
            yield item

    async def _run(self) -> None:
        backoff = 1.0
        # Corporate networks often block direct outbound 443 to overseas hosts
        # but allow it via an HTTP proxy. websockets doesn't auto-read env, so
        # pass proxy explicitly from HTTPS_PROXY / ALL_PROXY (None = direct).
        proxy = (
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
        )
        while not self._stop:
            try:
                headers = {"Authorization": f"Bearer {self.token}"}
                kw: dict = {"additional_headers": headers}
                if proxy:
                    kw["proxy"] = proxy
                async with connect(self.url, **kw) as ws:
                    log.info("connected to relay", url=self.url)
                    backoff = 1.0
                    self._connected = True
                    if self.on_connected:
                        try:
                            await self.on_connected()
                        except Exception:
                            log.exception("on_connected callback failed")
                    sender = asyncio.create_task(self._sender(ws))
                    receiver = asyncio.create_task(self._receiver(ws))
                    done, pending = await asyncio.wait(
                        {sender, receiver}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    self._connected = False
                    for t in pending:
                        t.cancel()
            except Exception as e:
                self._connected = False
                log.warning("relay connection lost, reconnecting", error=str(e), backoff=backoff)
            self._drain_send_queue()
            if not self._stop:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _drain_send_queue(self) -> None:
        dropped = 0
        while not self._send_q.empty():
            try:
                self._send_q.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            log.debug("drained send queue on disconnect", dropped=dropped)

    async def _sender(self, ws) -> None:
        while True:
            msg = await self._send_q.get()
            await ws.send(serialize(msg))

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                cmd = deserialize(raw)
            except ProtocolError as e:
                log.warning("dropping malformed frame from relay", error=str(e))
                continue
            await self._inbox.put(cmd)
