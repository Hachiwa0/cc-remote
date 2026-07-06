"""In-process MCP server exposing the `ask_user` tool.

The agent calls `ask_user(question, options)` when it needs a clarifying
choice from the user (plan mode, ambiguous requests, etc.). The SDK routes
the `tools/call` to this in-process server (registered via
`ClaudeAgentOptions.mcp_servers={"cc-remote-ask":{"type":"sdk","instance":...}}`).
Our handler blocks on a callback provided by WrapperMachine, which emits an
`AskUser` wire event to the client and awaits a Future keyed by `ask_id`.
The client renders a question card; the user's pick comes back as an
`AnswerQuestion` command, which resolves the Future. The handler returns the
answer as the tool_result, and the SDK resumes the turn — mid-turn pause,
identical UX to cc's interactive AskUserQuestion (which is TTY-only and thus
unavailable in SDK mode).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from mcp import types
from mcp.server import Server

ASK_USER_TOOL = "ask_user"

# Signature: async (question, options) -> answer string.
# WrapperMachine provides this; it emits AskUser + awaits the client's answer.
AskCallback = Callable[[str, list[dict[str, str]]], Awaitable[str]]


def make_ask_server(ask: AskCallback) -> Server:
    """Build an in-process MCP server exposing the `ask_user` tool."""
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
                        "question": {
                            "type": "string",
                            "description": "The question to ask the user.",
                        },
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
            )
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]):
        if name != ASK_USER_TOOL:
            return [types.TextContent(type="text", text=f"unknown tool: {name}")]
        question = str(arguments.get("question", ""))
        opts = arguments.get("options") or []
        # Normalize: keep only label + ds, coerce to str.
        options = [
            {"label": str(o.get("label", "")), **({"ds": str(o["ds"])} if o.get("ds") else {})}
            for o in opts if isinstance(o, dict) and o.get("label")
        ]
        answer = await ask(question, options)
        return [types.TextContent(type="text", text=answer or "(no answer)")]

    return server
