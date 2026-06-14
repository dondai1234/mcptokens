# mcptokens

**576 tokens in your agent's harness.** One tool, `inspect`, that
counts the tool-definition cost of any other MCP server (stdio or
Streamable HTTP) before you enable it.

```bash
pip install mcptokens
```

<p>
  <a href="https://pypi.org/project/mcptokens/"><img src="https://img.shields.io/pypi/v/mcptokens.svg" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/dondai1234/mcptokens"><img src="https://img.shields.io/badge/Repo-dondai1234%2Fmcptokens-1f6feb.svg" alt="Repo"></a>
</p>

---

## Why

When your agent enables a new MCP server, its tools enter the
agent's context on every turn. The cost compounds fast. `mcptokens`
lets the agent ask *is this server cheap enough?* before flipping
the switch.

## Transports

Same tool, same response shape:

- **stdio** (default): spawn a local process and speak JSON-RPC
  on its stdin/stdout.
- **streamable_http**: POST `initialize` and `tools/list` to a
  remote MCP endpoint per MCP 2025-03-26. Server may reply via
  `application/json` (one message) or `text/event-stream`.

## How

Add `mcptokens` to your agent's MCP config (Claude Code, Pi,
OpenCode, Codex, ...). The agent gains one tool:

```python
inspect(command=["python", "-m", "some_mcp_server"])
# stdio spawn argv, same as your MCP config

inspect(command=["hound"])
# pre-installed binary

inspect(
    transport="streamable_http",
    url="http://localhost:8080/mcp",
    headers={"Authorization": "Bearer ..."},  # optional
)
# remote MCP server

# Returns the same JSON shape every call:
# {ok, server, tool_count, wire_total_tokens,
#  tools: [{name, total}], encoding, elapsed_ms, version}
```

`wire_total_tokens` is the number to report. Use it BEFORE
enabling a candidate server: a large value means don't enable.

## The numbers

| | |
|--|--|
| **Self-cost on wire** | **576 tokens** of `cl100k_base`            |
| Tools exposed         | **1**, named `inspect`                     |
| Transports            | stdio, streamable_http                     |
| Cross-platform        | Linux, macOS, Windows                      |
| Python                | 3.11+                                      |
| Imports               | stdlib, `tiktoken`, `mcp`                  |

## Install

```bash
pip install mcptokens
```

Add `mcptokens` to your agent's `mcpServers` / `mcp_servers`. Done.

## CLI (debug surface)

```bash
mcptokens python -m some_mcp_server     # human table
mcptokens --json python -m some_mcp_server   # pipeline-friendly JSON
mcptokens --timeout 30 python -m srv    # custom spawn, custom timeout
mcptokens serve                         # run as an MCP server
```

| Flag          | Default        | Purpose                                       |
|---------------|----------------|-----------------------------------------------|
| `--encoding`  | `cl100k_base`  | `cl100k_base` or `o200k_base`                 |
| `--timeout`   | `15`           | Per-server timeout in seconds (1 to 60)       |
| `--json`      | `false`        | Output JSON instead of the table              |

## License

MIT. `pip install mcptokens` from the canonical PyPI index.
