"""mcptokens engine.

Spawn a stdio MCP server, speak JSON-RPC `initialize` + `tools/list`,
count the wire tokens that an LLM agent would receive. Cross-platform
safe (Windows in particular: stdlib has no `os.set_blocking`, and
`selectors.DefaultSelector` raises WinError 10093 unless WSAStartup
has run; we use a daemon reader thread + `queue.Queue` instead).

Public surface:
    inspect_server(cmd, *, encoding="cl100k_base", timeout_seconds=15.0)
        -> InspectReport
    InspectError  (raised on spawn / protocol failures)
    InspectReport, ToolStats   (dataclasses; serialize via .as_dict())

v1.0.0 adds a transport-aware dispatcher, `inspect(req)`, which
accepts either `transport="stdio"` with a spawn argv, or
`transport="streamable_http"` with a URL. Both share the response
shape so the agent learns one tool, not two.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import tiktoken

__all__ = [
    "inspect_server",
    "InspectError",
    "InspectReport",
    "ToolStats",
    "DEFAULT_ENCODING",
    "DEFAULT_TIMEOUT_SECONDS",
]

DEFAULT_ENCODING = "cl100k_base"
DEFAULT_TIMEOUT_SECONDS = 15.0
SUPPORTED_ENCODINGS = ("cl100k_base", "o200k_base")
_INIT_ID = 1
_TOOLS_LIST_ID = 2
_LSP_PROTOCOL_VERSION = "2024-11-05"


class InspectError(Exception):
    """Raised for spawn / protocol / shape failures that the agent
    needs to know about so it can retry or fall back."""


# --- output dataclasses ---------------------------------------------------


@dataclass
class ToolStats:
    name: str
    name_tokens: int = 0
    description_tokens: int = 0
    schema_tokens: int = 0
    annotations_tokens: int = 0
    total_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tokens": {
                "name": self.name_tokens,
                "description": self.description_tokens,
                "schema": self.schema_tokens,
                "annotations": self.annotations_tokens,
                "total": self.total_tokens,
            },
        }


@dataclass
class InspectReport:
    ok: bool
    server: str
    tools: list[ToolStats] = field(default_factory=list)
    wire_total_tokens: int = 0
    encoding: str = DEFAULT_ENCODING
    elapsed_ms: int = 0
    error: str = ""
    version: str = ""

    def as_dict(self, *, verbose: bool = False) -> dict[str, Any]:
        """Serialize the report. `verbose=False` (default) keeps the
        payload compact so the agent can scan many reports in one
        prompt without burning its context on per-tool schema dumps.
        Set `verbose=True` to include the full Recipe A+ breakdown."""
        if verbose:
            tool_payloads = [t.as_dict() for t in self.tools]
        else:
            tool_payloads = [
                {"name": t.name, "tokens": t.total_tokens} for t in self.tools
            ]
        return {
            "ok": self.ok,
            "server": self.server,
            "tool_count": len(self.tools),
            "tools": tool_payloads,
            "wire_total_tokens": self.wire_total_tokens,
            "encoding": self.encoding,
            "elapsed_ms": self.elapsed_ms,
            "version": self.version,
        }


# --- json-rpc helpers -----------------------------------------------------


def _encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def _decode(b: bytes) -> dict[str, Any] | None:
    try:
        obj = json.loads(b.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# --- defensive shape coercion --------------------------------------------


def _coerce_tools(result: Any) -> list[dict[str, Any]]:
    """The server's `tools/list` result shape can drift. We accept:
        {"tools": [...]}, [...], None, "anything else" -> [].
    Each tool entry that isn't a dict is dropped.
    """
    if isinstance(result, list):
        candidates = result
    elif isinstance(result, dict):
        inner = result.get("tools")
        candidates = inner if isinstance(inner, list) else []
    else:
        candidates = []
    return [t for t in candidates if isinstance(t, dict)]


def _count_tool(tool: dict[str, Any], enc) -> ToolStats:
    """Recipe A+ — split the wire bytes into 4 buckets so the agent
    sees where its tokens are, then return a ToolStats."""
    name = str(tool.get("name") or "").strip() or "<unnamed>"
    description = tool.get("description") or ""
    schema = tool.get("inputSchema") or {}
    annotations = tool.get("annotations") or {}
    name_tokens = len(enc.encode(name))
    description_tokens = len(enc.encode(description)) if isinstance(description, str) else 0
    schema_tokens = len(enc.encode(json.dumps(schema, separators=(",", ":")))) if schema else 0
    annotations_tokens = (
        len(enc.encode(json.dumps(annotations, separators=(",", ":"))))
        if annotations
        else 0
    )
    total = name_tokens + description_tokens + schema_tokens + annotations_tokens
    return ToolStats(
        name=name,
        name_tokens=name_tokens,
        description_tokens=description_tokens,
        schema_tokens=schema_tokens,
        annotations_tokens=annotations_tokens,
        total_tokens=total,
    )


# --- subprocess plumbing -------------------------------------------------


class _Killed(Exception):
    """Internal: process didn't exit cleanly on shutdown."""


