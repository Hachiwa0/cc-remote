"""FastAPI relay: /api/login (password -> session cookie), /ws (wrapper + web
clients), /healthz, optional static hosting of the web client from the same
origin.

Auth: wrapper authenticates with a Bearer WRAPPER_TOKEN header; web clients
authenticate with a Secure HttpOnly session cookie obtained from /api/login.
Cookie-authenticated WebSockets must also match PUBLIC_ORIGIN when configured.

The relay never imports claude_agent_sdk and never touches the model API.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from cc_remote.config import (
    RelayConfig, relay_config, valid_machine_id, validate_relay_config,
)
from cc_remote.log import logger
from cc_remote.relay.auth import (
    SESSION_COOKIE_NAME, SessionClaims, authenticate, authenticate_login,
    make_session_token, session_token_claims, wrapper_machine_scope,
)
from cc_remote.relay.pairing import RelayHub
from cc_remote.relay.push import (
    PushDispatcher, PushOutcome, PushSubscription, PushSubscriptionStore,
)

log = logger("cc_remote.relay.server")

_LOGIN_WINDOW = 60.0
_LOGIN_MAX = 5
_LOGIN_MAX_IPS = 4096
_LOGIN_MAX_TOTAL_ATTEMPTS = 16384


def _turn_push_outcome(result: object | None) -> PushOutcome:
    subtype = str(getattr(result, "subtype", "") or "").lower()
    if subtype in {
        "error_during_execution", "interrupted", "cancelled", "canceled",
    }:
        return "interrupted"
    if bool(getattr(result, "is_error", False)):
        return "failed"
    return "success"


_LOGIN_CLEANUP_INTERVAL = 10.0
SESSION_EXPIRED_CLOSE_CODE = 1008
SESSION_EXPIRED_CLOSE_REASON = "session expired"
SESSION_REVOKED_CLOSE_CODE = 1008
SESSION_REVOKED_CLOSE_REASON = "session revoked"
_PUSH_BODY_MAX_BYTES = 16 * 1024
_PUSH_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,1024}")


class LoginRateLimiter:
    """Bounded per-IP login limiter with global stale-entry cleanup."""

    def __init__(
        self,
        *,
        window: float = _LOGIN_WINDOW,
        max_per_ip: int = _LOGIN_MAX,
        max_ips: int = _LOGIN_MAX_IPS,
        max_total_attempts: int = _LOGIN_MAX_TOTAL_ATTEMPTS,
        cleanup_interval: float = _LOGIN_CLEANUP_INTERVAL,
    ) -> None:
        self.window = window
        self.max_per_ip = max_per_ip
        self.max_ips = max_ips
        self.max_total_attempts = max_total_attempts
        self.cleanup_interval = cleanup_interval
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._total_attempts = 0
        self._last_cleanup = 0.0

    @property
    def key_count(self) -> int:
        return len(self._attempts)

    @property
    def total_attempts(self) -> int:
        return self._total_attempts

    def reset(self) -> None:
        self._attempts.clear()
        self._total_attempts = 0
        self._last_cleanup = 0.0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window
        total = 0
        for ip in list(self._attempts):
            fresh = [attempt for attempt in self._attempts[ip] if attempt > cutoff]
            if fresh:
                self._attempts[ip] = fresh
                total += len(fresh)
            else:
                del self._attempts[ip]
        self._total_attempts = total
        self._last_cleanup = now

    def limited(self, ip: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        if current - self._last_cleanup >= self.cleanup_interval:
            self._cleanup(current)

        attempts = self._attempts.get(ip)
        if attempts is not None:
            cutoff = current - self.window
            fresh = [attempt for attempt in attempts if attempt > cutoff]
            self._total_attempts -= len(attempts) - len(fresh)
            if fresh:
                self._attempts[ip] = fresh
                attempts = fresh
            else:
                del self._attempts[ip]
                attempts = None

        if attempts is not None and len(attempts) >= self.max_per_ip:
            return True
        if attempts is None and len(self._attempts) >= self.max_ips:
            return True
        if self._total_attempts >= self.max_total_attempts:
            return True

        if attempts is None:
            attempts = []
            self._attempts[ip] = attempts
        attempts.append(current)
        self._total_attempts += 1
        return False


_login_limiter = LoginRateLimiter()


@dataclass
class _SessionEntry:
    expires_at: int
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class SessionRegistry:
    """Process-local revocation registry for signed browser sessions."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._entries: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()

    def _prune_locked(self, now: float) -> None:
        for jti, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[jti]

    async def register(self, claims: SessionClaims) -> bool:
        async with self._lock:
            self._prune_locked(time.time())
            if claims.jti in self._entries or len(self._entries) >= self.cap:
                return False
            self._entries[claims.jti] = _SessionEntry(claims.expires_at)
            return True

    async def active(self, claims: SessionClaims) -> bool:
        return await self.active_id(claims.jti, claims.expires_at)

    async def active_id(self, jti: str, expires_at: float) -> bool:
        async with self._lock:
            self._prune_locked(time.time())
            entry = self._entries.get(jti)
            return entry is not None and entry.expires_at == expires_at

    async def subscribe(self, claims: SessionClaims) -> Optional[asyncio.Event]:
        async with self._lock:
            self._prune_locked(time.time())
            entry = self._entries.get(claims.jti)
            if entry is None or entry.expires_at != claims.expires_at:
                return None
            return entry.revoked

    async def revoke(self, jti: str) -> bool:
        async with self._lock:
            entry = self._entries.pop(jti, None)
            if entry is None:
                return False
            entry.revoked.set()
            return True


