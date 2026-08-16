from __future__ import annotations

import json

import httpx
import pytest

from cc_remote.wrapper.dsh_client import (
    DshClient,
    DshProtocolError,
    DshRpcError,
    DshSessionHandle,
    DshUnavailable,
    normalize_dsh_url,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:3080", "http://127.0.0.1:3080"),
        ("http://localhost:3080/", "http://localhost:3080"),
        ("http://[::1]:3080", "http://[::1]:3080"),
    ],
)
def test_normalize_dsh_url_accepts_only_canonical_loopback(raw, expected):
    assert normalize_dsh_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://127.0.0.1:3080",
        "http://192.168.1.3:3080",
        "http://example.com:3080",
        "http://user:pass@127.0.0.1:3080",
        "http://127.0.0.1:3080/api",
        "http://127.0.0.1:3080?token=secret",
    ],
)
def test_normalize_dsh_url_rejects_non_loopback_or_secret_shapes(raw):
    with pytest.raises(ValueError):
        normalize_dsh_url(raw)


async def _mocked_client(handler) -> DshClient:
    client = DshClient("http://127.0.0.1:3080")
    client._http = httpx.AsyncClient(
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    return client


def test_client_allocates_http_transport_lazily_and_validates_timeout():
    client = DshClient("http://127.0.0.1:3080")
    assert client._http is None
    with pytest.raises(ValueError, match="between 0.1 and 60"):
        DshClient("http://127.0.0.1:3080", timeout=float("inf"))


def test_rpc_allowlist_excludes_dsh_configuration_and_subagent_mutations():
    client = DshClient("http://127.0.0.1:3080")
    for method in (
        "agentPreset.select",
        "goal.create",
        "subagent.prompt",
        "host.listDirectory",
    ):
        assert method not in client.ALLOWED_METHODS
    assert {"commands/list", "commands/execute"}.issubset(
        client.ALLOWED_METHODS
    )


@pytest.mark.asyncio
async def test_call_correlates_rpc_id_and_returns_value():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/api/session.list"
        assert body == {
            "type": "client-request",
            "rpcId": "browser-msg-1",
            "method": "session.list",
            "payload": {},
        }
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": {"items": []}},
        })

    client = await _mocked_client(handler)
    try:
        assert await client.call(
            "session.list", rpc_id="browser-msg-1"
        ) == {"items": []}
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_call_rejects_rpc_id_mismatch():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": "different",
            "result": {"ok": True, "value": {}},
        })

    client = await _mocked_client(handler)
    try:
        with pytest.raises(DshProtocolError, match="rpcId mismatch"):
            await client.call("session.list", rpc_id="expected")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_call_surfaces_structured_business_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": False, "error": {
                "code": "session-not-found",
                "message": "missing",
                "details": {"sessionId": "x"},
            }},
        })

    client = await _mocked_client(handler)
    try:
        with pytest.raises(DshRpcError) as captured:
            await client.call("session.history", {"sessionId": "x"})
        assert captured.value.code == "session-not-found"
        assert captured.value.details == {"sessionId": "x"}
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_probe_requires_minimum_host_session_and_preset_contract():
    values = {
        "host.describe": {
            "version": "0.0.1",
            "cwd": "/tmp/project",
            "provider": "deepseek-official",
            "model": "deepseek-v4-flash",
            "attachedSessions": 0,
            "canOpenPath": True,
        },
        "session.list": {"items": []},
        "agentPreset.list": {"presets": [{
            "id": "code",
            "trust": "system",
            "isDefault": True,
            "name": "Code",
        }], "authorable": True, "hasDocument": True},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": values[body["method"]]},
        })

    client = await _mocked_client(handler)
    try:
        result = await client.probe()
        assert result.default_preset_id == "code"
        assert result.host["model"] == "deepseek-v4-flash"
        assert not hasattr(result, "sessions")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_probe_bounds_retained_presets_and_keeps_a_late_default():
    presets = [{
        "id": f"preset-{index}",
        "trust": "user",
        "isDefault": index == 139,
        "name": "n" * 400,
        "description": "d" * 5000,
    } for index in range(140)]
    values = {
        "host.describe": {
            "version": "0.1.0",
            "cwd": "/tmp/project",
            "attachedSessions": 140,
            "canOpenPath": True,
        },
        "session.list": {"items": [{"unused": "x" * 1024}]},
        "agentPreset.list": {
            "presets": presets,
            "authorable": True,
            "hasDocument": True,
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "type": "server-response",
            "rpcId": body["rpcId"],
            "result": {"ok": True, "value": values[body["method"]]},
        })

    client = await _mocked_client(handler)
    try:
        result = await client.probe()
        assert len(result.presets) == client.PREFLIGHT_PRESET_CAP
        assert result.default_preset_id == "preset-139"
        assert result.presets[-1]["id"] == "preset-139"
        assert len(result.presets[-1]["name"]) == 256
        assert len(result.presets[-1]["description"]) == 4096
    finally:
        await client.stop()


