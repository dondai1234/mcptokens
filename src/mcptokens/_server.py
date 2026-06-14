"""mcptokens MCP server. One tool: `inspect`.

Hard constraint: the loaded tool definition, serialised as JSON,
MUST tokenize to under `_SELF_TOKEN_BUDGET` tokens of `cl100k_base`.
The `_enforce_self_token_budget()` call below fires at import time
on any payload that drifts above the budget. Failing loud at
import is cheaper than silently shipping a bloated server.

Total cost at the publish target is < 500 tokens.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import tiktoken

try:
    from mcp import server, types
    from mcp.server.stdio import stdio_server
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "mcptokens' MCP server requires the `mcp` package. "
        "Install with `pip install mcp[cli]`."
    ) from exc

from mcptokens import __version__
from mcptokens._engine import (
    DEFAULT_ENCODING,
    DEFAULT_TIMEOUT_SECONDS,
    InspectError,
    SUPPORTED_ENCODINGS,
    inspect_server,
)

_SELF_TOKEN_BUDGET = 1000

_TOOL_DEF: dict[str, Any] = {
    "name": "inspect",
    "description": (
        "Count the tool-definition token cost of any MCP server. "
        "Pass argv (e.g. ['hound'] or ['python','-m','server']); "
        "returns per-tool tokens and a wire total. Call BEFORE "
        "enabling an MCP server to know its cost. This server's "
        "own cost stays under 1k tokens, so shipping it is cheap."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Argv to spawn the stdio MCP server, e.g. "
                    "['python','-m','server'] or ['hound']."
                ),
            },
            "encoding": {
                "type": "string",
                "enum": list(SUPPORTED_ENCODINGS),
                "default": DEFAULT_ENCODING,
            },
            "timeout": {
                "type": "number",
                "default": DEFAULT_TIMEOUT_SECONDS,
                "minimum": 1,
                "maximum": 60,
            },
        },
        "required": ["command"],
    },
}


def _enforce_self_token_budget() -> int:
    """Render `_TOOL_DEF` as it appears on the wire, count tokens,
    and raise if it blew the budget. Returns the measured cost."""
    payload = json.dumps(_TOOL_DEF, separators=(",", ":"))
    enc = tiktoken.get_encoding(DEFAULT_ENCODING)
    cost = len(enc.encode(payload))
    if cost > _SELF_TOKEN_BUDGET:
        raise RuntimeError(
            f"mcptokens tool definition is {cost} tokens, "
            f"over the {_SELF_TOKEN_BUDGET}-token self-budget. "
            f"Trim the description or inputSchema before shipping."
        )
    return cost


_SELF_TOKEN_COST = _enforce_self_token_budget()


_server = server.Server("mcptokens")


@_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [types.Tool(**_TOOL_DEF)]


@_server.call_tool()
async def _call_tool(
    name: str,
    arguments: dict[str, Any],
) -> list[types.TextContent]:
    if name != "inspect":
        raise ValueError(f"mcptokens has no tool named {name!r}")
    command = arguments.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(a, str) for a in command
    ):
        raise ValueError(
            "inspect: argument `command` must be a non-empty list of strings"
        )
    encoding = arguments.get("encoding", DEFAULT_ENCODING)
    timeout = arguments.get("timeout", DEFAULT_TIMEOUT_SECONDS)

    try:
        report = inspect_server(
            command, encoding=encoding, timeout_seconds=float(timeout), version=__version__
        )
    except InspectError as exc:
        # Surface as a JSON-flavoured text content so the agent
        # gets a structured payload it can read in one round.
        payload = {
            "ok": False,
            "server": " ".join(command),
            "error": str(exc),
            "encoding": encoding,
        }
        return [types.TextContent(type="text", text=json.dumps(payload))]

    return [types.TextContent(type="text", text=json.dumps(report.as_dict()))]


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await _server.run(
            read_stream,
            write_stream,
            _server.create_initialization_options(),
        )


def run_server() -> int:
    """Console-script entry. Runs until SIGINT or the parent closes
    stdin. Returns 0 on a clean exit."""
    asyncio.run(_run())
    return 0
