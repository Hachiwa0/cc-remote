"""ClaudeSDKClient lifecycle: connect / query / interrupt / receive / resume.

Isolates the one version-sensitive call site (`include_partial_messages` on
ClaudeAgentOptions) so an SDK upgrade touches only this file. The wrapper does
NOT set ANTHROPIC_BASE_URL or setting_sources — it wants ~/.claude/settings.json
loaded so cc inherits the model link (127.0.0.1:19191 -> z.AI GLM), the model
id, and bypassPermissions.
"""
from __future__ import annotations

import shutil

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, __version__ as SDK_VERSION
from mcp.server import Server

from cc_remote.config import WrapperConfig
from cc_remote.log import logger

log = logger("cc_remote.wrapper.sdk")

REQUIRED_SDK = (0, 2)  # 0.2.x; the interrupt/drain contract is version-sensitive


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

    @staticmethod
    def preflight() -> None:
        if not shutil.which("claude"):
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
            cwd=cwd or self.cfg.cc_cwd,           # dynamic: must match the resumed session's cwd
            resume=resume_id or None,
            # fork_session=True resumes `resume_id`'s context but writes new turns to
            # a FRESH session id, leaving the original transcript untouched — used for
            # ephemeral /btw side-forks.
            fork_session=fork,
            effort=self.effort,                   # reasoning strength; None -> CLI default (high)
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
        log.warning("cc stderr: " + line.rstrip())

    async def connect(self, resume_id: str | None = None, cwd: str | None = None,
                      fork: bool = False) -> None:
        opts = self._options(resume_id, cwd, fork=fork)
        self.client = ClaudeSDKClient(options=opts)
        await self.client.connect()
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

    def receive_response(self):
        assert self.client is not None
        return self.client.receive_response()

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
