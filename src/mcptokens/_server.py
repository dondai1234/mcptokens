"""mcptokens MCP server. One tool: `inspect`.

Hard constraint: the loaded tool definition, serialised as JSON,
MUST tokenize to under `_SELF_TOKEN_BUDGET` tokens of `cl100k_base`.
The `_enforce_self_token_budget()` call below fires at import time
on any payload that drifts above the budget. Failing loud at
import is cheaper than silently shipping a bloated server.

Framing compatibility: the mcp SDK's `stdio_server` only knows
line-delimited JSON on input. When a parent client sends
`Content-Length` framed messages, the SDK logs an
`Internal Server Error` `notifications/message` to stdout before
the real response. OpenCode and other strict clients read that
error and drop the connection (MCP error -32000 `Connection
closed`). We pre-process sys.stdin so the SDK always sees clean
NDJSON, regardless of which framing the parent sends.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from typing import Optional

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
_MAX_MESSAGE_BYTES = 100 * 1024 * 1024  # 100MB; protects against runaways

_TOOL_DEF: dict = {
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


_server = server.Server("mcptokens", version=__version__)


@_server.list_tools()
async def _list_tools() -> list:
    return [types.Tool(**_TOOL_DEF)]


@_server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list:
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
        payload = {
            "ok": False,
            "server": " ".join(command),
            "error": str(exc),
            "encoding": encoding,
        }
        return [types.TextContent(type="text", text=json.dumps(payload))]

    return [types.TextContent(type="text", text=json.dumps(report.as_dict()))]


# --- framing-compatible stdin adapter ----------------------------------


class _FramedNDJSONStream(io.TextIOBase):
    """Reads from a real stdin pipe and presents one NDJSON line per
    MCP message, regardless of how the parent framed the message.

    Two input modes are accepted:
    - NDJSON: each line is a complete JSON-RPC message.
    - Content-Length framed: `Content-Length: N\\r\\n\\r\\n[N bytes]`,
      where `\\r\\n\\r\\n` (or `\\n\\n`) separates headers from body.

    Output: each complete message is yielded as a single line
    terminated by `\\n` (without the line terminator on return;
    `readline()` strips the `\\n`, like every other text-mode file).

    Why: the mcp SDK 1.27 `stdio_server` line-iterates stdin and
    parses each line as a JSON-RPC message. With NDJSON parents,
    that works. With Content-Length parents (OpenCode's framing),
    the SDK treats header lines and empty lines as malformed JSON
    and emits `notifications/message` errors to stdout before the
    real response. Some clients (notably OpenCode) read those
    errors as "server broken" and drop the connection with MCP
    -32000 `Connection closed`. Absorbing both framings here keeps
    the SDK happy and clients stable across the matrix of
    NDJSON-only and Content-Length-capable parents."""

    def __init__(self, real_binary) -> None:
        super().__init__()
        # real_binary: a binary buffered file (e.g. sys.stdin.buffer).
        self._real = real_binary
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readline(self, size: int = -1) -> str:  # type: ignore[override]
        """Pull from underlying until we have one full MCP message
        to return as a line (without trailing `\\n`). Returns the
        decoded UTF-8 string of that line, or empty string on EOF."""
        while True:
            msg_bytes = self._try_extract_one()
            if msg_bytes is not None:
                return msg_bytes.decode("utf-8", errors="replace")
            chunk = self._real.read1(8192)
            if not chunk:
                if not self._buf:
                    return ""
                leftover = self._buf
                self._buf = b""
                return leftover.decode("utf-8", errors="replace")
            self._buf += chunk

    def _try_extract_one(self) -> Optional[bytes]:
        """Try to extract one complete NDJSON frame from self._buf.
        Returns the body bytes (without trailing `\\n`) if a full
        message is buffered; None if more bytes are needed."""
        if not self._buf:
            return None
        # Strip leading whitespace/crlf.
        if self._buf[:1] in (b" ", b"\t", b"\r", b"\n"):
            self._buf = self._buf.lstrip(b" \t\r\n")
            if not self._buf:
                return None
        # Content-Length framed?
        if self._buf.lower().startswith(b"content-length:"):
            sep_crlf = self._buf.find(b"\r\n\r\n")
            sep_lf = self._buf.find(b"\n\n")
            if sep_crlf == -1 and sep_lf == -1:
                return None  # incomplete
            if sep_crlf == -1:
                sep, sep_len = sep_lf, 2
            elif sep_lf == -1:
                sep, sep_len = sep_crlf, 4
            elif sep_crlf <= sep_lf:
                sep, sep_len = sep_crlf, 4
            else:
                sep, sep_len = sep_lf, 2
            headers_str = self._buf[:sep].decode("ascii", errors="replace")
            content_length = -1
            for hl in headers_str.splitlines():
                h = hl.strip()
                if h.lower().startswith("content-length:"):
                    try:
                        content_length = int(h.split(":", 1)[1].strip())
                    except ValueError:
                        content_length = -1
            if content_length < 0 or content_length > _MAX_MESSAGE_BYTES:
                # Unparseable / oversize: skip past the headers and
                # yield an empty line so the SDK discards it cleanly.
                self._buf = self._buf[sep + sep_len:]
                return b""
            body_start = sep + sep_len
            body_end = body_start + content_length
            if len(self._buf) < body_end:
                return None  # body not yet complete
            body = self._buf[body_start:body_end].rstrip(b"\r\n")
            self._buf = self._buf[body_end:]
            return body
        # Assume NDJSON: `{` at start; find first `\n`.
        if self._buf[:1] == b"{":
            idx_nl = self._buf.find(b"\n")
            if idx_nl == -1:
                return None
            line = self._buf[:idx_nl]
            self._buf = self._buf[idx_nl + 1:]
            return line.rstrip(b"\r")
        # Stray byte at start (not JSON, not a header): drop one
        # and try again. MCP messages always start with `{` after
        # the headers; anything else is junk between frames.
        self._buf = self._buf[1:]
        return self._try_extract_one()


async def _run() -> None:
    """Hands the SDK a framing-aware stdin so OpenCode (and any
    other strict MCP client) reads a clean initialize response
    instead of an Internal Server Error notification."""
    import anyio
    framed_stdin = _FramedNDJSONStream(sys.stdin.buffer)
    async_stdin = anyio.wrap_file(framed_stdin)
    async with stdio_server(stdin=async_stdin) as (read_stream, write_stream):
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