def test_server_request_validation_requires_method_payload_match():
    with pytest.raises(DshProtocolError):
        DshClient._validate_server_request({
            "type": "server-request",
            "rpcId": "push-1",
            "method": "session/event",
            "payload": {"type": "host/session-status"},
        })


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"accepted": False},
        {"accepted": True, "command": "compact"},
        {"accepted": True, "command": {"kind": "failure"}},
        {"accepted": True, "command": {"kind": "success", "text": 7}},
    ],
)
async def test_session_prompt_rejects_malformed_receipts(value):
    class StubClient:
        async def call(self, *_args, **_kwargs):
            return value

    handle = DshSessionHandle(StubClient(), "native-session", "/tmp")
    with pytest.raises(DshProtocolError, match="session.prompt"):
        await handle.prompt("hello", rpc_id="query-1")


@pytest.mark.asyncio
async def test_session_prompt_accepts_valid_command_receipt():
    class StubClient:
        async def call(self, *_args, **_kwargs):
            return {
                "accepted": True,
                "command": {"kind": "success", "text": "Compacted"},
            }

    handle = DshSessionHandle(StubClient(), "native-session", "/tmp")
    assert await handle.prompt("/compact", rpc_id="query-1") == {
        "accepted": True,
        "command": {"kind": "success", "text": "Compacted"},
    }


@pytest.mark.asyncio
async def test_effective_command_resolution_and_execution_use_typert_gateway():
    class StubClient:
        def __init__(self):
            self.calls = []

        async def call(self, method, payload, **kwargs):
            self.calls.append((method, payload, kwargs))
            if method == "commands/list":
                return [{
                    "name": "compact",
                    "description": "Compact context",
                    "input": {"hint": "optional focus"},
                }]
            assert method == "commands/execute"
            return {
                "commandId": "native-command-1",
                "result": {
                    "kind": "success",
                    "text": "Compacted",
                    "sourceEventSeq": 41,
                },
            }

    client = StubClient()
    handle = DshSessionHandle(client, "native-session", "/tmp")

    descriptor = await handle.resolve_command("/compact now")
    assert descriptor is not None
    assert descriptor.name == "compact"
    execution = await handle.execute_command(
        "/compact now", rpc_id="remote-command",
    )

    assert execution.command_id == "native-command-1"
    assert execution.source_event_seq == 41
    assert client.calls == [
        (
            "commands/list",
            {"args": {"agentId": "native-session"}},
            {},
        ),
        (
            "commands/execute",
            {"args": {
                "agentId": "native-session",
                "line": "/compact now",
            }},
            {"rpc_id": "remote-command", "no_timeout": True},
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    ["/ compact", "/Compact", "/compact.foo", "/1compact"],
)
async def test_command_resolution_matches_dsh_parser_exactly(line):
    class StubClient:
        async def call(self, *_args, **_kwargs):
            raise AssertionError("invalid command syntax must not hit DSH")

    handle = DshSessionHandle(StubClient(), "native-session", "/tmp")
    assert await handle.resolve_command(line) is None


@pytest.mark.asyncio
async def test_command_execution_requires_error_text():
    class StubClient:
        async def call(self, *_args, **_kwargs):
            return {
                "commandId": "native-command-1",
                "result": {"kind": "error"},
            }

    handle = DshSessionHandle(StubClient(), "native-session", "/tmp")
    with pytest.raises(DshProtocolError, match="invalid execution"):
        await handle.execute_command("/compact", rpc_id="remote-command")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, {}, {"accepted": False}])
async def test_session_cancel_rejects_malformed_receipts(value):
    class StubClient:
        async def call(self, *_args, **_kwargs):
            return value

    handle = DshSessionHandle(StubClient(), "native-session", "/tmp")
    with pytest.raises(DshProtocolError, match="session.cancel"):
        await handle.cancel()