class _BodyTooLarge(ValueError):
    pass


def _cookie_secure(cfg: RelayConfig) -> bool:
    """Allow an insecure cookie only for the explicit loopback quick-start."""
    origin = urlsplit(cfg.public_origin.strip().rstrip("/"))
    return not (
        origin.scheme == "http"
        and origin.hostname in {"127.0.0.1", "::1", "localhost"}
    )


def _rate_limited(ip: str) -> bool:
    return _login_limiter.limited(ip)


def _request_origin_allowed(req: Request, cfg: RelayConfig) -> bool:
    """Reject browser cross-origin POSTs while retaining non-browser CLI use."""
    origin = req.headers.get("origin", "").strip()
    return not origin or origin == cfg.public_origin


def _request_ip(req: Request) -> str:
    """Use Caddy's client address only when the direct peer is loopback.

    The bundled relay binds 127.0.0.1 behind Caddy. Without this trusted-proxy
    rule every public user shares the single 127.0.0.1 login bucket, so five bad
    attempts can continuously lock out the legitimate user. A directly exposed
    relay never trusts a caller-supplied forwarding header.
    """
    peer = req.client.host if req.client else "?"
    if peer not in {"127.0.0.1", "::1", "localhost"}:
        return peer[:128]
    forwarded = req.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if not forwarded:
        return peer
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return peer


async def _read_json_limited(req: Request, max_bytes: int):
    content_length = req.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError("invalid content-length") from exc
        if declared_length < 0:
            raise ValueError("invalid content-length")
        if declared_length > max_bytes:
            raise _BodyTooLarge

    body = bytearray()
    async for chunk in req.stream():
        if len(body) + len(chunk) > max_bytes:
            raise _BodyTooLarge
        body.extend(chunk)
    return json.loads(body)


async def _active_claims(
    req: Request,
    cfg: RelayConfig,
    sessions: SessionRegistry,
) -> SessionClaims | None:
    token = req.cookies.get(SESSION_COOKIE_NAME, "")
    claims = session_token_claims(token, cfg.session_secret)
    if (
        claims is None
        or claims.expires_at <= time.time()
        or not await sessions.active(claims)
    ):
        return None
    return claims


