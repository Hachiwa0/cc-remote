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

from cc_remote.config import WrapperConfig
from cc_remote.log import logger

log = logger("cc_remote.wrapper.sdk")

REQUIRED_SDK = (0, 2)  # 0.2.x; the interrupt/drain contract is version-sensitive


class SdkHandle:
    def __init__(self, cfg: WrapperConfig):
        self.cfg = cfg
        self.client: ClaudeSDKClient | None = None

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

    def _options(self, resume_id: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            include_partial_messages=True,        # StreamEvent with content_block_delta
            permission_mode="bypassPermissions",  # unattended; matches settings.json
            cwd=self.cfg.cc_cwd,
            resume=resume_id or None,
            stderr=self._on_stderr,               # surface cc subprocess errors
            # setting_sources left None -> load ~/.claude/settings.json (model link, model id)
        )

    @staticmethod
    def _on_stderr(line: str) -> None:
        log.warning("cc stderr: " + line.rstrip())

    async def connect(self, resume_id: str | None = None) -> None:
        opts = self._options(resume_id)
        self.client = ClaudeSDKClient(options=opts)
        await self.client.connect()
        log.info("sdk connected", resume=bool(resume_id), cwd=self.cfg.cc_cwd, sdk_version=SDK_VERSION)

    async def query(self, prompt: str) -> None:
        assert self.client is not None
        await self.client.query(prompt)

    async def interrupt(self) -> None:
        assert self.client is not None
        await self.client.interrupt()

    def receive_response(self):
        assert self.client is not None
        return self.client.receive_response()

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            finally:
                self.client = None

    async def force_reconnect(self, resume_id: str | None) -> None:
        """Last-resort after a drain timeout: tear down and reconnect with resume."""
        log.warning("force-reconnecting SDK client after drain timeout")
        try:
            await self.disconnect()
        except Exception as e:
            log.warning("disconnect during force-reconnect failed", error=str(e))
        await self.connect(resume_id=resume_id)
