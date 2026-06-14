"""Test the core inspect engine: spawning, JSON-RPC id-matching,
null-safe coercion, timeout, malformed tools, transport dispatch
(stdio + Streamable HTTP).

We test against a fake stdio MCP server written to a temp script
for the stdio path, and against an in-process `http.server.ThreadingHTTPServer`
on a free localhost port for the HTTP path.
"""
from __future__ import annotations

import http.server
import json
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest
import tiktoken

from mcptokens._engine import (
    DEFAULT_ENCODING,
    DEFAULT_TIMEOUT_SECONDS,
    SUPPORTED_ENCODINGS,
    InspectError,
    _coerce_command,
    _coerce_tools,
    _count_tool,
    inspect,
    inspect_server,
)


# --- Fake stdio MCP server scripts (stdio transport) ----------------


_FAKE_GOOD_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python
    import json, sys

    def frame(_id, result):
        return json.dumps({"jsonrpc": "2.0", "id": _id, "result": result}) + "\\n"

    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if method == "initialize":
            sys.stdout.write(frame(msg["id"], {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fake-good", "version": "0.1"},
                "capabilities": {"tools": {}},
            }))
        elif method == "tools/list":
            sys.stdout.write(frame(msg["id"], {
                "tools": [{
                    "name": "echo",
                    "description": "Echo a string.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "annotations": {"title": "Echo"},
                }],
            }))
        sys.stdout.flush()
    """
)


_FAKE_BAD_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python
    import json, sys

    def frame(_id, result):
        return json.dumps({"jsonrpc": "2.0", "id": _id, "result": result}) + "\\n"

    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if method == "initialize":
            sys.stdout.write(frame(msg["id"], {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fake-bad", "version": "0.1"},
                "capabilities": {"tools": {}},
            }))
        elif method == "tools/list":
            sys.stdout.write(frame(msg["id"], {
                "tools": [
                    {
                        "name": "ok_tool",
                        "description": "A normal tool.",
                        "inputSchema": {"type": "object"},
                        "annotations": {},
                    },
                    None,
                    "string-tool",
                    {
                        "name": None,
                        "description": "no name",
                        "inputSchema": {"type": "object"},
                    },
                    {
                        "name": "missing_desc",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {
                        "name": "missing_schema",
                        "description": "no schema",
                    },
                ],
            }))
        sys.stdout.flush()
    """
)


_FAKE_SLOW_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python
    import json, sys, time

    def frame(_id, result):
        return json.dumps({"jsonrpc": "2.0", "id": _id, "result": result}) + "\\n"

    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        if method == "initialize":
            time.sleep(60)
            break
    """
)


_FAKE_NOTIFICATION_ONLY_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python
    # Emits only notifications (no responses with id). Engine must
    # time out cleanly, not consume the notification as a response.
    import json, sys, time

    def emit_notification(method):
        msg = json.dumps({"jsonrpc": "2.0", "method": method}) + "\\n"
        sys.stdout.write(msg)
        sys.stdout.flush()

    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("method") == "initialize":
            emit_notification("notifications/initialized")
            time.sleep(60)
            break
    """
)


def _write_fake(tmp_path, name, body) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# --- Defensive shape coercion (engine internals) ------------------


def test_coerce_tools_accepts_object_and_list():
    """`tools/list` can return a dict with `tools`, a list, or junk."""
    enc = tiktoken.get_encoding(DEFAULT_ENCODING)
    valid_tool = {
        "name": "ok",
        "description": "ok",
        "inputSchema": {"type": "object"},
    }
    assert _coerce_tools({"tools": [valid_tool, None, "x", valid_tool]}) == [valid_tool, valid_tool]
    assert _coerce_tools([valid_tool, None]) == [valid_tool]
    assert _coerce_tools(None) == []
    assert _coerce_tools("not-a-dict-or-list") == []
    assert _coerce_tools({"tools": "not-a-list"}) == []


def test_count_tool_handles_missing_and_typed_wrong_fields():
    enc = tiktoken.get_encoding(DEFAULT_ENCODING)
    s1 = _count_tool({"name": "t"}, enc)
    assert s1.name == "t"
    assert s1.total_tokens >= 1
    s2 = _count_tool({"name": None}, enc)
    assert s2.name == "<unnamed>"
    s3 = _count_tool({"name": "t", "description": None}, enc)
    assert s3.description_tokens == 0


