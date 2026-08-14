"""Tests for the drain-v2 park channel (park.py): roster parsing, herald
wave spawning/report parsing, binary resolution, and env hygiene. All
subprocess work is injected — no test ever spawns a real ``claude``."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from claude_swap.park import (
    HERALD_MODEL,
    ParkChannel,
    ParkSession,
    WaveResult,
    _parse_wave_output,
)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


class RecordingRun:
    """Stands in for subprocess.run: records calls, replays scripted results."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


ROSTER_ROWS = [
    {
        "pid": 100,
        "cwd": "/w/a",
        "kind": "background",
        "sessionId": "sid-a",
        "name": "fix-a",
        "status": "busy",
        "state": "working",
    },
    {
        "pid": 101,
        "cwd": "/w/b",
        "kind": "interactive",
        "sessionId": "sid-b",
        "name": "Yor",
        "status": "idle",
    },
    {"pid": 102, "cwd": "/w/c", "kind": "background"},  # nameless: skipped
    "garbage",
]


class TestRoster:
    def test_parses_rows_and_skips_unaddressable(self):
        run = RecordingRun([_proc(stdout=json.dumps(ROSTER_ROWS))])
        channel = ParkChannel(claude_bin="/bin/claude", run=run)
        roster = channel.roster()
        assert roster == [
            ParkSession(
                name="fix-a",
                session_id="sid-a",
                kind="background",
                status="busy",
                state="working",
                pid=100,
            ),
            ParkSession(
                name="Yor",
                session_id="sid-b",
                kind="interactive",
                status="idle",
                state=None,
                pid=101,
            ),
        ]
        assert roster[0].executing and not roster[1].executing
        assert run.calls[0]["argv"] == ["/bin/claude", "agents", "--json"]

    def test_failure_modes_return_none(self):
        cases = [
            _proc(returncode=1, stderr="boom"),
            _proc(stdout="not json"),
            _proc(stdout='{"an": "object"}'),
            subprocess.TimeoutExpired(cmd="claude", timeout=20.0),
            OSError("spawn failed"),
        ]
        for case in cases:
            channel = ParkChannel(claude_bin="/bin/claude", run=RecordingRun([case]))
            assert channel.roster() is None

    def test_no_binary_returns_none_without_spawning(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("os.path.isfile", lambda p: False)
        run = RecordingRun([])
        channel = ParkChannel(run=run)
        assert channel.roster() is None
        assert run.calls == []


class TestWave:
    def _envelope(self, result: str, is_error: bool = False) -> str:
        return json.dumps({"type": "result", "is_error": is_error, "result": result})

    def test_sends_one_herald_with_message_and_targets(self):
        report = json.dumps({"sent": ["fix-a", "fix-b"], "failed": {}})
        run = RecordingRun([_proc(stdout=self._envelope(report))])
        channel = ParkChannel(claude_bin="/bin/claude", run=run)
        result = channel.send_wave(["fix-a", "fix-b"], "замри и жди")
        assert result == WaveResult(ok=True, delivered=["fix-a", "fix-b"])
        call = run.calls[0]
        assert call["argv"][0:2] == ["/bin/claude", "-p"]
        prompt = call["argv"][2]
        assert "замри и жди" in prompt
        assert "- fix-a" in prompt and "- fix-b" in prompt
        assert call["argv"][3:] == [
            "--allowedTools",
            "SendMessage",
            "--output-format",
            "json",
            "--model",
            HERALD_MODEL,
        ]

    def test_child_env_drops_claude_and_auth_vars(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/x.sock")
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nope")
        monkeypatch.setenv("HARMLESS", "keep")
        run = RecordingRun([_proc(stdout=self._envelope("{}"))])
        channel = ParkChannel(claude_bin="/bin/claude", run=run)
        channel.send_wave(["fix-a"], "msg")
        env = run.calls[0]["env"]
        assert "CLAUDE_CODE_MESSAGING_SOCKET" not in env
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CONFIG_DIR" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert env["HARMLESS"] == "keep"

    def test_empty_targets_skip_the_spawn(self):
        run = RecordingRun([])
        channel = ParkChannel(claude_bin="/bin/claude", run=run)
        result = channel.send_wave([], "msg")
        assert result.ok is True and result.delivered == []
        assert run.calls == []

    def test_spawn_failures_are_channel_failures(self):
        cases = [
            subprocess.TimeoutExpired(cmd="claude", timeout=120.0),
            OSError("no such file"),
            _proc(returncode=2, stderr="exploded"),
            _proc(stdout="not json at all"),
            _proc(stdout=self._envelope("nope", is_error=True)),
        ]
        for case in cases:
            channel = ParkChannel(claude_bin="/bin/claude", run=RecordingRun([case]))
            result = channel.send_wave(["fix-a"], "msg")
            assert result.ok is False, case
            assert result.detail

    def test_unparseable_report_is_unconfirmed_not_failed(self):
        for result_text in ("done!", "{broken json", ""):
            channel = ParkChannel(
                claude_bin="/bin/claude",
                run=RecordingRun([_proc(stdout=self._envelope(result_text))]),
            )
            result = channel.send_wave(["fix-a"], "msg")
            assert result.ok is True
            assert result.delivered is None

    def test_report_amid_prose_is_extracted(self):
        text = 'All sent. {"sent": ["fix-a"], "failed": {"fix-b": "no route"}} Bye.'
        result = _parse_wave_output(json.dumps({"result": text}))
        assert result.ok is True
        assert result.delivered == ["fix-a"]
        assert result.failed == {"fix-b": "no route"}


class TestBinaryResolution:
    def test_explicit_bin_wins(self):
        channel = ParkChannel(claude_bin="/custom/claude", run=RecordingRun([]))
        assert channel.binary() == "/custom/claude"

    def test_which_then_fallbacks(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/from/path/claude")
        assert ParkChannel(run=RecordingRun([])).binary() == "/from/path/claude"

    def test_resolution_is_cached(self, monkeypatch):
        calls = []

        def fake_which(name):
            calls.append(name)
            return "/from/path/claude"

        monkeypatch.setattr("shutil.which", fake_which)
        channel = ParkChannel(run=RecordingRun([]))
        assert channel.binary() == channel.binary()
        assert calls == ["claude"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
