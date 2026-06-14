# Changelog

All notable changes to `mcptokens` are documented in this file.

## [Unreleased]

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
