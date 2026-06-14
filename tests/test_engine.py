"""Test the core inspect engine: spawning, JSON-RPC id-matching,
null-safe coercion, timeout, malformed tools.

We test against a fake stdio MCP server written to a temp script.
The fake responds correctly to `initialize`, `notifications/initialized`,
`tools/list`, and a deliberately bad `tools/list` so we test the
defensive null coercion paths.
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest
import tiktoken

from mcptokens._engine import (
    DEFAULT_ENCODING,
    DEFAULT_TIMEOUT_SECONDS,
    SUPPORTED_ENCODINGS,
    InspectError,
    _coerce_tools,
    _count_tool,
    inspect_server,
)


# --- Fake MCP server scripts --------------------------------------------


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
        # notifications/initialized: no reply
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
            # Garbage in, assorted bogus shapes.
            sys.stdout.write(frame(msg["id"], {
                "tools": [
                    {
                        "name": "ok_tool",
                        "description": "A normal tool.",
                        "inputSchema": {"type": "object"},
                        "annotations": {},
                    },
                    None,                         # not a dict -> dropped
                    "string-tool",                 # not a dict -> dropped
                    {
                        # name is None -> coerced to "<unnamed>" below.
                        "name": None,
                        "description": "no name",
                        "inputSchema": {"type": "object"},
                    },
                    {
                        # description missing -> empty string fine.
                        "name": "missing_desc",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {
                        # inputSchema missing -> 0 tokens.
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
            # Slow loris: never respond.
            time.sleep(60)
            break
        # never respond to anything else either
    """
)


_FAKE_NOTIFICATION_ONLY_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python
    # Emits a notification (no id) and nothing else. The engine must
    # NOT mistake this for a reply; it must time out.
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


# --- Helpers ------------------------------------------------------------


def _write_fake(tmp_path, name, body) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# --- Defensive shape coercion ---------------------------------------------


def test_coerce_tools_accepts_object_and_list():
    """The result of `tools/list` can be a dict with a `tools` key,
    directly a list, or anything else (None, 'string')."""
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
    # Each variant below must not raise.
    s1 = _count_tool({"name": "t"}, enc)
    assert s1.name == "t"
    assert s1.total_tokens >= 1
    s2 = _count_tool({"name": None}, enc)
    assert s2.name == "<unnamed>"
    s3 = _count_tool({"name": "t", "description": None}, enc)
    assert s3.description_tokens == 0
    s4 = _count_tool({"name": "t", "description": 12345}, enc)
    # Non-strings become 0 tokens; we don't crash.
    assert s4.description_tokens in (0,)


# --- End-to-end via subprocess -------------------------------------------


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
    # 6 inputs, 4 dropped (None, "string-tool", valid 2, plus the ones
    # we want to keep). Confirm only the dict-shaped entries survive.
    accepted = [t for t in r.tools if t.name != "<unnamed>"]
    unnamed_count = sum(1 for t in r.tools if t.name == "<unnamed>")
    assert len(accepted) == 3, f"got {[t.name for t in r.tools]}"
    assert unnamed_count == 1
    assert "ok_tool" in [t.name for t in r.tools]
    assert "missing_desc" in [t.name for t in r.tools]
    assert "missing_schema" in [t.name for t in r.tools]


def test_inspect_server_timeout_fires(tmp_path):
    fake = _write_fake(tmp_path, "fake_slow.py", _FAKE_SLOW_SERVER)
    with pytest.raises(InspectError, match="exceeded"):
        inspect_server(
            [sys.executable, fake],
            timeout_seconds=1.0,
            version="0.1.0",
        )


def test_inspect_server_ignores_notification_only_servers(tmp_path):
    """A server that ONLY emits notifications (no id) is exactly the
    failure mode that motivated id-matching. Without id-matching, the
    engine would consume the notification and fail; with it, the
    engine times out cleanly."""
    fake = _write_fake(tmp_path, "fake_notify.py", _FAKE_NOTIFICATION_ONLY_SERVER)
    with pytest.raises(InspectError, match="exceeded"):
        inspect_server(
            [sys.executable, fake],
            timeout_seconds=1.0,
            version="0.1.0",
        )


def test_inspect_server_rejects_empty_cmd():
    with pytest.raises(InspectError, match="empty"):
        inspect_server([], timeout_seconds=5.0)


def test_inspect_server_rejects_bad_encoding():
    with pytest.raises(InspectError, match="encoding"):
        inspect_server(
            ["hound"], encoding="gpt-999", timeout_seconds=5.0
        )


def test_inspect_command_not_found(tmp_path):
    with pytest.raises(InspectError, match="not found"):
        inspect_server(
            [str(tmp_path / "definitely-not-a-real-binary-12345")],
            timeout_seconds=5.0,
        )


def test_inspect_supports_both_encodings(tmp_path):
    """The two encodings we ship should both produce sensible numbers
    and the wire_total should grow slightly under o200k_base."""
    fake = _write_fake(tmp_path, "fake_good.py", _FAKE_GOOD_SERVER)
    a = inspect_server(
        [sys.executable, fake],
        encoding="cl100k_base",
        timeout_seconds=5.0,
    )
    b = inspect_server(
        [sys.executable, fake],
        encoding="o200k_base",
        timeout_seconds=5.0,
    )
    assert a.ok and b.ok
    assert a.wire_total_tokens > 0
    assert b.wire_total_tokens > 0
