# mcptokens

**172 tokens in your agent's harness.** One tool, `inspect`, that
counts the tool-definition cost of any other MCP server before you
enable it.

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

## How

Add `mcptokens` to your agent's MCP config (Claude Code, Pi,
OpenCode, Codex, ...). The agent gains one tool:

```jsonc
{
  "name": "inspect",
  "description": "Count tool-def tokens of any MCP server. Pass argv [...]",
  "inputSchema": {
    "properties": {
      "command":  {"type": "array",  "items": {"type": "string"}},
      "encoding": {"type": "string", "enum": ["cl100k_base","o200k_base"], "default": "cl100k_base"},
      "timeout":  {"type": "number", "default": 15, "minimum": 1, "maximum": 60}
    },
    "required": ["command"]
  }
}
```

Then the agent calls:

```python
inspect(command=["hound"])                      # 1023 wire tokens, 8 tools
inspect(command=["python", "-m", "my_server"])  # ...
```

The agent decides whether to enable the candidate server based on
the answer.

## The numbers

| | |
|--|--|
| **Self-cost on wire** | **172 tokens** of `cl100k_base` |
| Tools exposed         | **1**, named `inspect`           |
| Cross-platform        | Linux, macOS, Windows            |
| Python                | 3.11+                            |
| Imports               | `tiktoken`, `mcp`                |
| Source                | 5 files, ~700 LOC                |
| Tests                 | 31 passing in under 10 seconds   |

## Install

```bash
pip install mcptokens
```

Add `mcptokens` to your agent's `mcpServers` / `mcp_servers`. Done.

## CLI (debug surface)

```bash
mcptokens hound                       # human table
mcptokens --json hound                # pipeline-friendly JSON
mcptokens --timeout 30 python -m srv  # custom spawn
mcptokens serve                       # run as an MCP server
mcptokens --version                   # 0.1.0
```

| Flag          | Default        | Purpose                                       |
|---------------|----------------|-----------------------------------------------|
| `--encoding`  | `cl100k_base`  | `cl100k_base` or `o200k_base`                 |
| `--timeout`   | `15`           | Per-server timeout in seconds (1 to 60)       |
| `--json`      | `false`        | Output JSON instead of the table              |

## Why it's cheap

One server, one tool, tight description, minimal schema. The
shipped tool definition tokenizes to **172 tokens**. A small tool
list is the product.

## Tests

```bash
python -m pytest tests/  # 31 passing in under 10 seconds
```

Engine tests cover JSON-RPC id-matching, null-safe shape coercion,
timeouts, and end-to-end via `mcp.ClientSession`.

## License

MIT. `pip install mcptokens` from the canonical PyPI index.
