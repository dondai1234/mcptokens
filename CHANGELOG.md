# Changelog

All notable changes to `mcptokens` are documented in this file.

## [Unreleased]

## [1.0.1] — 2026-06-14

UX fix. v1.0.0's tool description told the agent to ask the
user for the spawn argv on every candidate MCP server, which is
needless friction: the agent can read its harness's MCP config,
inspect a running server, or otherwise figure it out without a
round-trip.

Removed. The description now lists canonical spawn patterns
(binary, python module, npx, docker) only as references, and
the agent picks one of:

  inspect(command=["python","-m","some_mcp_server"])

shape (string or array shlex-split) and figures out the actual
argv from its own context. Tests pin:
`test_description_does_not_pester_user` asserts no
"ask the user" string remains.

Self-cost dropped 691 → 576 tokens of `cl100k_base` as a
side-effect of removing the line.

## [1.0.0] — 2026-06-14

First major release. One tool — `inspect` — now covers both
stdio and Streamable HTTP MCP servers, with a sharper description
that lets the agent learn the call shape on the first try.

Multi-transport support:

- `transport="stdio"` (default): spawn a subprocess. Same as
  before.
- `transport="streamable_http"`: POST initialize + tools/list
  to a remote endpoint per MCP 2025-03-26. Server may reply
  via `application/json` (single message) or `text/event-stream`
  (one or more messages). The inspector handles both. Stdlib
  `urllib.request` — no `httpx` dependency. Optional `headers`
  for auth (e.g. `{"Authorization": "Bearer ..."}`).

Agent-time speedups (the model spends fewer tokens deciding
how to use it):

- `command` accepts a string (`"python -m srv"`) OR array
  (`["python","-m","srv"]`). Shlex-split; both normalized to a
  list before spawn.
- Description lists the canonical spawn patterns (binary, python
  module, npm/npx, docker) and explicitly nudges the agent to
  ASK THE USER for argv it doesn't already know.
- Output is compact by default (only `{name, total}` per tool)
  so the agent can scan many candidates in one round without
  burning context on per-schema dumps. Pass `verbose=true` on
  a tool call for the full Recipe A+ breakdown (we left this
  hook off the public schema deliberately to avoid bloating the
  agent's view of the world; only the description references it).

Reliability hardening:

- Errors come back as `{"ok": false, "error": "...", ...}` —
  same shape for spawn / protocol / HTTP / timeout / unknown
  transport. No stack traces leak to the agent.
- HTTP transport tolerates 202 Accepted on `notifications/initialized`
  per spec; missing `Mcp-Session-Id` header is fine.
- The dispatcher traps unexpected exceptions before they can
  break the MCP server loop; a defensive net for any path we
  didn't enumerate.

Self-cost rose to 691 tokens of `cl100k_base` (one tool, two
transports, sharper description). Still under the 1000-token
budget; pinned by `test_self_cost_under_budget` (< 900) and
`test_description_is_tight` (< 500).

Tests: 45 / 45 passing in ~20 s, including:

- Stdio: subprocess end-to-end, malformed shapes, timeout,
  notification-only servers, missing-binary failure.
- HTTP: real `http.server.ThreadingHTTPServer` on `127.0.0.1`,
  both `application/json` and `text/event-stream` responses.
- Dispatcher: command-as-string shlex split, command-as-array,
  transport="streamable_http" with URL, URL-less, unreachable URL,
  unknown transport, compact default response, verbose response.

## [0.1.5] — 2026-06-14

Bug fix: `mcptokens serve` no longer drops the connection on
OpenCode and other strict MCP clients that send `Content-Length`
framed inputs.

The mcp SDK 1.27 `stdio_server` only knew line-delimited JSON on
input. When a parent sent `Content-Length` headers, the SDK
logged `Internal Server Error` `notifications/message` to stdout
BEFORE the actual response, and OpenCode (and other strict
clients) read that error and closed the connection with MCP
`-32000 Connection closed`.

The server now pre-processes stdin via a small
`_FramedNDJSONStream` that absorbs both NDJSON and
`Content-Length`-framed messages and emits one NDJSON line per
MCP message. Tests pin this regression:

- `test_serve_handles_content_length_framing` sends a fully
  Content-Length framed initialize and asserts no error
  notification leaks to stdout.
- `test_framed_stream_handles_ndjson_and_framed_mixed`
  exercises mixed NDJSON + Content-Length in a single stream.

Server `Server("mcptokens", version=__version__)` now reports the
actual `mcptokens` package version (was misreporting the mcp SDK
version under some framing combos).

## [0.1.4] — 2026-06-14

Repository cleanup.

- Deleted three per-version `RELEASE_NOTES_v0.1.{1,2,3}.md`
  files. The CHANGELOG.md is now the canonical ledger.
- Each GitHub Release note is set inline from its CHANGELOG
  entry. No per-version files in the repo.

## [0.1.3] — 2026-06-14

Public copy scrub.

- README on PyPI: dropped `## Tests` section, dropped the
  `Source` and `Tests` rows from `## The numbers`, replaced
  `mcptokens hound` examples with generic
  `python -m some_mcp_server`, and removed the comment
  `# 1023 wire tokens, 8 tools` from the inspect example.

## [0.1.2] — 2026-06-14

PyPI metadata cleanup.

- `project_urls` (Repository / Issues / Changelog) point at
  the new repo (`dondai1234/mcptokens`). The 0.1.1 wheel
  pointed them at the abandoned `dondai1234/contextlens`.
- CHANGELOG branch in the URL updated from `master` to
  `main`.

## [0.1.1] — 2026-06-14

Repo migration.

- Project moved from `dondai1234/contextlens` to the fresh repo
  `dondai1234/mcptokens`. Source unchanged; PyPI package name
  unchanged. New repo carries a single initial commit with the
  full project tree.

## [0.1.0] — 2026-06-14

First release on PyPI.

- One MCP tool exposed: `inspect`.
- Engine spawns a stdio MCP server, runs JSON-RPC `initialize`
  and `tools/list`, defensive against malformed server shapes,
  returns per-tool tokens plus a wire total.
- Cross-platform stdio recipe: daemon reader thread +
  `queue.Queue`, with `queue.get(timeout=remaining)`.
  `os.set_blocking` is absent on `sys.platform == "win32"`;
  `selectors.DefaultSelector` raises `WinError 10093` on
  Windows when WSAStartup hasn't been called.
- JSON-RPC id-matching so servers that emit only notifications
  don't ghost-return empty tools.
- Slim CLI: `mcptokens [--json] [--timeout N] [--encoding E]
  <server-argv>...` and `mcptokens serve`.
- Self-cost of the shipped tool definition: **172 tokens** of
  `cl100k_base`, enforced at import via
  `_enforce_self_token_budget()` in
  `src/mcptokens/_server.py`.
