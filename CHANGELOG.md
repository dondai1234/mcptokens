# Changelog

All notable changes to `mcptokens` are documented in this file.

## [Unreleased]

## [0.1.3] — 2026-06-14

Public copy scrub.

- README on PyPI: dropped `## Tests` section, dropped the
  `Source` and `Tests` rows from `## The numbers`, replaced
  `mcptokens hound` examples with generic `python -m
  some_mcp_server`, and removed the `# 1023 wire tokens, 8
  tools` comment under the inspect example.
- CHANGELOG: kept history here so release notes don't pollute
  the repo.

## [0.1.2] — 2026-06-14

PyPI metadata cleanup.

- `project_urls` (Repository / Issues / Changelog) now point at
  the new repo (`dondai1234/mcptokens`). The 0.1.1 wheel had
  them pointing at the abandoned `dondai1234/contextlens`.
- CHANGELOG branch in the URL updated from `master` to `main`.

## [0.1.1] — 2026-06-14

Repo migration.

- Project moved from `dondai1234/contextlens` to a fresh repo,
  `dondai1234/mcptokens`. Source is unchanged; PyPI package name
  is unchanged. New commits only and a single initial commit
  with the full project tree.

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
  Windows when WSAStartup has not been called.
- JSON-RPC id-matching so servers that emit only notifications
  don't ghost-return empty tools.
- Slim CLI: `mcptokens [--json] [--timeout N] [--encoding E]
  <server-argv>...` and `mcptokens serve`.
- Self-cost of the shipped tool definition: **172 tokens** of
  `cl100k_base`, enforced at import via
  `_enforce_self_token_budget()` in `src/mcptokens/_server.py`.
- Tests: subprocess + JSON-RPC id-matching + null-safe coercion
  + timeout + e2e via `mcp.ClientSession` + CLI flag parsing.