def _push_subject(claims: SessionClaims) -> str:
    # Legacy password mode intentionally represents one shared user.
    return claims.subject or "legacy"


def _parse_push_subscription(
    body: object,
    claims: SessionClaims,
) -> PushSubscription | None:
    if not isinstance(body, dict):
        return None
    machine_id = body.get("machine_id")
    endpoint = body.get("endpoint")
    keys = body.get("keys")
    if (
        not isinstance(machine_id, str)
        or not valid_machine_id(machine_id)
        or not claims.allows_machine(machine_id)
        or not isinstance(endpoint, str)
        or len(endpoint) > 4096
        or not isinstance(keys, dict)
    ):
        return None
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if (
        not isinstance(p256dh, str)
        or not _PUSH_KEY_RE.fullmatch(p256dh)
        or not isinstance(auth, str)
        or not _PUSH_KEY_RE.fullmatch(auth)
    ):
        return None
    return PushSubscription(
        subject=_push_subject(claims),
        machine_id=machine_id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        session_jti=claims.jti,
        expires_at=claims.expires_at,
    )


async def _serve_client_until_expiry(
    websocket: WebSocket,
    hub: RelayHub,
    expires_at: int,
    revoked: asyncio.Event,
    machine_id: str = "default",
) -> None:
    """Serve until disconnect, signed expiry, or server-side revocation."""
    remaining = max(0.0, expires_at - time.time())
    owner = asyncio.current_task()
    assert owner is not None
    close_signal: asyncio.Future[tuple[int, str]] = (
        asyncio.get_running_loop().create_future()
    )

    async def guard() -> None:
        try:
            await asyncio.wait_for(revoked.wait(), timeout=remaining)
            close = (SESSION_REVOKED_CLOSE_CODE, SESSION_REVOKED_CLOSE_REASON)
        # Python 3.10 raises asyncio.TimeoutError here; builtin TimeoutError is
        # only a reliable alias from Python 3.11 onward.
        except asyncio.TimeoutError:
            close = (SESSION_EXPIRED_CLOSE_CODE, SESSION_EXPIRED_CLOSE_REASON)
        if not close_signal.done():
            close_signal.set_result(close)
            owner.cancel()

    guard_task = asyncio.create_task(guard())
    try:
        if machine_id == "default":
            await hub.serve_client(websocket)
        else:
            await hub.serve_client(websocket, machine_id)
    except asyncio.CancelledError:
        if not close_signal.done():
            raise
        code, reason = close_signal.result()
        log.info("ws session closed", reason=reason)
        await websocket.close(code=code, reason=reason)
    finally:
        if not guard_task.done():
            guard_task.cancel()
        await asyncio.gather(guard_task, return_exceptions=True)


