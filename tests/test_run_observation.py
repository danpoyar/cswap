"""Observe the real CLI-to-exec boundary without launching a model."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.session import SessionManager


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ledger = tmp_path / ".local/state/cswap/command-as-prompt.jsonl"

    class FakeSession:
        def __init__(self, switcher):
            pass

        def run(self, account, tail, **kwargs):
            SessionManager._exec(None, "/fake/claude", tail, {})

        def exec_default(self, tail):
            SessionManager._exec(None, "/fake/claude", tail, {})

    def dispatch(tail, *, default=False):
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher, \
             patch("claude_swap.cli._guard_root"), \
             patch("claude_swap.session.SessionManager", FakeSession), \
             patch("claude_swap.session.sys.platform", "linux"), \
             patch("os.execvpe", side_effect=SystemExit(0)) as execute:
            switcher.return_value.slot_for_directory.return_value = (None, None)
            with pytest.raises(SystemExit):
                cli._run_command([*([] if default else ["28"]), "--", *tail])
            assert execute.call_args.args == ("/fake/claude", ["/fake/claude", *tail], {})
        return [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []

    return dispatch, ledger


@pytest.mark.parametrize("tail,kind", [
    (["claude", "logs", "69a90090"], "claude-logs"),
    (["python3", "-c", "CODE"], "python-c"),
])
def test_incident_observed_before_unchanged_exec(handoff, tail, kind):
    dispatch, ledger = handoff
    rows = dispatch(tail)
    assert len(rows) == 1, "incident reached exec without an observation"
    assert rows[0]["kind"] == kind
    assert rows[0]["argc"] == len(tail)
    assert set(rows[0]) == {"id", "timestamp", "kind", "argc"}
    assert tail[-1] not in ledger.read_text()


@pytest.mark.parametrize("tail", [
    [], ["--resume"], ["auth", "status"], ["--no-share"],
    ["-p", "python3 -c CODE"], ["--model", "example", "claude"],
    ["claude", "-p", "ok", "--max-turns", "1"],
    ["Tell me about python3"], ["claude logs 69a90090"],
    ["python3"], ["python3", "is", "useful"], ["claude", "logs"],
])
def test_legal_tails_unchanged_without_observation(handoff, tail):
    dispatch, _ = handoff
    assert dispatch(tail) == []


def test_default_route_and_append(handoff):
    dispatch, _ = handoff
    dispatch(["python3", "-c", "first"])
    rows = dispatch(["python3", "-c", "second"], default=True)
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]


def test_unwritable_ledger_reports_failure_but_preserves_exec(handoff, capsys):
    dispatch, ledger = handoff
    ledger.parent.parent.mkdir(parents=True)
    ledger.parent.write_text("not a directory")
    assert dispatch(["python3", "-c", "SECRET"]) == []
    err = capsys.readouterr().err
    assert "cswap-command-as-prompt: observation unavailable" in err
    assert "SECRET" not in err


def test_windows_handoff_preserves_argv_and_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tail = ["python3", "-c", "CODE"]
    with patch("claude_swap.session.sys.platform", "win32"), \
         patch("claude_swap.session.subprocess.run") as execute:
        execute.return_value.returncode = 7
        with pytest.raises(SystemExit) as result:
            SessionManager._exec(None, "claude.exe", tail, {"A": "B"})
    assert result.value.code == 7
    execute.assert_called_once_with(["claude.exe", *tail], env={"A": "B"})
    rows = (tmp_path / ".local/state/cswap/command-as-prompt.jsonl").read_text().splitlines()
    assert len(rows) == 1
