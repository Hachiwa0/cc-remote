"""Bounded loopback client for the DeepSeek Harness (DSH) Web API.

DSH owns its model credentials, plugin runtime, agents, and durable session log.
cc-remote talks only to DSH's local HTTP RPC carrier and its two downstream
WebSocket feeds.  The carrier currently has no authentication, so this module
deliberately refuses every non-loopback authority even when a caller supplies
one explicitly.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

from cc_remote.log import logger

log = logger("cc_remote.wrapper.dsh_client")

DSH_WIRE_PREFIX = "dsh@"
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_COMMAND_LINE = re.compile(r"^/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])")


def dsh_wire_session_id(native_session_id: str) -> str:
    """Namespace one DSH id before it enters the shared cc-remote router."""
    if (
        not isinstance(native_session_id, str)
        or not native_session_id
        or len(native_session_id) > 123
        or not native_session_id[0].isalnum()
        or any(
            not (character.isalnum() or character in "._:-")
            for character in native_session_id
        )
    ):
        raise ValueError("DSH session id cannot be represented on the wire")
    return f"{DSH_WIRE_PREFIX}{native_session_id}"


def dsh_native_session_id(wire_session_id: str) -> str:
    """Decode an exact DSH wire id; bare UUIDs are intentionally rejected."""
    if not isinstance(wire_session_id, str) or not wire_session_id.startswith(
        DSH_WIRE_PREFIX
    ):
        raise ValueError("not a DSH wire session id")
    native = wire_session_id[len(DSH_WIRE_PREFIX):]
    if dsh_wire_session_id(native) != wire_session_id:
        raise ValueError("invalid DSH wire session id")
    return native


class DshError(RuntimeError):
    """Base class for a DSH transport or contract failure."""


class DshUnavailable(DshError):
    """The configured loopback DSH host cannot currently be reached."""


class DshProtocolError(DshError):
    """DSH returned a carrier shape cc-remote cannot safely consume."""


class DshRpcError(DshError):
    """A well-formed DSH RPC returned a business error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class DshPreflight:
    host: dict[str, Any]
    presets: list[dict[str, Any]]
    default_preset_id: str | None


@dataclass(frozen=True)
class DshCommandDescriptor:
    """Display-safe effective command contributed to one DSH Agent."""

    name: str
    description: str
    input_hint: str | None = None


@dataclass(frozen=True)
class DshCommandExecution:
    """Exact durable identity and settled result of one DSH command."""

    command_id: str
    kind: str
    text: str | None = None
    source_event_seq: int | None = None


FrameCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
AvailabilityCallback = Callable[
    [bool, DshPreflight | None, str | None], Awaitable[None] | None
]


def normalize_dsh_url(value: str) -> str:
    """Return a canonical loopback HTTP origin or raise ``ValueError``."""
    raw = value.strip()
    if not raw:
        raise ValueError("DSH URL must not be empty")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("DSH URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "DSH URL must be a credential-free loopback http origin"
        )
    host = parsed.hostname
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != 80:
        authority = f"{authority}:{port}"
    return urlunsplit(("http", authority, "", "", ""))