def create_app(cfg: Optional[RelayConfig] = None) -> FastAPI:
    if cfg is None:
        cfg = relay_config()
    validate_relay_config(cfg)
    app = FastAPI(title="cc-remote relay")
    sessions = SessionRegistry(cfg.session_registry_cap)
    push_store: PushSubscriptionStore | None = None
    push_dispatcher: PushDispatcher | None = None
    if cfg.push_vapid_public_key:
        push_store = PushSubscriptionStore(cfg.push_db_path)
        push_dispatcher = PushDispatcher(
            push_store,
            vapid_private_key=cfg.push_vapid_private_key,
            vapid_subject=cfg.push_vapid_subject,
            session_active=lambda subscription: sessions.active_id(
                subscription.session_jti, subscription.expires_at),
        )

    async def on_live_turn_end(machine_id: str, msg: object) -> None:
        if push_dispatcher is None:
            return
        result = getattr(msg, "result", None)
        await push_dispatcher.notify_turn_end(
            machine_id,
            outcome=_turn_push_outcome(result),
        )

    hub = RelayHub(cfg, on_live_turn_end=on_live_turn_end)
    login_slots = asyncio.Semaphore(cfg.login_inflight_cap)
    app.state.hub = hub
    app.state.sessions = sessions
    app.state.login_slots = login_slots
    app.state.push_store = push_store
    app.state.push_dispatcher = push_dispatcher

    @app.get("/api/auth-config")
    async def auth_config() -> JSONResponse:
        return JSONResponse(
            {"multi_user": bool(cfg.login_users_json)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/push-config")
    async def push_config(req: Request) -> JSONResponse:
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse(
                {"ok": False}, status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "enabled": push_dispatcher is not None,
                "public_key": cfg.push_vapid_public_key
                if push_dispatcher is not None else "",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/push/subscribe")
    async def push_subscribe(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        if push_store is None:
            return JSONResponse({"error": "push_disabled"}, status_code=503)
        try:
            body = await _read_json_limited(req, _PUSH_BODY_MAX_BYTES)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        subscription = _parse_push_subscription(body, claims)
        if subscription is None:
            return JSONResponse({"error": "invalid_subscription"}, status_code=400)
        await push_store.upsert(subscription)
        return JSONResponse(
            {"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        if push_store is None:
            return JSONResponse({"ok": True})
        try:
            body = await _read_json_limited(req, _PUSH_BODY_MAX_BYTES)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        if not isinstance(endpoint, str) or not (1 <= len(endpoint) <= 4096):
            return JSONResponse({"error": "invalid_subscription"}, status_code=400)
        await push_store.remove_endpoint(_push_subject(claims), endpoint)
        return JSONResponse(
            {"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/login")
    async def login(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse(
                {"error": "origin_rejected"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        ip = _request_ip(req)
        if _rate_limited(ip):
            return JSONResponse(
                {"error": "rate_limited"},
                status_code=429,
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        # Do not queue an unbounded number of slow bodies behind the gate.
        # There is no await between locked() and acquire(), so this check and
        # the immediate decrement are atomic with respect to the event loop.
        if login_slots.locked():
            return JSONResponse(
                {"error": "login_capacity"},
                status_code=503,
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        await login_slots.acquire()
        try:
            try:
                body = await asyncio.wait_for(
                    _read_json_limited(req, cfg.login_body_max_bytes),
                    timeout=cfg.login_read_timeout,
                )
            except asyncio.TimeoutError:
                return JSONResponse(
                    {"error": "request_timeout"},
                    status_code=408,
                    headers={"Cache-Control": "no-store"},
                )
        except _BodyTooLarge:
            return JSONResponse(
                {"error": "too_large"},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            return JSONResponse(
                {"error": "bad_request"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        finally:
            login_slots.release()
        candidate = body.get("password", "") if isinstance(body, dict) else ""
        password = candidate if isinstance(candidate, str) else ""
        username_value = body.get("username", "") if isinstance(body, dict) else ""
        username = username_value if isinstance(username_value, str) else ""
        access = authenticate_login(username, password, cfg)
        if access is None:
            log.warning("login failed", ip=ip)
            return JSONResponse({"error": "invalid"}, status_code=401)
        subject, machines = access
        token, exp = make_session_token(
            cfg.session_secret,
            cfg.session_ttl_seconds,
            subject=subject,
            machines=machines,
        )
        claims = session_token_claims(token, cfg.session_secret)
        assert claims is not None
        if not await sessions.register(claims):
            log.warning("session registry full", ip=ip)
            return JSONResponse({"error": "session_capacity"}, status_code=503)
        log.info("login ok", ip=ip, exp=exp)
        response = JSONResponse(
            {"ok": True, "exp": exp}, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=cfg.session_ttl_seconds,
            path="/",
            secure=_cookie_secure(cfg),
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/session")
    async def session_status(req: Request) -> JSONResponse:
        token = req.cookies.get(SESSION_COOKIE_NAME, "")
        claims = session_token_claims(token, cfg.session_secret)
        if (
            claims is None
            or claims.expires_at <= time.time()
            or not await sessions.active(claims)
        ):
            return JSONResponse(
                {"ok": False}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse(
            {"ok": True, "exp": claims.expires_at,
             "username": claims.subject},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/logout")
    async def logout(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse(
                {"error": "origin_rejected"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        token = req.cookies.get(SESSION_COOKIE_NAME, "")
        claims = session_token_claims(token, cfg.session_secret)
        if claims is not None:
            if push_store is not None:
                await push_store.remove_session(claims.jti)
            await sessions.revoke(claims.jti)
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=_cookie_secure(cfg),
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/machines")
    async def machine_list(req: Request) -> JSONResponse:
        token = req.cookies.get(SESSION_COOKIE_NAME, "")
        claims = session_token_claims(token, cfg.session_secret)
        if (
            claims is None
            or claims.expires_at <= time.time()
            or not await sessions.active(claims)
        ):
            return JSONResponse(
                {"ok": False}, status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        machines = [machine_id for machine_id in hub.machine_ids
                    if claims.allows_machine(machine_id)]
        return JSONResponse(
            {"ok": True, "machines": machines},
            headers={"Cache-Control": "no-store"},
        )

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        authorization = websocket.headers.get("authorization", "")
        role = authenticate(authorization, cfg)  # wrapper Bearer
        wrapper_scope: str | None = None
        if role == "wrapper":
            wrapper_scope = wrapper_machine_scope(
                authorization[7:].strip(), cfg)
        claims: Optional[SessionClaims] = None
        if role is None:
            token = websocket.cookies.get(SESSION_COOKIE_NAME, "")
            origin = websocket.headers.get("origin", "")
            expected_origin = cfg.public_origin.strip().rstrip("/")
            origin_ok = not expected_origin or origin == expected_origin
            claims = session_token_claims(token, cfg.session_secret)
            if (
                claims is not None
                and claims.expires_at > time.time()
                and origin_ok
                and await sessions.active(claims)
            ):
                role = "client"
            elif token and not origin_ok:
                log.warning("ws origin rejected", origin=origin or "-")
        if role is None:
            await websocket.close(code=1008, reason="unauthorized")
            return
        await websocket.accept()
        log.info("ws accepted", role=role)
        if role == "wrapper":
            await hub.serve_wrapper(
                websocket,
                None if wrapper_scope == "*" else wrapper_scope,
            )
        else:
            assert claims is not None
            revoked = await sessions.subscribe(claims)
            if revoked is None:
                await websocket.close(
                    code=SESSION_REVOKED_CLOSE_CODE,
                    reason=SESSION_REVOKED_CLOSE_REASON,
                )
                return
            requested_machine = websocket.query_params.get("machine", "").strip()
            if requested_machine and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", requested_machine
            ):
                await websocket.close(code=1008, reason="invalid machine")
                return
            if requested_machine and not claims.allows_machine(requested_machine):
                await websocket.close(code=1008, reason="machine not authorized")
                return
            if requested_machine:
                machine_id = requested_machine
            else:
                connected = [candidate for candidate in hub.machine_ids
                             if claims.allows_machine(candidate)]
                if connected:
                    machine_id = connected[0]
                elif "*" not in claims.machines:
                    machine_id = claims.machines[0]
                else:
                    machine_id = hub.default_machine_id()
            if machine_id == "default":
                # Keep the legacy call shape for embedded relays and tests that
                # replace the expiry guard. Named machines use the extended
                # route-aware form below.
                await _serve_client_until_expiry(
                    websocket, hub, claims.expires_at, revoked)
            else:
                await _serve_client_until_expiry(
                    websocket, hub, claims.expires_at, revoked, machine_id)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "wrapper_connected": hub.wrapper_connected,
                "machines": hub.machine_ids, "clients": hub.client_count}

    # Static web client. Mounted last so /api/login and /ws and /healthz win.
    if cfg.static_dir and os.path.isdir(cfg.static_dir):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="static")
        log.info("serving static web client", dir=cfg.static_dir)

    return app
