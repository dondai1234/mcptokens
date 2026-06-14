"""Streamable HTTP transport for inspecting MCP servers.

Spec MCP 2025-03-26:

- Single endpoint at a configured URL (often `/mcp`).
- Client sends JSON-RPC messages as JSON-encoded POST bodies.
- Server MAY respond with `Content-Type: application/json` (one
  JSON-RPC message) OR `Content-Type: text/event-stream` (SSE
  stream carrying one or more JSON-RPC messages).
- Server MAY assign a session ID at initialize time, returned
  in the `Mcp-Session-Id` response header. Clients send it back
  on every subsequent request.
- Client MUST send `Accept: application/json, text/event-stream`
  on POST so the server knows both encodings work.

The inspector sends initialize + notifications/initialized +
tools/list, parses each response, then counts tokens. We use
stdlib `urllib.request` to avoid a dependency on `httpx` or any
other HTTP client. Single endpoint, single connection per call;
we don't reuse sockets across calls (each call is one inspect).
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

import tiktoken

from mcptokens._engine import (
    DEFAULT_ENCODING,
    DEFAULT_TIMEOUT_SECONDS,
    SUPPORTED_ENCODINGS,
    InspectError,
    InspectReport,
    _coerce_tools,
    _count_tool,
)


_HTTP_POST = "POST"
_REQUIRED_ACCEPT = "application/json, text/event-stream"
_PROTOCOL_VERSION = "2025-03-26"


def _http_post(
    url: str,
    body: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, str], bytes, str]:
    """POST a single JSON-RPC `body` to `url`. Returns
    (status, response-headers, raw-body-bytes, content-type)."""
    merged_headers = {
        "Content-Type": "application/json",
        "Accept": _REQUIRED_ACCEPT,
        **headers,
    }
    req = urllib.request.Request(
        url,
        method=_HTTP_POST,
        data=body.encode("utf-8"),
        headers=merged_headers,
    )
    # Set the socket timeout from the deadline so the request can't
    # block past our budget even if the server is slow to respond.
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise InspectError(f"transport error: {exc!r}") from exc
    try:
        ctype = response.headers.get("Content-Type", "")
        body_bytes = response.read()
        # Flatten headers to plain dict (urllib uses HTTPMessage).
        hdrs = {k: response.headers.get(k) for k in response.headers.keys()}
    finally:
        response.close()
    return response.status, hdrs, body_bytes, ctype


def _parse_sse(body_bytes: bytes) -> list[dict[str, str]]:
    """Parse an SSE response body into a list of {event, data} dicts.
    Empty line separates events. Comment lines start with `:`."""
    text = body_bytes.decode("utf-8", errors="replace")
    events: list[dict[str, str]] = []
    cur_event: str | None = None
    cur_data: list[str] = []
    for line in text.split("\n"):
        if not line:
            if cur_data or cur_event is not None:
                events.append(
                    {"event": cur_event or "message", "data": "\n".join(cur_data)}
                )
                cur_event = None
                cur_data = []
        elif line.startswith(":"):
            continue
        elif ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                cur_event = value
            elif field == "data":
                cur_data.append(value)
            # id, retry, and other fields are ignored — we don't
            # care about resumability for a one-shot inspection.
    return events


def _extract_messages(content_type: str, body_bytes: bytes) -> list[dict[str, Any]]:
    """Pull JSON-RPC messages from a server response body, depending
    on its `Content-Type` (application/json or text/event-stream)."""
    if content_type.startswith("application/json"):
        try:
            obj = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InspectError(f"server sent invalid JSON: {exc!r}") from exc
        if isinstance(obj, list):
            return obj
        return [obj]
    if content_type.startswith("text/event-stream"):
        events = _parse_sse(body_bytes)
        msgs: list[dict[str, Any]] = []
        for ev in events:
            try:
                msgs.append(json.loads(ev["data"]))
            except json.JSONDecodeError:
                continue
        return msgs
    raise InspectError(f"unexpected content-type: {content_type!r}")


def _ensure_supported(encoding: str, timeout: float) -> tiktoken.Encoding:
    if encoding not in SUPPORTED_ENCODINGS:
        raise InspectError(
            f"encoding {encoding!r} is not supported. "
            f"Pick one of {list(SUPPORTED_ENCODINGS)}."
        )
    if timeout <= 0 or timeout > 60:
        raise InspectError(f"timeout {timeout!r} is outside (0, 60]")
    return tiktoken.get_encoding(encoding)


def _send_request_with_id(
    url: str,
    msg_id: int,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str],
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Send a JSON-RPC request and return the matching `result`.
    Raises `InspectError` on timeout, mismatch, or server `error`."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
    )
    remaining = max(0.001, deadline_monotonic - time.monotonic())
    status, _resp_headers, body_bytes, ctype = _http_post(
        url, body, headers, remaining
    )
    if status != 200:
        raise InspectError(f"{method}:HTTP {status}")
    msgs = _extract_messages(ctype, body_bytes)
    match = [m for m in msgs if isinstance(m, dict) and m.get("id") == msg_id]
    if len(match) != 1:
        raise InspectError(
            f"{method}:expected 1 message with id={msg_id}, got "
            f"{len(msgs) if msgs else 0} (id-skew or extra notifications)"
        )
    msg = match[0]
    if "error" in msg:
        raise InspectError(f"{method} error: {msg['error']}")
    return msg.get("result", {})


def _send_notification(
    url: str,
    method: str,
    headers: dict[str, str],
    deadline_monotonic: float,
) -> None:
    """Send a JSON-RPC notification (no `id`). Per MCP spec the
    server usually returns 202 Accepted; we tolerate that and
    treat non-200 as non-fatal (notifications don't strictly need
    a response)."""
    body = json.dumps({"jsonrpc": "2.0", "method": method})
    remaining = max(0.001, deadline_monotonic - time.monotonic())
    try:
        status, _, _, _ = _http_post(url, body, headers, remaining)
    except InspectError:
        # Tolerate transient errors on notifications; the inspect
        # succeeds as long as initialize and tools/list worked.
        return
    # Notifications MAY return 202 Accepted (body ignored) or any
    # success code; we don't strictly need to inspect the body.
    del status


def inspect_http(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    encoding: str = DEFAULT_ENCODING,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    version: str = "1.0.0",
    client_name: str = "mcptokens",
) -> InspectReport:
    """Connect to a Streamable HTTP MCP server at `url`, run the
    initialize + tools/list handshake, return the per-tool tokens
    plus a wire total. `headers` is forwarded on every POST
    (use it for `Authorization: Bearer ...`)."""
    enc = _ensure_supported(encoding, timeout_seconds)
    hdrs = dict(headers or {})
    started = time.monotonic()
    deadline = started + timeout_seconds

    # Step 1: initialize. Capture session id from response.
    init_result, init_resp_headers = _initialize(
        url, hdrs, client_name, version, deadline
    )
    del init_result  # we don't need it; session id is the value
    session_id = init_resp_headers.get("Mcp-Session-Id") or init_resp_headers.get(
        "mcp-session-id"
    )
    if session_id:
        hdrs = {**hdrs, "Mcp-Session-Id": session_id}

    # Step 2: notifications/initialized.
    _send_notification(url, "notifications/initialized", hdrs, deadline)

    # Step 3: tools/list. Required for the report.
    result = _send_request_with_id(
        url,
        msg_id=2,
        method="tools/list",
        params={},
        headers=hdrs,
        deadline_monotonic=deadline,
    )

    tools = _coerce_tools(result)
    stats = [_count_tool(t, enc) for t in tools]
    wire_total = len(
        enc.encode(
            json.dumps(result if result is not None else {}, separators=(",", ":"))
        )
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return InspectReport(
        ok=True,
        server=url,
        tools=stats,
        wire_total_tokens=wire_total,
        encoding=encoding,
        elapsed_ms=elapsed_ms,
        version=version,
    )


def _initialize(
    url: str,
    headers: dict[str, str],
    client_name: str,
    version: str,
    deadline_monotonic: float,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Send `initialize`, return (result, all-response-headers)."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": version},
            },
        }
    )
    remaining = max(0.001, deadline_monotonic - time.monotonic())
    status, resp_headers, body_bytes, ctype = _http_post(url, body, headers, remaining)
    if status != 200:
        raise InspectError(f"initialize:HTTP {status}")
    msgs = _extract_messages(ctype, body_bytes)
    init_msgs = [m for m in msgs if isinstance(m, dict) and m.get("id") == 1]
    if len(init_msgs) != 1:
        raise InspectError(
            f"initialize:expected 1 message with id=1, got {len(init_msgs)}"
        )
    msg = init_msgs[0]
    if "error" in msg:
        raise InspectError(f"initialize error: {msg['error']}")
    return msg.get("result", {}), resp_headers
