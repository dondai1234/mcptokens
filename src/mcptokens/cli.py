"""mcptokens CLI. Two verbs:

    mcptokens [--json] [--timeout N] [--encoding E] <server-argv...>
        Spawn one stdio MCP server, count its tokens. Flags are
        consumed only when they appear BEFORE the spawn argv.

    mcptokens serve
        Run as an MCP server (the product's primary mode).

    mcptokens --version | --help
"""
from __future__ import annotations

import json
import sys
from typing import Optional, Sequence

import mcptokens
from mcptokens._engine import (
    DEFAULT_ENCODING,
    DEFAULT_TIMEOUT_SECONDS,
    SUPPORTED_ENCODINGS,
    InspectError,
    inspect_server,
)

# Documented exit codes.
_EXIT_OK = 0
_EXIT_INSPECT = 1
_EXIT_ARG = 2


def _help_text() -> str:
    return (
        "mcptokens — Ultra-light MCP server for tool-def token counting.\n"
        "\n"
        "Usage:\n"
        "  mcptokens                          Show this help and exit 0.\n"
        "  mcptokens --version                Print version and exit 0.\n"
        "  mcptokens [--json] [--timeout N]   Spawn an MCP server argv and\n"
        "                  [--encoding E]     inspect its tool-def tokens.\n"
        "                  <server-argv...>   Flags must come before the\n"
        "                                      spawn argv.\n"
        "  mcptokens serve                    Run as an MCP server.\n"
        "\n"
        "Examples:\n"
        "  mcptokens hound                    # show wire total + per-tool\n"
        "  mcptokens --json hound             # JSON, for piping\n"
        "  mcptokens --timeout 30 python -m some_server\n"
        "\n"
        "One tool exposed when `mcptokens serve`: `inspect`. The MCP server\n"
        "is the product — the CLI is a thin human-side debug hatch.\n"
    )


def _split_flags(raw: list[str]) -> tuple[dict, list[str]]:
    """Walk `raw`. Take leading `--json`, `--timeout N`, `--encoding E`,
    `--version` as our flags. Stop scanning the moment we hit anything
    else (positional, unknown flag, server-looking token). Everything
    from that point is the spawn argv.

    Returning:
        flags: {json, timeout, encoding, version}
        cmd: list[str] of spawn argv

    Anybody writing `mcptokens <known-flag> <boundary>` gets the flag.
    Anybody writing `mcptokens hound --json` is treating --json as a
    flag for the server being spawned (correct: their mental model
    is "the rest is the server's argv").
    """
    flags = {
        "json": False,
        "timeout": DEFAULT_TIMEOUT_SECONDS,
        "encoding": DEFAULT_ENCODING,
        "version": False,
    }
    i = 0
    while i < len(raw):
        a = raw[i]
        if a == "--json":
            flags["json"] = True
            i += 1
        elif a == "--version":
            flags["version"] = True
            i += 1
        elif a in ("-h", "--help"):
            print(_help_text())
            sys.exit(_EXIT_OK)
        elif a == "--timeout":
            if i + 1 >= len(raw):
                raise _ArgError("--timeout needs a value, e.g. --timeout 15")
            try:
                flags["timeout"] = float(raw[i + 1])
            except ValueError:
                raise _ArgError(f"--timeout {raw[i + 1]!r} is not a number")
            if not (0 < flags["timeout"] <= 60):
                raise _ArgError(f"--timeout {flags['timeout']} is outside (0, 60]")
            i += 2
        elif a == "--encoding":
            if i + 1 >= len(raw):
                raise _ArgError(
                    "--encoding needs a value, e.g. --encoding cl100k_base"
                )
            enc = raw[i + 1]
            if enc not in SUPPORTED_ENCODINGS:
                raise _ArgError(
                    f"--encoding {enc!r} is not supported. "
                    f"Pick one of {list(SUPPORTED_ENCODINGS)}."
                )
            flags["encoding"] = enc
            i += 2
        elif a.startswith("--"):
            # Unknown long-form flag at the front. The user clearly
            # meant it as a mcptokens flag; reject it's a typo fast
            # rather than forward it to the server as spawn argv.
            raise _ArgError(
                f"unknown flag: {a}. Try --json, --timeout, --encoding, --version, --help."
            )
        else:
            # First non-flag token. Boundary: everything from here
            # is the spawn argv (including things like `-m` for
            # `python -m hound`, or `--cache-ttl 60` for hound).
            return flags, raw[i:]
    return flags, []


class _ArgError(Exception):
    """Internal: argument parsing fails with a clean user-facing message."""


def _run_serve() -> int:
    from mcptokens._server import run_server
    return run_server()


def _print_human(report) -> str:
    name_w = max(len("TOOL"), max((len(t.name) for t in report.tools), default=4))
    sep_w = name_w + 38
    lines = [
        f"# {report.server}   ({report.encoding}, {report.elapsed_ms} ms)",
        f"{'TOOL':<{name_w}}  {'NAME':>5}  {'DESC':>5}  {'SCHEMA':>7}  {'ANNOT':>5}  {'TOTAL':>7}",
        "-" * sep_w,
    ]
    for t in report.tools:
        lines.append(
            f"{t.name:<{name_w}}  {t.name_tokens:>5d}  {t.description_tokens:>5d}  "
            f"{t.schema_tokens:>7d}  {t.annotations_tokens:>5d}  {t.total_tokens:>7d}"
        )
    parts = sum(t.total_tokens for t in report.tools)
    lines.append("-" * sep_w)
    lines.append(
        f"{'PER-TOOL SUBTOTAL':<{name_w}}  {'':<5}  {'':<5}  {'':<7}  {'':<5}  {parts:>7d}"
    )
    lines.append(
        f"{'WIRE TOTAL':<{name_w}}  {'':<5}  {'':<5}  {'':<7}  {'':<5}  {report.wire_total_tokens:>7d}"
    )
    return "\n".join(lines)


def _run_inspect(cmd: list[str], *, timeout: float, encoding: str, as_json: bool) -> int:
    if not cmd:
        print(
            "mcptokens: empty spawn command. Pass one, e.g. `mcptokens hound`.",
            file=sys.stderr,
        )
        return _EXIT_ARG
    try:
        report = inspect_server(
            cmd, encoding=encoding, timeout_seconds=timeout, version=mcptokens.__version__
        )
    except InspectError as exc:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "server": " ".join(cmd), "error": str(exc)},
                    indent=2,
                )
            )
        else:
            print(f"mcptokens: {exc}", file=sys.stderr)
        return _EXIT_INSPECT
    if as_json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(_print_human(report))
    return _EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    if raw == []:
        print(_help_text())
        return _EXIT_OK

    try:
        flags, cmd = _split_flags(raw)
    except _ArgError as exc:
        print(f"mcptokens: {exc}", file=sys.stderr)
        print(f"\nFor help: `mcptokens --help`.", file=sys.stderr)
        return _EXIT_ARG

    if flags["version"] and not cmd:
        print(f"mcptokens {mcptokens.__version__}")
        return _EXIT_OK

    if cmd == ["serve"]:
        return _run_serve()

    return _run_inspect(
        cmd,
        timeout=flags["timeout"],
        encoding=flags["encoding"],
        as_json=flags["json"],
    )
