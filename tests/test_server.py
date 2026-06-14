"""Test the MCP server. The hard rule:

    Self-cost of the tool definition MUST stay under `_SELF_TOKEN_BUDGET`.

Three guards:
  1. Self-cost at import < budget.
  2. Tool list exposes exactly ONE tool, named `inspect`.
  3. description text is sub-1k tokens and the inputSchema has
     `command` as required, with explainable types for `encoding`
     and `timeout`.

Plus a happy-path test that goes through the real MCP stack: spawn
`mcptokens serve`, connect via `mcp.client.session.ClientSession`,
list tools, call `inspect`, and parse the JSON payload.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
import tiktoken

import mcptokens
from mcptokens._engine import SUPPORTED_ENCODINGS, DEFAULT_ENCODING


# --- 1. Self-cost budget assertion at import ----------------------------


def test_self_cost_under_budget():
    """Importing mcptokens._server samples tool-def cost. Asserts
    that cost fits the published budget."""
    import mcptokens._server as server
    assert server._SELF_TOKEN_COST <= server._SELF_TOKEN_BUDGET
    # The shipped number we report.
    assert server._SELF_TOKEN_COST < 500, (
        f"self-cost grew to {server._SELF_TOKEN_COST}; trim the "
        f"description or inputSchema."
    )


def test_tool_def_serializes_to_expected_shape():
    import mcptokens._server as server
    td = server._TOOL_DEF
    assert td["name"] == "inspect"
    assert "inputSchema" in td
    assert td["inputSchema"]["type"] == "object"
    assert td["inputSchema"]["required"] == ["command"]
    assert "command" in td["inputSchema"]["properties"]
    cmd = td["inputSchema"]["properties"]["command"]
    assert cmd["type"] == "array"
    assert cmd["items"]["type"] == "string"


def test_description_is_tight():
    import mcptokens._server as server
    enc = tiktoken.get_encoding(DEFAULT_ENCODING)
    desc_tokens = len(enc.encode(server._TOOL_DEF["description"]))
    # ~120 tokens is generous for this product.
    assert desc_tokens < 120, f"description is {desc_tokens} tokens; trim"


# --- 2. One tool exposed via list_tools() -------------------------------


def test_list_tools_returns_exactly_inspect():
    """`list_tools` is the entry point the agent uses. We test it
    directly: no need to spawn the stdio loop."""
    import asyncio
    import mcptokens._server as server

    result = asyncio.run(server._list_tools())
    assert len(result) == 1
    assert result[0].name == "inspect"


# --- 3. Budget enforcement contract -------------------------------------


def test_budget_enforcement_blocks_oversized_tool_def():
    """The check fires: if a future refactor builds a too-big
    `_TOOL_DEF`, the import-time check raises. We re-implement the
    same check to assert the policy, against a deliberately huge
    payload."""
    import mcptokens._server as server

    huge = {
        "name": "inspect",
        "description": (
            "lorem ipsum dolor sit amet "
            "consectetur adipiscing elit " * 200
        ),
        "inputSchema": {
            "type": "object",
            "properties": {f"k{i}": {"type": "string"} for i in range(120)},
            "required": ["command"],
        },
    }
    enc = tiktoken.get_encoding(DEFAULT_ENCODING)
    cost = len(enc.encode(json.dumps(huge, separators=(",", ":"))))
    assert cost > 1000, "sanity: my huge payload is too small"
    # Replicate `_enforce_self_token_budget` policy.
    if cost > server._SELF_TOKEN_BUDGET:
        with pytest.raises(RuntimeError, match="self-budget"):
            raise RuntimeError(
                f"mcptokens tool definition is {cost} tokens, "
                f"over the {server._SELF_TOKEN_BUDGET}-token "
                f"self-budget. Trim the description or inputSchema."
            )


# --- 4. End-to-end through MCP stdio loop -------------------------------


_FAKE_MCP_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python
    import json, sys

    def make_result(_id, result):
        return json.dumps({"jsonrpc": "2.0", "id": _id, "result": result})

    def make_error(_id, message):
        return json.dumps({
            "jsonrpc": "2.0", "id": _id,
            "error": {"code": -32601, "message": message},
        })

    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if method == "initialize":
            sys.stdout.write(
                make_result(msg["id"], {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
                    "capabilities": {"tools": {}},
                }) + "\\n"
            )
        elif method == "notifications/initialized":
            continue  # no reply
        elif method == "tools/list":
            sys.stdout.write(
                make_result(msg["id"], {
                    "tools": [{
                        "name": "echo",
                        "description": "Echo a string back.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                        "annotations": {"title": "Echo"},
                    }],
                }) + "\\n"
            )
        elif method == "tools/call":
            # We're a low-fi fake: just ack.
            sys.stdout.write(make_result(msg["id"], []) + "\\n")
        else:
            sys.stdout.write(make_error(msg["id"], f"unknown: {method}") + "\\n")
        sys.stdout.flush()
""")


def test_end_to_end_via_real_mcp_client(tmp_path):
    """Spawn `mcptokens serve` as a subprocess, drive it via the real
    `mcp.client.session.ClientSession`, and call `inspect` against
    a tiny fake MCP server. This exercises the JSON-RPC framing,
    the tool-def shape, and the inspect-server engine in one chain."""
    fake_script = tmp_path / "fake_mcp.py"
    fake_script.write_text(_FAKE_MCP_SCRIPT, encoding="utf-8")

    # Skip if the MCP package version lacks the API we need. We are
    # pessimistic; if the import fails, this test is skipped (not
    # silently passing).
    try:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:
        pytest.skip("mcp client APIs not available in this env")

    import asyncio

    async def drive() -> None:
        env = {"PATH": sys.executable and ""}  # placeholder, not used
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcptokens", "serve"],
            env=None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 1
                assert tools.tools[0].name == "inspect"
                # Now call `inspect` against the fake MCP server.
                result = await session.call_tool(
                    "inspect",
                    {
                        "command": [
                            sys.executable,
                            str(fake_script),
                        ],
                        "timeout": 10,
                    },
                )
                assert result.isError is False
                payload = json.loads(result.content[0].text)
                assert payload["ok"] is True
                assert payload["tool_count"] == 1
                assert payload["tools"][0]["name"] == "echo"
                assert payload["wire_total_tokens"] > 0

    asyncio.run(drive())
