"""Test the CLI surface. Two verbs: spawn-mode, serve-mode.

We test against a fake stdio MCP server script written to tmp.
"""
from __future__ import annotations

import io
import json
import sys
import textwrap
from contextlib import redirect_stdout, redirect_stderr

import pytest

from mcptokens import cli
from mcptokens.cli import main, _split_flags, _run_inspect


_FAKE = textwrap.dedent(
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
                "serverInfo": {"name": "fake", "version": "0.1"},
                "capabilities": {"tools": {}},
            }))
        elif method == "tools/list":
            sys.stdout.write(frame(msg["id"], {
                "tools": [{
                    "name": "noop",
                    "description": "no-op",
                    "inputSchema": {"type": "object"},
                }],
            }))
        sys.stdout.flush()
    """
)


# --- Argument parsing --------------------------------------------------


def test_split_flags_consumes_leading_known_flags():
    flags, cmd = _split_flags(["--json", "--timeout", "5", "hound"])
    assert flags["json"] is True
    assert flags["timeout"] == 5.0
    assert cmd == ["hound"]


def test_split_flags_consume_no_flags_yields_empty_cmd():
    flags, cmd = _split_flags([])
    assert flags["json"] is False
    assert cmd == []


def test_split_flags_stops_at_unknown_token():
    """Hound's `--cache-ttl` is a server flag, not our flag. The
    parsing rule is: known mcptokens flags (and their values) get
    consumed. The first token that's not a known flag is the
    boundary; everything from there is the spawn cmd."""
    flags, cmd = _split_flags(
        ["--json", "--timeout", "10", "python", "-m", "hound", "--cache-ttl", "60"]
    )
    assert flags["json"] is True
    assert flags["timeout"] == 10.0
    assert cmd == ["python", "-m", "hound", "--cache-ttl", "60"]


def test_split_flags_rejects_bad_timeout():
    import pytest
    with pytest.raises(cli._ArgError, match="timeout"):
        _split_flags(["--timeout", "abc"])


def test_split_flags_rejects_unsupported_encoding():
    with pytest.raises(cli._ArgError, match="encoding"):
        _split_flags(["--encoding", "bogus"])


def test_split_flags_rejects_out_of_range_timeout():
    with pytest.raises(cli._ArgError, match="outside"):
        _split_flags(["--timeout", "0"])
    with pytest.raises(cli._ArgError, match="outside"):
        _split_flags(["--timeout", "120"])


# --- main() entry ------------------------------------------------------


def test_main_version_only(capsys):
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcptokens" in out
    # Pin: pin to the running version. Drift between __init__
    # version and CLI test string is a smell.
    import mcptokens
    assert mcptokens.__version__ in out


def test_main_empty_argv_exits_zero(capsys):
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Ultra-light" in out or "usage" in out.lower()


def test_main_unknown_flag_at_front_exits_two(capsys):
    rc = main(["--definitely-not-a-flag"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--definitely-not-a-flag" in err  # error message mentions it


def test_main_help_exits_zero(capsys):
    """--help exits 0 with the help body printed."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


# --- spawn-mode --------------------------------------------------------


def test_run_inspect_happy_path(tmp_path, capsys):
    fake = tmp_path / "fake.py"
    fake.write_text(_FAKE, encoding="utf-8")
    rc = _run_inspect([sys.executable, str(fake)], timeout=5.0, encoding="cl100k_base", as_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "WIRE TOTAL" in out
    assert "noop" in out


def test_run_inspect_json_pipe(tmp_path, capsys):
    fake = tmp_path / "fake.py"
    fake.write_text(_FAKE, encoding="utf-8")
    rc = _run_inspect([sys.executable, str(fake)], timeout=5.0, encoding="cl100k_base", as_json=True)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["tool_count"] == 1


def test_run_inspect_failure_returns_one(tmp_path, capsys):
    rc = _run_inspect(
        [str(tmp_path / "missing-binary")], timeout=2.0, encoding="cl100k_base", as_json=False
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err.lower() or "spawn failed" in err.lower()


def test_run_inspect_empty_cmd_exits_two(capsys):
    rc = _run_inspect([], timeout=5.0, encoding="cl100k_base", as_json=False)
    assert rc == 2


def test_run_inspect_json_failure(tmp_path, capsys):
    rc = _run_inspect(
        [str(tmp_path / "missing-binary")], timeout=2.0, encoding="cl100k_base", as_json=True
    )
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "error" in payload