# --- command flex (string|array) -------------------------------


def test_coerce_command_accepts_string_or_list():
    assert _coerce_command("python -m srv") == ["python", "-m", "srv"]
    assert _coerce_command('python -m "my srv"') == ["python", "-m", "my srv"]
    assert _coerce_command(["python", "-m", "srv"]) == ["python", "-m", "srv"]
    assert _coerce_command(["hound"]) == ["hound"]


def test_coerce_command_rejects_garbage():
    with pytest.raises(InspectError, match="empty"):
        _coerce_command("")
    with pytest.raises(InspectError, match="empty"):
        _coerce_command([])
    with pytest.raises(InspectError, match="string or list"):
        _coerce_command(None)
    with pytest.raises(InspectError, match="non-empty"):
        _coerce_command([1, 2])


# --- End-to-end via subprocess (stdio) --------------------------


def test_inspect_server_happy_path(tmp_path):
    fake = _write_fake(tmp_path, "fake_good.py", _FAKE_GOOD_SERVER)
    r = inspect_server(
        [sys.executable, fake], timeout_seconds=5.0, version="0.1.0"
    )
    assert r.ok is True
    assert r.error == ""
    assert len(r.tools) == 1
    assert r.tools[0].name == "echo"
    assert r.tools[0].total_tokens > 0
    assert r.wire_total_tokens > 0
    assert r.encoding == DEFAULT_ENCODING


def test_inspect_server_bad_shapes_do_not_crash(tmp_path):
    fake = _write_fake(tmp_path, "fake_bad.py", _FAKE_BAD_SERVER)
    r = inspect_server(
        [sys.executable, fake], timeout_seconds=5.0, version="0.1.0"
    )
    assert r.ok is True
    accepted = [t for t in r.tools if t.name != "<unnamed>"]
    unnamed_count = sum(1 for t in r.tools if t.name == "<unnamed>")
    assert len(accepted) == 3, f"got {[t.name for t in r.tools]}"
    assert unnamed_count == 1
    assert "ok_tool" in [t.name for t in r.tools]


def test_inspect_server_timeout_fires(tmp_path):
    fake = _write_fake(tmp_path, "fake_slow.py", _FAKE_SLOW_SERVER)
    with pytest.raises(InspectError, match="exceeded"):
        inspect_server([sys.executable, fake], timeout_seconds=1.0, version="0.1.0")


def test_inspect_server_ignores_notification_only_servers(tmp_path):
    fake = _write_fake(tmp_path, "fake_notify.py", _FAKE_NOTIFICATION_ONLY_SERVER)
    with pytest.raises(InspectError, match="exceeded"):
        inspect_server([sys.executable, fake], timeout_seconds=1.0, version="0.1.0")


def test_inspect_server_rejects_empty_cmd():
    with pytest.raises(InspectError, match="empty"):
        inspect_server([], timeout_seconds=5.0)


def test_inspect_server_rejects_bad_encoding():
    with pytest.raises(InspectError, match="encoding"):
        inspect_server(["hound"], encoding="gpt-999", timeout_seconds=5.0)


def test_inspect_command_not_found(tmp_path):
    with pytest.raises(InspectError, match="not found"):
        inspect_server(
            [str(tmp_path / "definitely-not-a-real-binary-12345")],
            timeout_seconds=5.0,
        )


def test_inspect_supports_both_encodings(tmp_path):
    fake = _write_fake(tmp_path, "fake_good.py", _FAKE_GOOD_SERVER)
    a = inspect_server([sys.executable, fake], encoding="cl100k_base", timeout_seconds=5.0)
    b = inspect_server([sys.executable, fake], encoding="o200k_base", timeout_seconds=5.0)
    assert a.ok and b.ok
    assert a.wire_total_tokens > 0 and b.wire_total_tokens > 0


# --- dispatcher (v1.0.0) --------------------------------------


def test_inspect_dispatches_stdio_with_array_command():
    import shutil
    if not shutil.which("hound"):
        pytest.skip("hound binary not on PATH; stdio smoke only with hound")
    r = inspect({"transport": "stdio", "command": ["hound"], "timeout": 10})
    assert r.ok, f"unexpected failure: {r.error}"
    assert len(r.tools) >= 1
    assert r.wire_total_tokens > 0


