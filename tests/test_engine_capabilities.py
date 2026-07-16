from __future__ import annotations

import asyncio

import pytest

from cc_remote.protocol import (
    ERR_BAD_PROMPT,
    GetEngineCapabilities,
    ManageEnginePlugin,
)
from cc_remote.wrapper import engine_capabilities as capabilities_module
from cc_remote.wrapper import machine as machine_module
from tests.test_multisession import _mk_machine


class _ClaudeProcess:
    def __init__(self, stdout: bytes = b"[]") -> None:
        self.stdout = stdout
        self.returncode = 0
        self.killed = False

    async def communicate(self):
        return self.stdout, b""

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


def test_claude_capability_listing_uses_effective_configured_cli(
    monkeypatch, tmp_path
):
    async def run():
        configured = "/configured/claude"
        effective = "/sdk/runtime/claude"
        resolved = []
        spawned = []

        def resolve(value):
            resolved.append(value)
            return effective, "configured"

        async def spawn(*args, **kwargs):
            spawned.append((args, kwargs))
            return _ClaudeProcess(
                b'[{"id":"example","name":"Example","enabled":true}]'
            )

        monkeypatch.setattr(capabilities_module, "resolve_claude_cli", resolve)
        monkeypatch.setattr(capabilities_module, "_claude_skills", lambda _cwd: [])
        monkeypatch.setattr(
            capabilities_module.asyncio, "create_subprocess_exec", spawn
        )

        items, errors, _ = await capabilities_module.engine_capabilities(
            "claude", str(tmp_path), "code", configured
        )

        assert resolved == [configured]
        assert spawned[0][0][:4] == (
            effective,
            "plugin",
            "list",
            "--json",
        )
        assert [item["id"] for item in items] == ["example"]
        assert errors == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("action", "verb"),
    (("install", "install"), ("uninstall", "uninstall")),
)
def test_claude_plugin_mutation_uses_effective_configured_cli(
    monkeypatch, tmp_path, action, verb
):
    async def run():
        configured = "/configured/claude"
        effective = "/sdk/runtime/claude"
        resolved = []
        spawned = []

        def resolve(value):
            resolved.append(value)
            return effective, "configured"

        async def spawn(*args, **kwargs):
            spawned.append((args, kwargs))
            return _ClaudeProcess()

        monkeypatch.setattr(capabilities_module, "resolve_claude_cli", resolve)
        monkeypatch.setattr(
            capabilities_module.asyncio, "create_subprocess_exec", spawn
        )

        await capabilities_module.manage_engine_plugin(
            "claude",
            "example",
            action,
            str(tmp_path),
            space="code",
            claude_bin=configured,
        )

        assert resolved == [configured]
        assert spawned[0][0][:4] == (
            effective,
            "plugin",
            verb,
            "example",
        )

    asyncio.run(run())


def test_work_plugin_mutation_is_rejected_before_engine_access(
    monkeypatch, tmp_path
):
    async def run():
        monkeypatch.setattr(
            capabilities_module,
            "resolve_claude_cli",
            lambda _configured: pytest.fail("Work mutation must not resolve a CLI"),
        )
        with pytest.raises(ValueError, match="Work"):
            await capabilities_module.manage_engine_plugin(
                "claude",
                "example",
                "install",
                str(tmp_path),
                space="work",
                claude_bin="/configured/claude",
            )

    asyncio.run(run())


def test_machine_forwards_configured_cli_to_capability_listing(
    monkeypatch, tmp_path
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.claude_bin = "/configured/claude"
        seen = []

        async def discover(engine, cwd, space, claude_bin):
            seen.append((engine, cwd, space, claude_bin))
            return [], [], []

        monkeypatch.setattr(machine_module, "engine_capabilities", discover)
        await machine._handle_get_engine_capabilities(
            GetEngineCapabilities(
                engine="claude", cwd=str(tmp_path), client_id="client-1"
            )
        )

        assert seen == [
            ("claude", str(tmp_path), "code", "/configured/claude")
        ]

    asyncio.run(run())


def test_machine_forwards_work_space_to_plugin_backend(monkeypatch, tmp_path):
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.claude_bin = "/configured/claude"
        seen = []

        async def reject(engine, plugin_id, action, cwd, *, space, claude_bin):
            seen.append((engine, plugin_id, action, cwd, space, claude_bin))
            raise ValueError("Work 不允许修改引擎插件")

        monkeypatch.setattr(machine_module, "manage_engine_plugin", reject)
        result = await machine._handle_manage_engine_plugin(
            ManageEnginePlugin(
                engine="claude",
                action="install",
                plugin_id="example",
                space="work",
                cwd=str(tmp_path),
                client_id="client-1",
            )
        )

        assert seen == [
            (
                "claude",
                "example",
                "install",
                str(tmp_path),
                "work",
                "/configured/claude",
            )
        ]
        assert result.code == ERR_BAD_PROMPT
        assert transport.sent[-1] is result

    asyncio.run(run())