class DshClient:
    """One supervised connection to a local DSH host.

    Unary traffic uses POST ``/api/<method>``.  DSH's two WebSockets are
    downlink-only; responding to approvals/questions goes through
    ``POST /api/respond`` and echoes the server request's rpcId.
    """

    ALLOWED_METHODS = frozenset({
        "host.describe",
        "session.list",
        "session.create",
        "session.history",
        "session.models",
        "session.selectModel",
        "session.rename",
        "session.fork",
        "session.prompt",
        "session.attachment",
        "session.cancel",
        "workspace.list",
        "workspace.archiveSession",
        "agentPreset.list",
        "skill.list",
        "llm.models",
        # Plugin-contributed slash commands are discovered and executed
        # through DSH's Typert gateway.  Plugin installation/configuration RPCs
        # intentionally remain absent from this allowlist.
        "commands/list",
        "commands/execute",
    })
    PREFLIGHT_PRESET_CAP = 128

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_request_bytes: int = 32 * 1024 * 1024,
        max_response_bytes: int = 16 * 1024 * 1024,
        max_frame_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.base_url = normalize_dsh_url(base_url)
        parsed_timeout = float(timeout)
        if not math.isfinite(parsed_timeout) or not 0.1 <= parsed_timeout <= 60.0:
            raise ValueError("DSH timeout must be between 0.1 and 60 seconds")
        self.timeout = parsed_timeout
        self.max_request_bytes = max(1024, int(max_request_bytes))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.max_frame_bytes = max(1024, int(max_frame_bytes))
        # Constructing WrapperMachine is a pure operation in a large part of
        # the unit suite and in configuration probes.  Do not allocate an
        # AsyncClient until the first actual DSH request; otherwise every such
        # constructor owns a lifecycle resource even when DSH is never started.
        self._http: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.available = False
        self.preflight: DshPreflight | None = None
        self.unavailable_reason: str | None = None

    async def call(
        self,
        method: str,
        payload: Mapping[str, Any] | None = None,
        *,
        rpc_id: str | None = None,
        no_timeout: bool = False,
    ) -> Any:
        if method not in self.ALLOWED_METHODS:
            raise ValueError(f"DSH RPC method is not allowlisted: {method}")
        request_id = rpc_id or uuid4().hex
        self._validate_rpc_id(request_id)
        body = {
            "type": "client-request",
            "rpcId": request_id,
            "method": method,
            "payload": dict(payload or {}),
        }
        raw = self._encode_request(body)
        response = await self._post_bounded(
            f"/api/{method}", raw, no_timeout=no_timeout,
        )
        if response.get("type") != "server-response":
            raise DshProtocolError("DSH RPC response has an invalid type")
        if response.get("rpcId") != request_id:
            raise DshProtocolError("DSH RPC response rpcId mismatch")
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise DshProtocolError("DSH RPC response has an invalid result")
        if result["ok"]:
            if "value" not in result:
                raise DshProtocolError("DSH successful RPC omitted value")
            return result["value"]
        error = result.get("error")
        if not isinstance(error, dict):
            raise DshProtocolError("DSH failed RPC omitted its error")
        code = error.get("code")
        message = error.get("message")
        details = error.get("details")
        if not isinstance(code, str) or not isinstance(message, str):
            raise DshProtocolError("DSH RPC error has an invalid shape")
        raise DshRpcError(
            code,
            message,
            details=details if isinstance(details, dict) else None,
        )

    async def respond(
        self,
        rpc_id: str,
        value: Mapping[str, Any],
    ) -> bool:
        """Answer one DSH server request; return false for a stale request."""
        self._validate_rpc_id(rpc_id)
        raw = self._encode_request({
            "type": "client-response",
            "rpcId": rpc_id,
            "result": {"ok": True, "value": dict(value)},
        })
        receipt = await self._post_bounded("/api/respond", raw)
        accepted = receipt.get("accepted")
        if not isinstance(accepted, bool):
            raise DshProtocolError("DSH response receipt has an invalid shape")
        if not accepted:
            reason = receipt.get("reason")
            if reason not in {"not-pending", "bad-response"}:
                raise DshProtocolError("DSH response receipt has an invalid reason")
        return accepted

    async def respond_error(
        self,
        rpc_id: str,
        *,
        code: str = "cancelled",
        message: str = "Remote interaction was cancelled",
    ) -> bool:
        """Close one answerable server request through the error branch.

        DSH validates question answers as a complete non-empty batch.  Sending
        ``answers: []`` when the browser disappears is therefore not a legal
        cancellation and leaves the native question pending.  The RPC carrier
        has an explicit ``cancelled`` result for this exact boundary.
        """
        if code != "cancelled":
            raise ValueError("only the DSH cancelled response is supported")
        self._validate_rpc_id(rpc_id)
        raw = self._encode_request({
            "type": "client-response",
            "rpcId": rpc_id,
            "result": {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message[:4096],
                    "details": {},
                },
            },
        })
        receipt = await self._post_bounded("/api/respond", raw)
        accepted = receipt.get("accepted")
        if not isinstance(accepted, bool):
            raise DshProtocolError("DSH response receipt has an invalid shape")
        if not accepted:
            reason = receipt.get("reason")
            if reason not in {"not-pending", "bad-response"}:
                raise DshProtocolError("DSH response receipt has an invalid reason")
        return accepted

    async def probe(self) -> DshPreflight:
        """Verify the minimum contract needed by the integration."""
        host = await self.call("host.describe")
        sessions = await self.call("session.list")
        presets = await self.call("agentPreset.list")
        if not isinstance(host, dict):
            raise DshProtocolError("host.describe did not return an object")
        if not isinstance(host.get("version"), str):
            raise DshProtocolError("host.describe omitted version")
        if not isinstance(host.get("cwd"), str):
            raise DshProtocolError("host.describe omitted cwd")
        for key in ("provider", "model"):
            if key in host and not isinstance(host.get(key), str):
                raise DshProtocolError(f"host.describe returned invalid {key}")
        if (
            isinstance(host.get("attachedSessions"), bool)
            or not isinstance(host.get("attachedSessions"), int)
            or host["attachedSessions"] < 0
        ):
            raise DshProtocolError("host.describe omitted attachedSessions")
        if not isinstance(host.get("canOpenPath"), bool):
            raise DshProtocolError("host.describe omitted canOpenPath")
        rows = sessions.get("items") if isinstance(sessions, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise DshProtocolError("session.list did not return an item list")
        preset_rows = presets.get("presets") if isinstance(presets, dict) else None
        if (
            not isinstance(preset_rows, list)
            or not all(isinstance(row, dict) for row in preset_rows)
        ):
            raise DshProtocolError("agentPreset.list did not return presets")
        default_ids = [
            row.get("id") for row in preset_rows
            if row.get("isDefault") is True and isinstance(row.get("id"), str)
        ]
        if len(default_ids) > 1:
            raise DshProtocolError("agentPreset.list returned multiple defaults")

        # Preflight lives for the full supervised connection. Keep only the
        # path-free fields cc-remote actually presents instead of retaining a
        # second copy of the complete session catalog or arbitrary plugin data.
        host_projection: dict[str, Any] = {
            "version": host["version"][:128],
            "cwd": host["cwd"][:4096],
            "attachedSessions": host["attachedSessions"],
            "canOpenPath": host["canOpenPath"],
        }
        for key in ("provider", "model"):
            value = host.get(key)
            if isinstance(value, str) and value:
                host_projection[key] = value[:256]

        preset_projection: list[dict[str, Any]] = []
        default_projection: dict[str, Any] | None = None
        for row in preset_rows:
            preset_id = row.get("id")
            trust = row.get("trust")
            is_default = row.get("isDefault")
            if (
                not isinstance(preset_id, str)
                or not preset_id
                or len(preset_id) > 256
                or trust not in {"system", "user"}
                or not isinstance(is_default, bool)
            ):
                continue
            projected: dict[str, Any] = {
                "id": preset_id,
                "trust": trust,
                "isDefault": is_default,
            }
            name = row.get("name")
            description = row.get("description")
            broken = row.get("broken")
            if isinstance(name, str):
                projected["name"] = name[:256]
            if isinstance(description, str):
                projected["description"] = description[:4096]
            if isinstance(broken, str) and broken:
                projected["broken"] = broken[:4096]
            if len(preset_projection) < self.PREFLIGHT_PRESET_CAP:
                preset_projection.append(projected)
            if is_default:
                default_projection = projected

        representable_default = (
            default_ids[0]
            if default_ids
            and isinstance(default_ids[0], str)
            and 0 < len(default_ids[0]) <= 256
            else None
        )
        if (
            default_projection is not None
            and not any(
                row["id"] == default_projection["id"]
                for row in preset_projection
            )
        ):
            if len(preset_projection) >= self.PREFLIGHT_PRESET_CAP:
                preset_projection[-1] = default_projection
            else:
                preset_projection.append(default_projection)
        return DshPreflight(
            host=host_projection,
            presets=preset_projection,
            default_preset_id=representable_default,
        )

    async def start(
        self,
        on_mux: FrameCallback,
        on_host: FrameCallback,
        on_availability: AvailabilityCallback,
    ) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(
            self._supervise(on_mux, on_host, on_availability),
            name="cc-remote-dsh",
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def _supervise(
        self,
        on_mux: FrameCallback,
        on_host: FrameCallback,
        on_availability: AvailabilityCallback,
    ) -> None:
        backoff = 1.0
        while not self._stopping:
            tasks: list[asyncio.Task[None]] = []
            try:
                preflight = await self.probe()
                mux_ready = asyncio.Event()
                host_ready = asyncio.Event()
                tasks = [
                    asyncio.create_task(
                        self._read_stream("/api/events.mux", on_mux, mux_ready),
                        name="cc-remote-dsh-mux",
                    ),
                    asyncio.create_task(
                        self._read_stream("/api/events.host", on_host, host_ready),
                        name="cc-remote-dsh-host",
                    ),
                ]
                await asyncio.wait_for(
                    asyncio.gather(mux_ready.wait(), host_ready.wait()),
                    timeout=self.timeout,
                )
                self.preflight = preflight
                self.available = True
                self.unavailable_reason = None
                await self._invoke(on_availability, True, preflight, None)
                backoff = 1.0
                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
                raise DshUnavailable("DSH event stream closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = self._safe_reason(exc)
                if self.available or reason != self.unavailable_reason:
                    log.info(
                        "DSH unavailable",
                        error_type=type(exc).__name__,
                        reason=reason,
                    )
                self.available = False
                self.preflight = None
                self.unavailable_reason = reason
                await self._invoke(on_availability, False, None, reason)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2.0, 30.0)

    async def _read_stream(
        self,
        path: str,
        callback: FrameCallback,
        ready: asyncio.Event,
    ) -> None:
        websocket_url = self._websocket_url(path)
        async with connect(
            websocket_url,
            max_size=self.max_frame_bytes,
            max_queue=32,
            open_timeout=self.timeout,
            proxy=None,
        ) as websocket:
            ready.set()
            async for raw in websocket:
                if not isinstance(raw, str):
                    raise DshProtocolError("DSH event stream emitted binary data")
                if len(raw.encode("utf-8")) > self.max_frame_bytes:
                    raise DshProtocolError("DSH event stream frame exceeds limit")
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    raise DshProtocolError("DSH event stream emitted invalid JSON") from exc
                frame = self._validate_server_request(message)
                await self._invoke(callback, frame)

    def _websocket_url(self, path: str) -> str:
        parsed = urlsplit(self.base_url)
        return urlunsplit(("ws", parsed.netloc, path, "", ""))

    @staticmethod
    async def _invoke(callback: Callable[..., Any], *args: Any) -> None:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _validate_rpc_id(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8", "surrogatepass")) > 256
            or "\x00" in value
        ):
            raise ValueError("DSH rpcId must be a bounded non-empty string")

    def _encode_request(self, value: Mapping[str, Any]) -> bytes:
        try:
            raw = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ValueError("DSH request is not JSON serializable") from exc
        if len(raw) > self.max_request_bytes:
            raise ValueError("DSH request exceeds the configured byte limit")
        return raw

    async def _post_bounded(
        self,
        path: str,
        raw: bytes,
        *,
        no_timeout: bool = False,
    ) -> dict[str, Any]:
        client = self._http_client()
        try:
            async with client.stream(
                "POST",
                path,
                content=raw,
                headers={"content-type": "application/json"},
                timeout=None if no_timeout else self.timeout,
            ) as response:
                if response.status_code != 200:
                    raise DshUnavailable(
                        f"DSH HTTP carrier returned {response.status_code}"
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise DshProtocolError(
                            "DSH response has an invalid content-length"
                        ) from exc
                    if declared_size > self.max_response_bytes:
                        raise DshProtocolError("DSH response exceeds byte limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise DshProtocolError("DSH response exceeds byte limit")
                    chunks.append(chunk)
        except DshError:
            raise
        except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            raise DshUnavailable("DSH loopback carrier is unavailable") from exc
        try:
            value = json.loads(b"".join(chunks))
        except (ValueError, UnicodeDecodeError) as exc:
            raise DshProtocolError("DSH response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise DshProtocolError("DSH response must be a JSON object")
        return value

    def _http_client(self) -> httpx.AsyncClient:
        client = self._http
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                trust_env=False,
                follow_redirects=False,
            )
            self._http = client
        return client

    @staticmethod
    def _validate_server_request(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("type") != "server-request":
            raise DshProtocolError("DSH event envelope is not a server request")
        rpc_id = value.get("rpcId")
        method = value.get("method")
        payload = value.get("payload")
        if (
            not isinstance(rpc_id, str)
            or not rpc_id
            or len(rpc_id.encode("utf-8", "surrogatepass")) > 256
            or not isinstance(method, str)
            or not method
            or not isinstance(payload, dict)
            or payload.get("type") != method
        ):
            raise DshProtocolError("DSH event envelope has an invalid shape")
        return {
            "rpcId": rpc_id,
            "method": method,
            "payload": dict(payload),
        }

    @staticmethod
    def _safe_reason(exc: Exception) -> str:
        if isinstance(exc, DshRpcError):
            return f"{exc.code}: {exc.message}"[:384]
        if isinstance(exc, DshProtocolError):
            return str(exc)[:384]
        if isinstance(exc, DshUnavailable):
            return str(exc)[:384]
        return "DSH loopback service is unavailable"


class DshSessionHandle:
    """Small session-scoped facade used by ``SessionContext``.

    DSH owns the actual agent lifecycle in its host process; disconnecting a
    cc-remote resident therefore releases no DSH process and intentionally does
    not cancel a running turn.
    """

    def __init__(
        self,
        client: DshClient,
        session_id: str,
        cwd: str,
        *,
        model: str | None = None,
        effort: str | None = None,
        agent_preset: str | None = None,
    ) -> None:
        self.client = client
        self.session_id = session_id
        self.cwd = cwd
        self.model = model
        self.effort = effort
        self.agent_preset = agent_preset
        # These are DSH session projections, not cc-remote defaults.  Keep them
        # unknown until the host supplies the selected Agent's actual values.
        self.permission_mode = ""
        self.permission_options: tuple[dict[str, Any], ...] = ()
        self.context_pressure: dict[str, int] = {}
        self.context_breakdown: dict[str, int] = {}
        self._projection_seq: dict[str, int] = {}

    async def disconnect(self) -> None:
        return None

    async def prompt(
        self,
        prompt: str,
        *,
        images: list[dict[str, Any]] | None = None,
        rpc_id: str,
        mode: str = "queue",
        time_zone: str | None = None,
    ) -> Any:
        if mode not in {"queue", "steer"}:
            raise ValueError("invalid DSH prompt mode")
        content: list[dict[str, Any]] = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        for image in images or ():
            media_type = image.get("media_type")
            if media_type == "image/jpg":
                media_type = "image/jpeg"
            data = image.get("data")
            if media_type not in {
                "image/png", "image/jpeg", "image/webp", "image/gif",
            } or not isinstance(data, str):
                raise ValueError("invalid DSH image attachment")
            content.append({
                "type": "image",
                "mediaType": media_type,
                "data": data,
            })
        payload: dict[str, Any] = {
            "sessionId": self.session_id,
            "mode": mode,
            "content": content,
        }
        if time_zone:
            payload["clientTimeZone"] = time_zone
        value = await self.client.call(
            "session.prompt", payload, rpc_id=rpc_id,
        )
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshProtocolError("session.prompt returned an invalid receipt")
        command = value.get("command")
        if command is not None and (
            not isinstance(command, dict)
            or command.get("kind") != "success"
            or (
                "text" in command
                and not isinstance(command.get("text"), str)
            )
        ):
            raise DshProtocolError("session.prompt returned an invalid command")
        return value

    async def resolve_command(
        self, line: str,
    ) -> DshCommandDescriptor | None:
        """Resolve an exact slash token against this Agent's live registry.

        Commands may be contributed or shadowed by the selected Agent Preset,
        so a static cc-remote list is never authoritative.  Non-command text
        returns without touching the host; unknown slash text remains on
        ``session.prompt`` so DSH can apply its Skill/pre-step semantics.
        """
        if not line.startswith("/"):
            return None
        match = _COMMAND_LINE.match(line)
        if match is None:
            return None
        name = match.group(1)
        value = await self.client.call("commands/list", {
            "args": {"agentId": self.session_id},
        })
        if not isinstance(value, list) or len(value) > 2048:
            raise DshProtocolError("commands/list returned an invalid catalog")
        resolved: DshCommandDescriptor | None = None
        seen: set[str] = set()
        for row in value:
            if not isinstance(row, dict):
                raise DshProtocolError("commands/list returned an invalid command")
            row_name = row.get("name")
            description = row.get("description")
            input_value = row.get("input")
            if (
                not isinstance(row_name, str)
                or not row_name
                or len(row_name) > 512
                or _COMMAND_NAME.fullmatch(row_name) is None
                or not isinstance(description, str)
                or len(description) > 16 * 1024
                or row_name in seen
                or (
                    input_value is not None
                    and (
                        not isinstance(input_value, dict)
                        or not isinstance(input_value.get("hint"), str)
                        or len(input_value["hint"]) > 4096
                    )
                )
            ):
                raise DshProtocolError("commands/list returned an invalid command")
            seen.add(row_name)
            if row_name == name:
                resolved = DshCommandDescriptor(
                    name=row_name,
                    description=description,
                    input_hint=(
                        input_value["hint"]
                        if isinstance(input_value, dict) else None
                    ),
                )
        return resolved

    async def execute_command(
        self,
        line: str,
        *,
        rpc_id: str,
    ) -> DshCommandExecution:
        """Execute one previously resolved command and retain its native id.

        DSH command handlers may legitimately run longer than the ordinary
        carrier health timeout.  The owning asyncio task is the cancellation
        boundary: cancelling it closes the HTTP request, which DSH forwards to
        the handler's AbortSignal.
        """
        value = await self.client.call(
            "commands/execute",
            {"args": {"agentId": self.session_id, "line": line}},
            rpc_id=rpc_id,
            no_timeout=True,
        )
        if not isinstance(value, dict):
            raise DshProtocolError("commands/execute omitted its execution")
        command_id = value.get("commandId")
        result = value.get("result")
        if (
            not isinstance(command_id, str)
            or not command_id
            or len(command_id.encode("utf-8", "surrogatepass")) > 1024
            or "\x00" in command_id
            or not isinstance(result, dict)
            or result.get("kind") not in {"success", "error"}
            or (
                result.get("kind") == "error"
                and not isinstance(result.get("text"), str)
            )
            or (
                "text" in result
                and not isinstance(result.get("text"), str)
            )
        ):
            raise DshProtocolError("commands/execute returned an invalid execution")
        source_seq = result.get("sourceEventSeq")
        if (
            source_seq is not None
            and (
                result["kind"] != "success"
                or not isinstance(source_seq, int)
                or isinstance(source_seq, bool)
                or source_seq < 0
            )
        ):
            raise DshProtocolError("commands/execute returned an invalid source event")
        return DshCommandExecution(
            command_id=command_id,
            kind=result["kind"],
            text=result.get("text"),
            source_event_seq=source_seq,
        )

    @staticmethod
    def _projection_integer(value: Any, *, field: str) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > 9_007_199_254_740_991
        ):
            raise DshProtocolError(
                f"DSH projection has an invalid {field} value"
            )
        return value

    @classmethod
    def _permission_projection(
        cls, value: Any,
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        if not isinstance(value, dict):
            raise DshProtocolError("DSH permissions projection is invalid")
        current = value.get("currentValue")
        options = value.get("options")
        if (
            not isinstance(current, str)
            or not 0 < len(current) <= 256
            or not isinstance(options, list)
            or len(options) > 64
        ):
            raise DshProtocolError("DSH permissions projection is invalid")
        projected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for option in options:
            if not isinstance(option, dict):
                raise DshProtocolError(
                    "DSH permissions projection has an invalid option"
                )
            option_id = option.get("value")
            name = option.get("name")
            description = option.get("description")
            if (
                not isinstance(option_id, str)
                or not 0 < len(option_id) <= 256
                or not isinstance(name, str)
                or not 0 < len(name) <= 256
                or option_id in seen
                or (
                    description is not None
                    and (
                        not isinstance(description, str)
                        or len(description) > 2048
                    )
                )
            ):
                raise DshProtocolError(
                    "DSH permissions projection has an invalid option"
                )
            seen.add(option_id)
            # ``custom`` is a derived display state in DSH and cannot be a
            # command target.  Preserve it as the current mode, never as a
            # selectable row.
            if option_id == "custom":
                continue
            display_name = (
                "Full Access"
                if option_id == "danger-full-access"
                else " ".join(
                    word[:1].upper() + word[1:]
                    for word in name.split("-")
                )
                if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                else name
            )
            projected.append({
                "id": option_id,
                "name": display_name,
                **(
                    {"description": description}
                    if description is not None else {}
                ),
                "danger": option_id == "danger-full-access",
            })
        if current not in seen:
            raise DshProtocolError(
                "DSH permissions projection omitted its current option"
            )
        return current, tuple(projected)

    @classmethod
    def _context_pressure_projection(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise DshProtocolError("DSH contextPressure projection is invalid")
        projected: dict[str, int] = {}
        for field in ("pressureTokens", "projectedTokens", "contextWindow"):
            raw = value.get(field)
            if raw is not None:
                projected[field] = cls._projection_integer(raw, field=field)
        return projected

    @classmethod
    def _context_breakdown_projection(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise DshProtocolError("DSH contextBreakdown projection is invalid")
        projected: dict[str, int] = {}
        for field in ("systemTokens", "toolsTokens", "messageTokens"):
            raw = value.get(field)
            if raw is None:
                continue
            projected[field] = cls._projection_integer(raw, field=field)
        return projected

    def apply_projection(self, key: str, value: Any, seq: int) -> bool:
        """Apply one DSH last-wins projection without letting an older page
        overwrite a newer mux frame.  Returns whether the visible value changed.
        """
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < -1
            or seq > 9_007_199_254_740_991
        ):
            raise DshProtocolError("DSH projection has an invalid sequence")
        if key == "permissions":
            parsed: Any = self._permission_projection(value)
            current_value: Any = (
                self.permission_mode, self.permission_options
            )
        elif key == "contextPressure":
            parsed = self._context_pressure_projection(value)
            current_value = self.context_pressure
        elif key == "contextBreakdown":
            parsed = self._context_breakdown_projection(value)
            current_value = self.context_breakdown
        else:
            return False
        previous_seq = self._projection_seq.get(key, -2)
        if seq < previous_seq:
            return False
        if seq == previous_seq and parsed != current_value:
            raise DshProtocolError(
                f"DSH {key} projection conflicts at sequence {seq}"
            )
        self._projection_seq[key] = seq
        if parsed == current_value:
            return False
        if key == "permissions":
            self.permission_mode, self.permission_options = parsed
        elif key == "contextPressure":
            self.context_pressure = parsed
        else:
            self.context_breakdown = parsed
        return True

    def apply_projections(
        self, values: Mapping[str, Any], seq: int,
    ) -> set[str]:
        changed: set[str] = set()
        for key in ("permissions", "contextPressure", "contextBreakdown"):
            if key in values and self.apply_projection(key, values[key], seq):
                changed.add(key)
        return changed

    async def refresh_projections(self) -> set[str]:
        """Read the host-folded session projections without creating a turn."""
        raw = await self.client.call("session.history", {
            "sessionId": self.session_id,
            "maxMessages": 8,
        })
        if not isinstance(raw, dict):
            raise DshProtocolError("session.history did not return an object")
        projections = raw.get("projections")
        if projections is None:
            return set()
        if not isinstance(projections, dict):
            raise DshProtocolError("session.history projections are invalid")
        seq = projections.get("asOfSeq")
        values = projections.get("values")
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < -1
            or seq > 9_007_199_254_740_991
            or not isinstance(values, dict)
        ):
            raise DshProtocolError("session.history projections are invalid")
        return self.apply_projections(values, seq)

    async def set_permission_mode(self, mode: str, *, rpc_id: str) -> str:
        """Switch one live DSH session through its native /permission command.

        The projection is read before execution so a reliable retry becomes a
        no-op after a lost success receipt, and again afterwards so Web only
        announces a value the durable DSH log actually confirms.
        """
        if not isinstance(mode, str) or not 0 < len(mode) <= 256:
            raise ValueError("invalid DSH permission preset")
        await self.refresh_projections()
        allowed = {option["id"] for option in self.permission_options}
        if mode not in allowed:
            raise ValueError("DSH permission preset is not selectable")
        if self.permission_mode == mode:
            return mode
        receipt_error: DshError | None = None
        try:
            execution = await self.execute_command(
                f"/permission {mode}", rpc_id=rpc_id,
            )
        except (DshUnavailable, DshProtocolError) as exc:
            receipt_error = exc
        else:
            if execution.kind != "success":
                raise DshRpcError(
                    "permission-switch-failed",
                    execution.text or "DSH rejected the permission preset",
                )
        try:
            await self.refresh_projections()
        except DshError:
            if receipt_error is not None:
                raise receipt_error
            raise
        if self.permission_mode != mode:
            if receipt_error is not None:
                raise receipt_error
            raise DshProtocolError(
                "DSH permission projection did not confirm the selection"
            )
        return mode

    async def cancel(self) -> Any:
        value = await self.client.call(
            "session.cancel", {"sessionId": self.session_id}
        )
        if not isinstance(value, dict) or value.get("accepted") is not True:
            raise DshProtocolError("session.cancel returned an invalid receipt")
        return value

    async def interrupt(self) -> Any:
        return await self.cancel()

    async def refresh_models(self) -> dict[str, Any]:
        value = await self.client.call(
            "session.models", {"sessionId": self.session_id}
        )
        if not isinstance(value, dict):
            raise DshProtocolError("session.models returned an invalid value")
        current = value.get("current")
        if not isinstance(current, dict):
            raise DshProtocolError("session.models omitted current selection")
        provider = current.get("provider")
        model = current.get("model")
        effort = current.get("reasoningEffort")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise DshProtocolError("session.models current selection is invalid")
        from cc_remote.wrapper.dsh_stream import encode_dsh_model

        self.model = encode_dsh_model(provider, model)
        self.effort = effort if isinstance(effort, str) and effort else None
        return value

    async def select_model(
        self,
        provider: str,
        model: str,
        effort: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sessionId": self.session_id,
            "provider": provider,
            "model": model,
        }
        if effort:
            payload["reasoningEffort"] = effort
        try:
            value = await self.client.call("session.selectModel", payload)
        except (DshUnavailable, DshProtocolError) as receipt_error:
            # Model selection is durable session state.  A loopback carrier can
            # fail after DSH committed it, so reconcile the exact current route
            # before telling the browser the mutation failed.
            try:
                current_value = await self.client.call(
                    "session.models", {"sessionId": self.session_id}
                )
            except DshError:
                raise receipt_error
            current = (
                current_value.get("current")
                if isinstance(current_value, dict) else None
            )
            if not (
                isinstance(current, dict)
                and current.get("provider") == provider
                and current.get("model") == model
                and (
                    effort is None
                    or current.get("reasoningEffort") == effort
                )
            ):
                raise receipt_error
            value = {"selected": current}
        if not isinstance(value, dict) or not isinstance(value.get("selected"), dict):
            raise DshProtocolError("session.selectModel returned an invalid value")
        selected = value["selected"]
        applied_provider = selected.get("provider")
        applied_model = selected.get("model")
        applied_effort = selected.get("reasoningEffort")
        if not isinstance(applied_provider, str) or not isinstance(applied_model, str):
            raise DshProtocolError("session.selectModel omitted selected route")
        from cc_remote.wrapper.dsh_stream import encode_dsh_model

        self.model = encode_dsh_model(applied_provider, applied_model)
        self.effort = (
            applied_effort
            if isinstance(applied_effort, str) and applied_effort else None
        )
        return selected

    async def set_model(self, model: str) -> str:
        from cc_remote.wrapper.dsh_stream import decode_dsh_model

        provider, native_model = decode_dsh_model(model)
        catalog = await self.refresh_models()
        selected_effort: str | None = None
        groups = catalog.get("groups")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict) or group.get("id") != provider:
                    continue
                models = group.get("models")
                if not isinstance(models, list):
                    continue
                row = next((
                    item for item in models
                    if isinstance(item, dict)
                    and item.get("id") == native_model
                ), None)
                reasoning = row.get("reasoning") if isinstance(row, dict) else None
                efforts = (
                    reasoning.get("efforts")
                    if isinstance(reasoning, dict) else None
                )
                effort_ids = {
                    item.get("id") for item in efforts or ()
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                }
                if self.effort in effort_ids:
                    selected_effort = self.effort
                else:
                    default_effort = (
                        reasoning.get("defaultEffort")
                        if isinstance(reasoning, dict) else None
                    )
                    if isinstance(default_effort, str) and default_effort:
                        selected_effort = default_effort
                break
        await self.select_model(provider, native_model, selected_effort)
        return self.model or model

    async def set_effort(self, effort: str) -> str:
        from cc_remote.wrapper.dsh_stream import decode_dsh_model

        if not self.model:
            await self.refresh_models()
        provider, native_model = decode_dsh_model(self.model or "")
        await self.select_model(provider, native_model, effort)
        return self.effort or effort