def test_inspect_dispatches_stdio_with_string_command():
    import shutil
    if not shutil.which("hound"):
        pytest.skip("hound not on PATH")
    r = inspect({"transport": "stdio", "command": "hound", "timeout": 10})
    assert r.ok, f"unexpected failure: {r.error}"
    assert len(r.tools) >= 1


def test_inspect_stdio_failure_yields_ok_false_one_liner():
    """Stdio with a missing binary must NOT raise; the report gives
    `ok=false` and a useful error string. Agents reading the tool
    output get a clean failure, not a stack trace."""
    r = inspect({"transport": "stdio", "command": ["definitely-not-a-real-binary-xyz-999"]})
    assert r.ok is False
    assert "not found" in r.error.lower()


# --- Streamable HTTP fixtures (v1.0.0) -------------------------


class _FakeJSONHTTPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # silence
        pass

    def do_POST(self):  # type: ignore[override]
        content_length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_length) if content_length else b""
        try:
            msg = json.loads(body_raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        method = msg.get("method")
        m_id = msg.get("id")
        sid_header = None
        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": m_id,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "serverInfo": {"name": "fake-json", "version": "0.1"},
                    "capabilities": {"tools": {}},
                },
            }
            sid_header = "session-json-123"
        elif method == "tools/list":
            response = {
                "jsonrpc": "2.0",
                "id": m_id,
                "result": {
                    "tools": [
                        {"name": "echo", "description": "Echo.", "inputSchema": {"type": "object"}},
                        {"name": "compute", "description": "Compute.", "inputSchema": {"type": "object"}},
                    ]
                },
            }
        elif method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        else:
            response = {"jsonrpc": "2.0", "id": m_id, "error": {"code": -32601, "message": f"unknown: {method}"}}
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if sid_header:
            self.send_header("Mcp-Session-Id", sid_header)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _FakeSSEHTTPHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_POST(self):  # type: ignore[override]
        content_length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_length) if content_length else b""
        try:
            msg = json.loads(body_raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        method = msg.get("method")
        m_id = msg.get("id")
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "fake-sse", "version": "0.1"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": "echo", "description": "Echo.", "inputSchema": {"type": "object"}},
                ]
            }
        else:
            result = None
        if result is None:
            self.send_response(200)
            self.end_headers()
            return
        payload = json.dumps({"jsonrpc": "2.0", "id": m_id, "result": result})
        sse = f"event: message\ndata: {payload}\n\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(sse)))
        self.end_headers()
        try:
            self.wfile.write(sse)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _start_fake_http(handler_cls) -> tuple[str, object]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.timeout = 0.5
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}/mcp", server


# --- HTTP tests ---------------------------------------------


def test_inspect_http_json_response_round_trip():
    url, server = _start_fake_http(_FakeJSONHTTPHandler)
    try:
        r = inspect({"transport": "streamable_http", "url": url, "timeout": 10})
        assert r.ok, f"unexpected failure: {r.error}"
        assert len(r.tools) == 2
        assert {t.name for t in r.tools} == {"echo", "compute"}
        assert r.wire_total_tokens > 0
    finally:
        server.shutdown()
        server.server_close()


def test_inspect_http_sse_response_round_trip():
    url, server = _start_fake_http(_FakeSSEHTTPHandler)
    try:
        r = inspect({"transport": "streamable_http", "url": url, "timeout": 10})
        assert r.ok, f"unexpected failure: {r.error}"
        assert len(r.tools) == 1
        assert r.tools[0].name == "echo"
    finally:
        server.shutdown()
        server.server_close()


def test_inspect_http_missing_url_yields_ok_false():
    r = inspect({"transport": "streamable_http"})
    assert r.ok is False
    assert "url" in r.error


def test_inspect_http_unreachable_yields_ok_false():
    r = inspect({"transport": "streamable_http", "url": "http://127.0.0.1:1/mcp", "timeout": 2})
    assert r.ok is False
    assert r.error


def test_inspect_unknown_transport_yields_ok_false():
    r = inspect({"transport": "weird"})
    assert r.ok is False
    assert "transport" in r.error


# --- v1.1.0: error + hint always present in as_dict() ----------


