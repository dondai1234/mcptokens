"""mcptokens: ultra-light MCP server for inspecting tool-def token cost.

Use case: an AI agent (Claude Code, Pi, OpenCode, ...) connects to
mcptokens as one of its MCP servers, then calls the single exposed
tool `inspect` with a candidate server's argv. The agent gets back
per-tool tokens plus a realistic wire total. Use BEFORE enabling
an MCP server so the agent can decide whether the cost is worth it.
"""

__version__ = "1.0.1"
__all__ = ["__version__"]
