# mcptokens 0.1.1

Fresh repo, fresh metadata, same product. `mcptokens` is an
ultra-light MCP server whose sole job is to count the
tool-definition cost of any other MCP server before you enable it.

## Install

```bash
pip install mcptokens
```

## At a glance

- **Tokens in your harness:** 172 of `cl100k_base`.
- **Tools exposed:** 1 (`inspect`).
- **Cross-platform:** Linux, macOS, Windows.
- **Python:** 3.11+.

## Use as MCP server

Add `mcptokens` to your agent's MCP config. The agent gains:

```jsonc
{
  "name": "inspect",
  "description": "Count tool-def tokens of any MCP server. ...",
  "inputSchema": {
    "properties": {
      "command":  {"type": "array",  "items": {"type": "string"}},
      "encoding": {"type": "string", "enum": ["cl100k_base","o200k_base"],
                   "default": "cl100k_base"},
      "timeout":  {"type": "number", "default": 15, "minimum": 1, "maximum": 60}
    },
    "required": ["command"]
  }
}
```

## Use as CLI (debug surface)

```bash
mcptokens hound                      # human table
mcptokens --json hound               # pipeline-friendly
mcptokens --timeout 30 python -m srv # custom spawn
mcptokens serve                      # run as an MCP server
```

## Why this exists

When an agent enables a new MCP server, its tools enter the
agent's context on every turn. The cost compounds. `mcptokens`
lets the agent ask *is this server cheap enough?* before flipping
the switch.

## Repository

https://github.com/dondai1234/mcptokens
