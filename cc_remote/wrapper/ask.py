"""In-process MCP server exposing cc-remote's agent-facing tools.

Tools:
- `ask_user(question, options)`: ask the user a multiple-choice clarifying
  question. Blocks until the user answers (client renders a question card).
- `set_mode(mode)`: switch cc's permission mode yourself (e.g. "plan" when the
  user wants to plan, "bypassPermissions" to go back to normal). The mode
  change takes effect immediately for the rest of the turn.

The SDK routes `tools/call` to this in-process server (registered via
`ClaudeAgentOptions.mcp_servers={"cc-remote-ask":{"type":"sdk","instance":...}}`).
Handlers delegate to callbacks provided by WrapperMachine.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp import types
from mcp.server import Server

ASK_USER_TOOL = "ask_user"
SET_MODE_TOOL = "set_mode"
MODES = ["default", "acceptEdits", "plan", "auto", "bypassPermissions"]

# async (question, options) -> answer string
AskCallback = Callable[[str, list[dict[str, str]]], Awaitable[str]]
# async (mode) -> None  (machine: sdk.set_permission_mode + emit Perm)
SetModeCallback = Callable[[str], Awaitable[None]]


def make_ask_server(ask: AskCallback, set_mode: SetModeCallback) -> Server:
    """Build an in-process MCP server exposing the ask_user + set_mode tools."""
    server: Server = Server("cc-remote-ask")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=ASK_USER_TOOL,
                description=(
                    "Ask the user a clarifying question with multiple-choice options. "
                    "Use this in plan mode or whenever you need the user to pick between "
                    "approaches before proceeding. The call blocks until the user answers; "
                    "their selected option's label is returned as the tool result. Do NOT "
                    "ask via plain text when you could use this tool."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question to ask the user."},
                        "options": {
                            "type": "array",
                            "description": "2-5 selectable options.",
                            "minItems": 2,
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "Short option label."},
                                    "ds": {"type": "string", "description": "Optional one-line detail."},
                                },
                                "required": ["label"],
                            },
                        },
                    },
                    "required": ["question", "options"],
                },
            ),
            types.Tool(
                name=SET_MODE_TOOL,
                description=(
                    "Switch cc's permission mode yourself. Use this when the user expresses "
                    "intent — e.g. 'let's plan this' / 'plan first' -> set_mode('plan'); "
                    "'just do it' / 'go ahead and edit' -> set_mode('bypassPermissions') or "
                    "'acceptEdits'. Takes effect immediately for the rest of the turn. "
                    "Modes: 'default' (ask each time), 'acceptEdits' (auto-accept edits), "
                    "'plan' (read-only, plan first), 'auto', 'bypassPermissions' (skip all)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": MODES}},
                    "required": ["mode"],
                },
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]):
        if name == ASK_USER_TOOL:
            question = str(arguments.get("question", ""))
            opts = arguments.get("options") or []
            options = [
                {"label": str(o.get("label", "")), **({"ds": str(o["ds"])} if o.get("ds") else {})}
                for o in opts if isinstance(o, dict) and o.get("label")
            ]
            answer = await ask(question, options)
            return [types.TextContent(type="text", text=answer or "(no answer)")]
        if name == SET_MODE_TOOL:
            mode = str(arguments.get("mode", ""))
            if mode not in MODES:
                return [types.TextContent(type="text", text=f"unknown mode: {mode}; valid: {MODES}")]
            await set_mode(mode)
            return [types.TextContent(type="text", text=f"permission mode set to {mode}")]
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]

    return server
