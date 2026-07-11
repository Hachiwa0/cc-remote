"""ClaudeSDKClient lifecycle: connect / query / interrupt / receive / resume.

Isolates the one version-sensitive call site (`include_partial_messages` on
ClaudeAgentOptions) so an SDK upgrade touches only this file. The wrapper does
NOT set ANTHROPIC_BASE_URL or setting_sources — it wants ~/.claude/settings.json
loaded so cc inherits the model link (127.0.0.1:19191 -> z.AI GLM), the model
id, and bypassPermissions.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, PermissionResultDeny,
    __version__ as SDK_VERSION,
)
from claude_agent_sdk._internal.message_parser import parse_message as _parse_sdk_message
from claude_agent_sdk.types import ResultMessage, SystemMessage
from mcp.server import Server

from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.wrapper.child_env import child_env_tombstones
from cc_remote.wrapper.claude_goal import (
    NO_GOAL_EVENT,
    active_goal_from_message,
    current_goal,
    goal_message_update,
    make_claude_goal,
    read_claude_goal,
)

log = logger("cc_remote.wrapper.sdk")

REQUIRED_SDK = (0, 2)  # 0.2.x; the interrupt/drain contract is version-sensitive


def _explicit_cli_path(value: str) -> str | None:
    """Normalize an opt-in CLI path without changing blank/PATH behavior."""
    value = value.strip()
    if not value:
        return None
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise RuntimeError("CLAUDE_BIN must be an absolute path")
    return path


class SdkHandle:
    def __init__(self, cfg: WrapperConfig, ask_server: Server | None = None):
        self.cfg = cfg
        self.ask_server = ask_server  # in-process MCP server exposing ask_user
        self.client: ClaudeSDKClient | None = None
        # reasoning effort is a spawn-time flag (--effort), not a runtime setter.
        # `effort` is the desired level; `applied_effort` is what the live client
        # was spawned with — they differ after set_effort until the next reconnect.
        # Default to "max" so new sessions get the strongest reasoning out of the
        # box (matches the client's default chip); the user can lower it per session.
        self.effort: str | None = "max"
        self.applied_effort: str | None = None
        self.permission_callback: Callable[[str, dict[str, Any], Any], Awaitable[Any]] | None = None
        # Claude has no goal RPC.  This cache is reconstructed from native
        # goal_status transcript attachments on resume and updated by the live
        # /goal turn.  It is never persisted separately by cc-remote.
        self.goal: dict[str, Any] | None = None
        self.goal_session_id: str | None = None
        self._goal_message_tokens: dict[str, int] = {}

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any):
        callback = self.permission_callback
        if callback is None:
            log.warning("tool permission requested without client callback", tool=tool_name)
            return PermissionResultDeny(message="remote permission callback unavailable")
        return await callback(tool_name, tool_input, context)

    @staticmethod
    def preflight(cli_path: str = "") -> None:
        explicit = _explicit_cli_path(cli_path)
        if explicit:
            if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
                raise RuntimeError(
                    f"CLAUDE_BIN is not an executable file: {explicit}"
                )
        elif not shutil.which("claude"):
            raise RuntimeError("'claude' CLI not found on PATH; install Claude Code v2.1.51+")
        try:
            parts = SDK_VERSION.split(".")
            major, minor = int(parts[0]), int(parts[1])
        except Exception:
            raise RuntimeError(f"unparsable claude-agent-sdk version: {SDK_VERSION!r}")
        if (major, minor) != REQUIRED_SDK:
            raise RuntimeError(
                f"claude-agent-sdk {SDK_VERSION} != expected {REQUIRED_SDK[0]}.{REQUIRED_SDK[1]}.x; "
                f"the interrupt/drain contract may have changed — pin 0.2.110 or re-verify."
            )

    def _options(self, resume_id: str | None, cwd: str | None = None,
                 fork: bool = False) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            include_partial_messages=True,        # StreamEvent with content_block_delta
            permission_mode="bypassPermissions",  # unattended; matches settings.json
            can_use_tool=self._can_use_tool,
            cwd=cwd or self.cfg.cc_cwd,           # dynamic: must match the resumed session's cwd
            cli_path=_explicit_cli_path(self.cfg.claude_bin),
            resume=resume_id or None,
            # fork_session=True resumes `resume_id`'s context but writes new turns to
            # a FRESH session id, leaving the original transcript untouched — used for
            # ephemeral /btw side-forks.
            fork_session=fork,
            effort=self.effort,                   # reasoning strength; None -> CLI default (high)
            # The SDK otherwise copies the wrapper's complete environment into
            # Claude/tool subprocesses. Never expose relay login/bearer secrets.
            env=child_env_tombstones(),
            stderr=self._on_stderr,               # surface cc subprocess errors
            # setting_sources left None -> load ~/.claude/settings.json (model link, model id)
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    "You have two MCP tools on the cc-remote-ask server:\n"
                    "- `ask_user(question, options)`: ask the user a multiple-choice clarifying "
                    "question (instead of plain text). Blocks until they answer.\n"
                    "- `set_mode(mode)`: switch cc's permission mode yourself, in the middle of a "
                    "turn. When the user expresses intent — 'plan first' / 'let's plan this' -> "
                    "set_mode('plan'); 'just do it' / 'go ahead' -> set_mode('bypassPermissions') "
                    "or 'acceptEdits'. The user has no Shift+Tab here, so calling this is how you "
                    "enter plan mode for them.\n"
                    "Modes: default, acceptEdits, plan, auto, bypassPermissions."
                ),
            },
            mcp_servers=(
                {"cc-remote-ask": {"type": "sdk", "name": "cc-remote-ask", "instance": self.ask_server}}
                if self.ask_server is not None else {}
            ),
        )

    @staticmethod
    def _on_stderr(line: str) -> None:
        # Child stderr may contain credentials or a pathological single line.
        # Preserve only its size; the raw text is never useful enough to justify
        # copying it into the control-plane logger.
        log.warning("cc stderr: ***", chars=len(line))

    async def connect(self, resume_id: str | None = None, cwd: str | None = None,
                      fork: bool = False) -> None:
        opts = self._options(resume_id, cwd, fork=fork)
        self.client = ClaudeSDKClient(options=opts)
        await self.client.connect()
        self.goal_session_id = None if fork else resume_id
        self.goal = None
        self._goal_message_tokens.clear()
        if resume_id and not fork:
            await self.refresh_goal(resume_id)
        self.applied_effort = self.effort  # the live subprocess now reflects this effort
        log.info("sdk connected", resume=bool(resume_id), fork=fork, cwd=opts.cwd,
                 effort=self.effort, sdk_version=SDK_VERSION)

    async def query(self, prompt) -> None:
        """Send a request. `prompt` is a string, or an async iterable of user-
        message dicts (used for multimodal input — text + image blocks)."""
        assert self.client is not None
        await self.client.query(prompt)

    async def interrupt(self) -> None:
        assert self.client is not None
        await self.client.interrupt()

    async def set_model(self, model: str) -> None:
        """Switch the model for the live cc subprocess (takes effect next query,
        no reconnect)."""
        assert self.client is not None
        await self.client.set_model(model)
        log.info("model set", model=model)

    async def set_permission_mode(self, mode: str) -> None:
        """Switch the permission mode for the live cc subprocess (runtime, no reconnect)."""
        assert self.client is not None
        await self.client.set_permission_mode(mode)
        log.info("permission mode set", mode=mode)

    async def get_context_usage(self) -> dict:
        """Return the cc session's context window usage (matches CLI /context)."""
        assert self.client is not None
        return await self.client.get_context_usage()

    async def prepare_goal(self, thread_id: str, objective: str) -> dict[str, Any]:
        """Install the immediate state for a soon-to-be-submitted native /goal.

        The machine submits the actual ``/goal <condition>`` through the normal
        turn path immediately after this call.  Capturing context usage first
        preserves the same baseline that Claude's in-memory activeGoal tracks.
        """
        tokens_at_start = None
        try:
            usage = await self.get_context_usage()
            value = usage.get("totalTokens") if isinstance(usage, dict) else None
            if isinstance(value, int) and not isinstance(value, bool):
                tokens_at_start = max(0, value)
        except Exception as exc:
            # Goal execution must not fail just because the optional baseline
            # control request is unavailable on an older CLI.
            log.warning("goal context baseline unavailable", error=str(exc))
        self.goal_session_id = thread_id
        self.goal = make_claude_goal(
            thread_id, objective, tokens_at_start=tokens_at_start)
        self._goal_message_tokens.clear()
        return current_goal(self.goal)

    def clear_goal_state(self) -> None:
        self.goal = None
        self._goal_message_tokens.clear()

    def restore_goal_state(self, goal: dict[str, Any] | None) -> None:
        self.goal = dict(goal) if goal is not None else None
        self.goal_session_id = (
            self.goal.get("threadId") if self.goal is not None else self.goal_session_id)
        self._goal_message_tokens.clear()

    def rekey_goal(self, session_id: str) -> None:
        self.goal_session_id = session_id
        if self.goal is not None:
            self.goal["threadId"] = session_id

    async def refresh_goal(self, session_id: str | None = None):
        """Refresh cached state from Claude's native transcript if it exists."""
        target = session_id or self.goal_session_id
        if target:
            exists, goal = await asyncio.to_thread(read_claude_goal, target)
            if exists:
                self.goal = goal
                self.goal_session_id = target
                self._goal_message_tokens.clear()
        return current_goal(self.goal)

    def observe_goal_message(self, msg: Any, thread_id: str):
        """Apply a native active_goal event or live Stop-hook feedback."""
        update = active_goal_from_message(msg, thread_id)
        if update is not NO_GOAL_EVENT:
            self.goal = update
            self.goal_session_id = thread_id
            self._goal_message_tokens.clear()
            return True, current_goal(self.goal)
        changed = goal_message_update(
            msg, self.goal, self._goal_message_tokens)
        return changed, current_goal(self.goal)

    async def _receive_response_compat(self):
        """Preserve raw active_goal frames dropped by SDK <= 0.2.116.

        The SDK's public ``receive_response`` parses messages only after reading
        its private Query queue.  Its forward-compatible parser intentionally
        returns None for unknown top-level types, including ``active_goal``.
        Reading the same queue here and wrapping only that one known schema as a
        SystemMessage is the smallest compatibility layer; all other messages
        still use the SDK parser verbatim.
        """
        assert self.client is not None
        query = getattr(self.client, "_query", None)
        if query is None or not hasattr(query, "receive_messages"):
            async for message in self.client.receive_response():
                yield message
            return
        async for data in query.receive_messages():
            if isinstance(data, dict) and data.get("type") == "active_goal":
                message = SystemMessage(subtype="active_goal", data=data)
            else:
                message = _parse_sdk_message(data)
            if message is None:
                continue
            yield message
            if isinstance(message, ResultMessage):
                return

    def receive_response(self):
        assert self.client is not None
        return self._receive_response_compat()

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None

    async def force_reconnect(self, resume_id: str | None, cwd: str | None = None,
                              reason: str = "drain timeout") -> None:
        """Tear down and reconnect with resume. Used after a drain timeout, and to
        apply a spawn-time option change (e.g. effort) to a live session."""
        log.warning("force-reconnecting SDK client", reason=reason)
        try:
            await self.disconnect()
        except Exception as e:
            log.warning("disconnect during force-reconnect failed", error=str(e))
        await self.connect(resume_id=resume_id, cwd=cwd)
