"""FastAPI relay: /api/login (password -> session token), /ws (wrapper + web
clients), /healthz, optional static hosting of the web client from the same
origin.

Auth: wrapper authenticates with a Bearer WRAPPER_TOKEN header; web clients
authenticate with a session token in ?token=, obtained by POSTing a password to
/api/login. The static CLIENT_TOKEN is no longer used by the web client (it
used to be baked into the JS bundle, which was a public exposure).

The relay never imports claude_agent_sdk and never touches the model API.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from cc_remote.config import RelayConfig, relay_config
from cc_remote.log import logger
from cc_remote.relay.auth import (
    authenticate, make_session_token, verify_password, verify_session_token,
)
from cc_remote.relay.pairing import RelayHub

log = logger("cc_remote.relay.server")

# Simple in-memory login rate limit: {ip: [timestamps]}. Good enough for a
# single-process relay; entries older than 60s are dropped.
_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW = 60.0
_LOGIN_MAX = 5


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    if len(attempts) >= _LOGIN_MAX:
        return True
    attempts.append(now)
    return False


def create_app(cfg: Optional[RelayConfig] = None) -> FastAPI:
    cfg = cfg or relay_config()
    app = FastAPI(title="cc-remote relay")
    hub = RelayHub(cfg)
    app.state.hub = hub

    @app.post("/api/login")
    async def login(req: Request) -> JSONResponse:
        ip = req.client.host if req.client else "?"
        if _rate_limited(ip):
            return JSONResponse({"error": "rate_limited"}, status_code=429)
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        password = body.get("password", "") if isinstance(body, dict) else ""
        if not verify_password(password, cfg.login_password):
            log.warning("login failed", ip=ip)
            return JSONResponse({"error": "invalid"}, status_code=401)
        token, exp = make_session_token(cfg.session_secret, cfg.session_ttl_seconds)
        log.info("login ok", ip=ip, exp=exp)
        return JSONResponse({"token": token, "exp": exp})

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        role = authenticate(websocket.headers.get("authorization", ""), cfg)  # wrapper Bearer
        if role is None:
            # web client: ?token= must be a valid session token (from /api/login)
            if verify_session_token(websocket.query_params.get("token", ""), cfg.session_secret):
                role = "client"
        if role is None:
            await websocket.close(code=1008, reason="unauthorized")
            return
        await websocket.accept()
        log.info("ws accepted", role=role)
        if role == "wrapper":
            await hub.serve_wrapper(websocket)
        else:
            await hub.serve_client(websocket)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "wrapper_connected": hub.wrapper_connected,
                "clients": hub.client_count}

    # Static web client. Mounted last so /api/login and /ws and /healthz win.
    if cfg.static_dir and os.path.isdir(cfg.static_dir):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="static")
        log.info("serving static web client", dir=cfg.static_dir)

    return app