def _spawn(cmd: list[str]) -> subprocess.Popen:
    """Spawn the server. Never raise FileNotFoundError — re-raise as
    `InspectError` so the agent gets one predictable failure type."""
    if not cmd:
        raise InspectError("spawn cmd is empty")
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise InspectError(f"spawn failed: command not found: {cmd[0]!r}") from exc
    except (PermissionError, OSError) as exc:
        raise InspectError(f"spawn failed: {exc}") from exc


class _StdioReader:
    """Daemon reader thread: stdout -> `queue.Queue`. We use a thread
    instead of `selectors.DefaultSelector` because the latter raises
    `[WinError 10093]` (WSAStartup not called) on Windows when the
    current Python process hasn't yet opened a socket.

    Sentinel values for stream-lifecycle:
        None -> EOF (process closed stdout)
        ("ERR", exc) -> reader died with an exception
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._q: queue.Queue = queue.Queue()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        try:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    self._q.put(None)
                    return
                self._q.put(line)
        except Exception as exc:  # pragma: no cover (defensive)
            self._q.put(("ERR", exc))

    def recv_until_id(
        self,
        expected_id: Any,
        deadline_monotonic: float,
    ) -> dict[str, Any] | None:
        """Read JSON-RPC frames until we see one whose `id` matches.
        Skip notifications and out-of-order replies."""
        while True:
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            if remaining <= 0:
                return None
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None  # EOF
            if isinstance(line, tuple) and line[0] == "ERR":
                raise InspectError(f"reader died: {line[1]!r}")
            msg = _decode(line)
            if msg is None:
                continue
            if "id" not in msg:
                continue  # notification: skip
            if msg.get("id") != expected_id:
                continue
            return msg


# --- top-level entry -----------------------------------------------------


def inspect_server(
    cmd: list[str],
    *,
    encoding: str = DEFAULT_ENCODING,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    version: str = "0.1.0",
) -> InspectReport:
    """Spawn `cmd`, run initialize + tools/list, count tokens. The
    output is the same for the CLI and the MCP server path."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        raise InspectError("cmd must be a non-empty list[str] of strings")
    if encoding not in SUPPORTED_ENCODINGS:
        raise InspectError(
            f"encoding {encoding!r} is not supported. "
            f"Pick one of {list(SUPPORTED_ENCODINGS)}."
        )
    if timeout_seconds <= 0 or timeout_seconds > 60:
        raise InspectError(
            f"timeout_seconds {timeout_seconds!r} is outside (0, 60]"
        )

    enc = tiktoken.get_encoding(encoding)
    server_label = " ".join(cmd)
    started = time.monotonic()
    proc = _spawn(cmd)

    try:
        deadline = started + timeout_seconds
        reader = _StdioReader(proc)

        try:
            proc.stdin.write(
                _encode(
                    {
                        "jsonrpc": "2.0",
                        "id": _INIT_ID,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _LSP_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {
                                "name": "mcptokens",
                                "version": version,
                            },
                        },
                    }
                )
            )
            proc.stdin.flush()
            init = reader.recv_until_id(_INIT_ID, deadline)
            if init is None:
                raise InspectError(
                    f"`initialize` exceeded {timeout_seconds:g}s without response"
                )
            if "error" in init:
                raise InspectError(
                    f"server error on `initialize`: {init['error']}"
                )

            # notifications/initialized (no id; agent doesn't reply)
            try:
                proc.stdin.write(
                    _encode({"jsonrpc": "2.0", "method": "notifications/initialized"})
                )
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

            proc.stdin.write(
                _encode(
                    {
                        "jsonrpc": "2.0",
                        "id": _TOOLS_LIST_ID,
                        "method": "tools/list",
                        "params": {},
                    }
                )
            )
            proc.stdin.flush()
            tools_msg = reader.recv_until_id(_TOOLS_LIST_ID, deadline)
            if tools_msg is None:
                raise InspectError(
                    f"`tools/list` exceeded {timeout_seconds:g}s without response"
                )
            if "error" in tools_msg:
                raise InspectError(
                    f"server error on `tools/list`: {tools_msg['error']}"
                )

        finally:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass

        # Close stdin so the server's loop reads EOF. Give it 1s to
        # exit cleanly; if it doesn't, kill it.
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass

    except InspectError:
        # On error: close stdin to break server's reader, kill if not
        # exiting, then drain stderr to give the user a one-liner
        # into the failure.
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        stderr_tail = _drain_stderr(proc, max_chars=400)
        if stderr_tail:
            # Re-raise with the tail appended so the agent sees one
            # tidy line, not a stack trace.
            try:
                raise
            except InspectError as exc:
                if stderr_tail not in str(exc):
                    raise InspectError(f"{exc} | server stderr: {stderr_tail!r}") from None
        raise
    finally:
        # On happy path, still drain stderr in case there were warnings
        # worth recording. Don't blow up if the proc is gone.
        _ = proc.poll()

    result = tools_msg.get("result")
    tools = _coerce_tools(result)
    stats = [_count_tool(t, enc) for t in tools]
    wire_total = len(enc.encode(json.dumps(result if result is not None else {}, separators=(",", ":"))))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return InspectReport(
        ok=True,
        server=server_label,
        tools=stats,
        wire_total_tokens=wire_total,
        encoding=encoding,
        elapsed_ms=elapsed_ms,
        version=version,
    )


