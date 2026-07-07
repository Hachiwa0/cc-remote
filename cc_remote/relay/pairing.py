"""Relay hub: pairs one wrapper with N clients, routes per-client replay, fans
out live events.

- Single wrapper slot. The first wrapper to authenticate occupies it; a second
  is rejected with `wrapper_already_connected`.
- Clients register by `client_id` (from their hello). A reconnecting phone
  reuses its client_id, replacing any stale connection.
- Wrapper frames with `to=<client_id>` are routed to that client only (per-
  client replay); frames without `to` are broadcast to every client.
- wrapper hello announces (re)connection -> broadcast `wrapper_reconnected` so
  clients re-hello and replay from their last_seq.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from cc_remote.config import RelayConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    Error, ProtocolError, WrapperDisconnected, WrapperReconnected,
    deserialize, serialize,
    ERR_WRAPPER_OFFLINE, ERR_WRAPPER_ALREADY_CONNECTED, ERR_PROTOCOL,
)
from cc_remote.relay.forward import ClientConn

log = logger("cc_remote.relay")


class RelayHub:
    def __init__(self, cfg: RelayConfig):
        self.cfg = cfg
        self._wrapper_ws: Optional[WebSocket] = None
        self._clients: dict[str, ClientConn] = {}
        self._lock = asyncio.Lock()

    @property
    def wrapper_connected(self) -> bool:
        return self._wrapper_ws is not None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ---- wrapper side ----

    async def serve_wrapper(self, ws: WebSocket) -> None:
        async with self._lock:
            if self._wrapper_ws is not None:
                try:
                    await ws.send_text(serialize(Error(
                        code=ERR_WRAPPER_ALREADY_CONNECTED,
                        message="a wrapper is already connected",
                    )))
                    await ws.close(code=1008)
                except Exception:
                    pass
                return
            self._wrapper_ws = ws
        log.info("wrapper connected")
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = deserialize(raw)
                except ProtocolError as e:
                    log.warning("bad frame from wrapper", error=str(e))
                    continue
                await self._on_wrapper_msg(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("wrapper loop error")
        finally:
            await self._wrapper_gone()

    async def _on_wrapper_msg(self, msg) -> None:
        if msg.type == "hello" and getattr(msg, "role", None) == "wrapper":
            log.info("wrapper announced", cc_session_id=msg.cc_session_id,
                     state=msg.state, head=msg.buffer_head_seq, tail=msg.buffer_tail_seq)
            await self._broadcast(WrapperReconnected(
                cc_session_id=msg.cc_session_id, state=msg.state or "idle"))
            return
        to = getattr(msg, "to", None)
        if to:
            async with self._lock:
                conn = self._clients.get(to)
            if conn is not None:
                await conn.send(msg)
            else:
                log.debug("routed frame for unknown client, dropping", to=to, type=msg.type)
        else:
            await self._broadcast(msg)

    async def _wrapper_gone(self) -> None:
        async with self._lock:
            self._wrapper_ws = None
        log.warning("wrapper disconnected")
        await self._broadcast(WrapperDisconnected())

    # ---- client side ----

    async def serve_client(self, ws: WebSocket) -> None:
        conn: Optional[ClientConn] = None
        client_id: Optional[str] = None
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = deserialize(raw)
                except ProtocolError as e:
                    log.warning("bad frame from client", error=str(e))
                    continue

                if client_id is None:
                    if msg.type != "hello" or getattr(msg, "role", None) != "client":
                        await ws.send_text(serialize(Error(
                            code=ERR_PROTOCOL,
                            message="first frame must be a client hello",
                        )))
                        break
                    client_id = msg.client_id or uuid.uuid4().hex
                    conn = ClientConn(ws, self.cfg.client_queue_cap, client_id)
                    conn.start()
                    async with self._lock:
                        self._clients[client_id] = conn  # replaces stale conn on reconnect
                    log.info("client registered", client_id=client_id,
                             total=len(self._clients))

                # forward every client frame (including hello) to the wrapper
                w = self._wrapper_ws
                if w is None:
                    if conn is not None:
                        await conn.send(Error(code=ERR_WRAPPER_OFFLINE,
                                              message="wrapper is not connected"))
                else:
                    try:
                        await w.send_text(serialize(msg))
                    except Exception as e:
                        log.warning("forward to wrapper failed", error=str(e))
                        if conn is not None:
                            await conn.send(Error(code=ERR_WRAPPER_OFFLINE,
                                                  message="wrapper link broken"))
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("client loop error")
        finally:
            if client_id is not None and conn is not None:
                async with self._lock:
                    if self._clients.get(client_id) is conn:
                        del self._clients[client_id]
                await conn.stop()
            log.info("client removed", client_id=client_id, remaining=len(self._clients))

    # ---- broadcast ----

    async def _broadcast(self, msg) -> None:
        async with self._lock:
            conns = list(self._clients.values())
        dead: list[ClientConn] = []
        for c in conns:
            try:
                await c.send(msg)
            except Exception:
                dead.append(c)
        for c in dead:
            async with self._lock:
                if self._clients.get(c.client_id) is c:
                    del self._clients[c.client_id]
            await c.stop()