@pytest.mark.asyncio
async def test_model_selection_reconciles_a_lost_success_receipt():
    class StubClient:
        def __init__(self):
            self.calls = 0

        async def call(self, method, *_args, **_kwargs):
            self.calls += 1
            if method == "session.selectModel":
                raise DshUnavailable("receipt lost")
            assert method == "session.models"
            return {
                "current": {
                    "provider": "deepseek-official",
                    "model": "deepseek-v4-flash",
                    "reasoningEffort": "high",
                }
            }

    client = StubClient()
    handle = DshSessionHandle(client, "native-session", "/tmp")
    selected = await handle.select_model(
        "deepseek-official", "deepseek-v4-flash", "high"
    )

    assert selected["reasoningEffort"] == "high"
    assert handle.model == "dsh://deepseek-official/deepseek-v4-flash"
    assert handle.effort == "high"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_model_switch_drops_an_effort_the_target_does_not_support():
    class StubClient:
        def __init__(self):
            self.selected_payload = None

        async def call(self, method, payload, **_kwargs):
            if method == "session.models":
                return {
                    "current": {
                        "provider": "provider-a",
                        "model": "reasoning-model",
                        "reasoningEffort": "high",
                    },
                    "groups": [{
                        "id": "provider-a",
                        "models": [{
                            "id": "fast-model",
                            "reasoning": {
                                "efforts": [{"id": "low"}],
                                "defaultEffort": "low",
                            },
                        }],
                    }],
                }
            assert method == "session.selectModel"
            self.selected_payload = payload
            return {"selected": {
                "provider": "provider-a",
                "model": "fast-model",
                "reasoningEffort": "low",
            }}

    client = StubClient()
    handle = DshSessionHandle(
        client,
        "native-session",
        "/tmp",
        model="dsh://provider-a/reasoning-model",
        effort="high",
    )

    await handle.set_model("dsh://provider-a/fast-model")

    assert client.selected_payload["reasoningEffort"] == "low"
    assert handle.effort == "low"


def test_session_projections_are_bounded_and_newer_sequence_wins():
    handle = DshSessionHandle(object(), "native-session", "/tmp")
    assert handle.apply_projections({
        "permissions": {
            "options": [
                {"value": "workspace-write", "name": "workspace-write"},
                {"value": "custom", "name": "Custom"},
            ],
            "currentValue": "custom",
        },
        "contextPressure": {
            "pressureTokens": 10,
            "projectedTokens": 12,
            "contextWindow": 100,
        },
    }, 7) == {"permissions", "contextPressure"}
    assert handle.permission_mode == "custom"
    assert [row["id"] for row in handle.permission_options] == [
        "workspace-write",
    ]
    assert handle.permission_options[0]["name"] == "Workspace Write"
    assert handle.context_pressure["projectedTokens"] == 12

    assert handle.apply_projection(
        "contextPressure",
        {"projectedTokens": 3, "contextWindow": 100},
        6,
    ) is False
    assert handle.context_pressure["projectedTokens"] == 12
    with pytest.raises(DshProtocolError, match="conflicts"):
        handle.apply_projection(
            "contextPressure",
            {"projectedTokens": 13, "contextWindow": 100},
            7,
        )
    with pytest.raises(DshProtocolError, match="omitted its current option"):
        handle.apply_projection("permissions", {
            "options": [
                {"value": "workspace-write", "name": "workspace-write"},
            ],
            "currentValue": "missing",
        }, 8)
    with pytest.raises(DshProtocolError, match="invalid sequence"):
        handle.apply_projection(
            "contextPressure",
            {"projectedTokens": 13, "contextWindow": 100},
            9_007_199_254_740_992,
        )


@pytest.mark.asyncio
async def test_permission_switch_reconciles_lost_receipt_without_reexecution():
    class StubClient:
        def __init__(self):
            self.history_reads = 0
            self.command_calls = 0

        async def call(self, method, payload, **_kwargs):
            if method == "session.history":
                self.history_reads += 1
                current = (
                    "danger-full-access"
                    if self.command_calls else "workspace-write"
                )
                return {
                    "events": [],
                    "hasMore": False,
                    "projections": {
                        "asOfSeq": self.history_reads + self.command_calls,
                        "values": {"permissions": {
                            "options": [
                                {"value": "workspace-write", "name": "workspace-write"},
                                {"value": "danger-full-access", "name": "danger-full-access"},
                            ],
                            "currentValue": current,
                        }},
                    },
                }
            assert method == "commands/execute"
            assert payload["args"]["line"] == "/permission danger-full-access"
            self.command_calls += 1
            raise DshUnavailable("receipt lost after commit")

    client = StubClient()
    handle = DshSessionHandle(client, "native-session", "/tmp")

    assert await handle.set_permission_mode(
        "danger-full-access", rpc_id="permission-1",
    ) == "danger-full-access"
    # A reliable retry reads the already-applied projection and returns without
    # executing the non-idempotent native command a second time.
    assert await handle.set_permission_mode(
        "danger-full-access", rpc_id="permission-1",
    ) == "danger-full-access"
    assert client.command_calls == 1