def test_error_field_present_in_as_dict_on_failure():
    """v1.0.1 bug: as_dict() dropped the `error` field. The agent saw
    ok=false with no explanation and went on a wild goose chase.
    Pin: error and hint are ALWAYS in the serialised output."""
    r = inspect({"command": ["definitely-not-a-real-binary-xyz-999"]})
    payload = r.as_dict()
    assert "error" in payload, "error field missing from as_dict()"
    assert "hint" in payload, "hint field missing from as_dict()"
    assert payload["error"]  # non-empty
    assert payload["ok"] is False


def test_hint_present_on_command_not_found():
    """When a binary isn't found, the hint must direct the agent to
    its harness MCP config — that's where the real spawn argv lives."""
    r = inspect({"command": ["filesystem"]})
    assert r.ok is False
    assert r.hint  # non-empty
    assert "config" in r.hint.lower() or "harness" in r.hint.lower()


def test_hint_present_on_missing_command():
    """When the agent forgets to pass `command` at all, the hint
    must tell it where to look."""
    r = inspect({})
    assert r.ok is False
    assert r.hint


def test_hint_present_on_unknown_transport():
    r = inspect({"transport": "grpc"})
    assert r.ok is False
    assert r.hint
    assert "stdio" in r.hint or "streamable" in r.hint


def test_error_field_empty_on_success():
    """On a successful inspect, error and hint are empty strings
    (not missing, not None)."""
    import shutil
    if not shutil.which("hound"):
        pytest.skip("hound not on PATH")
    r = inspect({"command": ["hound"], "timeout": 10})
    assert r.ok is True
    payload = r.as_dict()
    assert payload["error"] == ""
    assert payload["hint"] == ""


# --- v1.1.0: Windows path handling -----------------------------


def test_coerce_command_preserves_windows_backslash_paths():
    """On Windows, `posix=False` so backslash paths survive.
    `C:\\Users\\foo\\srv.py` must NOT become `C:Usersfoosrv.py`."""
    import os
    if os.name != "nt":
        pytest.skip("Windows-only test")
    result = _coerce_command("python C:\\Users\\Dondai\\srv.py --port 8080")
    assert "C:\\Users\\Dondai\\srv.py" in result, f"backslash path mangled: {result}"


def test_coerce_command_strips_quotes_on_windows():
    """With posix=False, quotes aren't stripped by shlex. We strip
    them manually so `"my path"` still works."""
    import os
    if os.name != "nt":
        pytest.skip("Windows-only test")
    result = _coerce_command('python -m "my server"')
    assert result == ["python", "-m", "my server"]


# --- v1.1.0: Windows .cmd resolution ---------------------------


def test_spawn_resolves_cmd_on_windows():
    """On Windows, `npx` is `npx.cmd`. Popen can't find it without
    shell=True. The _spawn fallback uses shutil.which to resolve."""
    import shutil
    if not shutil.which("npx"):
        pytest.skip("npx not on PATH")
    # Just test that _spawn doesn't raise FileNotFoundError for npx.
    # The process will fail later (wrong args), but spawn must succeed.
    from mcptokens._engine import _spawn
    proc = _spawn(["npx", "--version"])
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    # If we got here without FileNotFoundError, the fix works.
    # Exit code 0 means npx --version ran successfully.
    assert proc.returncode == 0, f"npx --version exited {proc.returncode}"


# --- Compact vs verbose ---------------------------------------


def test_compact_default_response_is_compact():
    """`as_dict()` without verbose=True keeps each tool entry to
    `{"name", "tokens": int}` so the agent can scan many candidates
    in one round without burning context on per-schema dumps."""
    import shutil
    if not shutil.which("hound"):
        pytest.skip("hound not on PATH")
    r = inspect({"transport": "stdio", "command": ["hound"], "timeout": 10})
    assert r.ok
    payload = r.as_dict()
    for entry in payload["tools"]:
        assert set(entry.keys()) == {"name", "tokens"}
        assert isinstance(entry["tokens"], int)


def test_verbose_response_includes_full_breakdown():
    """Verbose flips the dataclass into the full Recipe A+ output
    (per-tool breakdown of name/description/schema/annotations)."""
    import shutil
    if not shutil.which("hound"):
        pytest.skip("hound not on PATH")
    r = inspect({"transport": "stdio", "command": ["hound"], "timeout": 10})
    assert r.ok
    payload = r.as_dict(verbose=True)
    for entry in payload["tools"]:
        tokens = entry["tokens"]
        assert isinstance(tokens, dict)
        assert {"name", "description", "schema", "annotations", "total"} <= set(tokens.keys())