def _drain_stderr(proc: subprocess.Popen, *, max_chars: int) -> str:
    """Read whatever is left on stderr (after the process is dead).
    Don't block forever — fd is closed; the OS says EOF fast."""
    try:
        raw = proc.stderr.read()
    except (OSError, ValueError):
        return ""
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return "..." + text[-max_chars:]


# --- dispatch (v1.0.0) -------------------------------------------------


def _coerce_command(command) -> list[str]:
    """`command` may arrive as a string OR a list of strings. We
    normalise to a list. Strings are split using POSIX shell rules
    so users can pass `"python -m srv"` exactly as written in their
    MCP config without re-tokenising."""
    import shlex
    if isinstance(command, list):
        if not command or not all(isinstance(a, str) for a in command):
            raise InspectError("command array must be a non-empty list of strings")
        return command
    if isinstance(command, str):
        if not command.strip():
            raise InspectError("command string is empty")
        try:
            return shlex.split(command, posix=True)
        except ValueError as exc:
            raise InspectError(f"command string failed shlex split: {exc!r}") from exc
    raise InspectError(
        f"command must be a string or list of strings, got {type(command).__name__}"
    )


def _fail_report(
    server: str,
    encoding: str,
    version: str,
    error: str,
    *,
    started_monotonic: float,
) -> InspectReport:
    """Build a non-OK report with zero tools but a one-line error."""
    return InspectReport(
        ok=False,
        server=server,
        tools=[],
        wire_total_tokens=0,
        encoding=encoding,
        elapsed_ms=int((time.monotonic() - started_monotonic) * 1000),
        error=error,
        version=version,
    )


def inspect(req: dict[str, Any]) -> InspectReport:
    """Single transport-aware entry. `req` is the JSON-RPC
    `arguments` dict the agent passes. Recognised keys:

    - `transport`: "stdio" (default) or "streamable_http".
    - `command`: spawn argv. String ("python -m srv") or array
      (["python","-m","srv"]). Required for stdio.
    - `url`: HTTP endpoint. Required for streamable_http.
    - `headers`: optional dict forwarded to every HTTP request.
    - `encoding`: tiktoken encoding ("cl100k_base" or "o200k_base").
    - `timeout`: per-call wall-clock timeout in seconds (1-60).

    Always returns an `InspectReport`; on failure `ok=False` and
    `error` carries a one-line reason. Same JSON shape regardless
    of transport — that's the whole point of v1.0.0: one tool,
    one response shape the agent learns once."""
    from mcptokens._http import inspect_http

    transport = req.get("transport", "stdio")
    encoding = req.get("encoding", DEFAULT_ENCODING)
    timeout = float(req.get("timeout", DEFAULT_TIMEOUT_SECONDS))
    version = req.get("version", "1.0.0")

    started = time.monotonic()
    if transport == "stdio":
        try:
            command = _coerce_command(req.get("command"))
        except InspectError as exc:
            return _fail_report(
                server="<stdio>", encoding=encoding, version=version,
                error=str(exc), started_monotonic=started,
            )
        if not command:
            return _fail_report(
                server="<stdio>", encoding=encoding, version=version,
                error="command is empty", started_monotonic=started,
            )
        try:
            return inspect_server(
                command,
                encoding=encoding,
                timeout_seconds=timeout,
                version=version,
            )
        except InspectError as exc:
            return _fail_report(
                server=" ".join(command), encoding=encoding, version=version,
                error=str(exc), started_monotonic=started,
            )
    if transport == "streamable_http":
        url = req.get("url")
        if not isinstance(url, str) or not url:
            return _fail_report(
                server="<streamable_http>", encoding=encoding, version=version,
                error="url is required for streamable_http transport",
                started_monotonic=started,
            )
        headers = req.get("headers") or {}
        if not isinstance(headers, dict):
            return _fail_report(
                server=url, encoding=encoding, version=version,
                error="headers must be a dict of {name: str}",
                started_monotonic=started,
            )
        try:
            return inspect_http(
                url,
                headers=dict(headers),
                encoding=encoding,
                timeout_seconds=timeout,
                version=version,
            )
        except InspectError as exc:
            return _fail_report(
                server=url, encoding=encoding, version=version,
                error=str(exc), started_monotonic=started,
            )
    return _fail_report(
        server=f"<{transport!r}>", encoding=encoding, version=version,
        error=f"unknown transport: {transport!r}",
        started_monotonic=started,
    )
