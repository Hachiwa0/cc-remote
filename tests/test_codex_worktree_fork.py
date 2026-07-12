from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cc_remote.protocol import (
    ForkSessionWorktree,
    SessionForked,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_worktrees import WorktreeSpec
from tests.test_multisession import _mk_ctx, _mk_machine


def _ctx(state: str = "idle"):
    ctx = _mk_ctx("parent", "parent")
    ctx.engine = "codex"
    ctx.state = state
    ctx.cwd = "/repo/component"
    ctx.sdk = SimpleNamespace(model="gpt-test")
    return ctx


def _spec(*, created: bool = True) -> WorktreeSpec:
    return WorktreeSpec(
        repository_root="/repo",
        worktree_root="/state/worktrees/repo/fork-1",
        cwd="/state/worktrees/repo/fork-1/component",
        branch="cc-remote/fork-1",
        created=created,
        branch_created=created,
    )


def _command(**overrides):
    values = {
        "session_id": "parent",
        "request_id": "request-1",
        "name": "Feature fork",
        "client_id": "client-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_worktree_fork_protocol_roundtrips_as_control_messages():
    command = deserialize(serialize(ForkSessionWorktree(
        session_id="parent", request_id="request-1", name="feature")))
    assert command.type == "fork_session_worktree"
    assert command.session_id == "parent" and command.name == "feature"

    event = deserialize(serialize(SessionForked(
        parent_session_id="parent",
        session_id="forked",
        cwd="/tmp/forked",
        git_branch="cc-remote/feature",
        request_id="request-1",
    )))
    assert event.type == "session_forked" and event.session_id == "forked"
    assert is_downstream(event) is False


def test_codex_worktree_fork_uses_persistent_rpc_and_returns_correlated_result(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            if method == "thread/list":
                return {"data": []}
            if method == "thread/fork":
                return {"thread": {"id": "forked-thread"}}
            if method == "thread/name/set":
                return {}
            raise AssertionError(method)

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "prepare_worktree", lambda *_args: _spec())
        monkeypatch.setattr(machine_module, "codex_session_settings", lambda _sid: {})

        result = await machine._handle_fork_session_worktree(_command())

        fork = next(call for call in calls if call[0] == "thread/fork")
        assert fork[1] == {
            "threadId": "parent",
            "cwd": "/state/worktrees/repo/fork-1/component",
            "ephemeral": False,
            "model": "gpt-test",
        }
        assert ("thread/name/set", {
            "threadId": "forked-thread", "name": "Feature fork",
        }, "/state/worktrees/repo/fork-1/component") in calls
        assert result.type == "session_forked"
        assert result.session_id == "forked-thread"
        assert result.request_id == "request-1"
        assert result.to == "client-1"
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_codex_worktree_fork_recovers_existing_thread_without_refork(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append(method)
            if method == "thread/list":
                return {"data": [{
                    "id": "existing-fork",
                    # app-server 0.144.1 omits this field from thread/list even
                    # though thread/read returns it.
                    "forkedFromId": None,
                    "cwd": "/state/worktrees/repo/fork-1/component",
                }] if params["archived"] is False else []}
            if method == "thread/read":
                return {"thread": {
                    "id": "existing-fork",
                    "forkedFromId": "parent",
                    "cwd": "/state/worktrees/repo/fork-1/component",
                }}
            if method == "thread/name/set":
                return {}
            raise AssertionError("thread/fork must not run during recovery")

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: _spec(created=False))

        result = await machine._handle_fork_session_worktree(_command())

        assert result.session_id == "existing-fork"
        assert "thread/fork" not in calls

    asyncio.run(run())


def test_codex_worktree_fork_rolls_back_fresh_worktree_on_confirmed_failure(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        rolled_back = []

        async def rpc(method, params, cwd=None):
            if method == "thread/list":
                return {"data": []}
            if method == "thread/fork":
                raise RuntimeError("fork rejected")
            raise AssertionError(method)

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "prepare_worktree", lambda *_args: _spec())
        monkeypatch.setattr(machine_module, "codex_session_settings", lambda _sid: {})
        monkeypatch.setattr(
            machine_module, "rollback_worktree", lambda spec: rolled_back.append(spec))

        result = await machine._handle_fork_session_worktree(_command())

        assert result.type == "error"
        assert result.request_id == "request-1"
        assert "派生失败" in result.message
        assert rolled_back == [_spec()]
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_codex_worktree_fork_rejects_running_parent_before_git(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx("running")}
        prepared = []

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: prepared.append(True))

        result = await machine._handle_fork_session_worktree(_command())

        assert result.type == "error" and result.code == "busy"
        assert prepared == []

    asyncio.run(run())
