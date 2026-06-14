# Changelog

All notable changes to mcptokens are documented in this file.

## [0.1.0] — 2026-06-14

First release. `mcptokens` is an ultra-light MCP server for inspecting
tool-definition token cost. One tool exposed: `inspect`.

The MCP server is the product. Add it to an AI agent harness (Claude
Code, Pi, OpenCode, ...) and the agent can call `inspect` against any
candidate stdio MCP server to know its per-tool tokens and wire total
BEFORE it's enabled. The whole server tokenizes to 172 tokens (under
a 1000-token self-budget enforced at import).

Components:
- `_engine.py` — spawn + JSON-RPC + token count. Cross-platform safe
  (daemon reader thread + queue.Queue).
- `_server.py` — MCP stdio server loop, one tool. Import-time
  `_enforce_self_token_budget()` raises if the tool def ever drifts
  over budget.
- `cli.py` — slim two-verb CLI: `mcptokens <server-argv>...` and
  `mcptokens serve`.

Notes:
- The product review surface is the MCP tool definition itself, not
  the CLI. The CLI is a thin human-side debug hatch.
- The package name `mcptokens` is final. New installs go through
  `pip install mcptokens`.

Self-cost: 172 tokens of `cl100k_base`.
