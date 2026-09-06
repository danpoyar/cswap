"""Forensic snapshot at ``adopt-real-login`` (CON-2323).

On 05-09 the daemon adopted 32→23 three times in 28 minutes: something wrote
slot 23's ``oauthAccount`` into ``~/.claude.json`` without a switch and
without a hand, and every trace was gone by the time a human looked.
``collect_adopt_snapshot`` captures the moment — every Claude Code process
with its ``CLAUDE_CONFIG_DIR``/``HOME``, the config file's mtime and
identity, the Keychain items' mdat, whoever holds the file open — and never
raises, never carries a secret.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claude_swap import adopt_snapshot
from claude_swap.adopt_snapshot import (
    collect_adopt_snapshot,
    count_ps_lines,
    default_adopt_snapshot,
    parse_lsof,
    parse_ps_claude,
    parse_security_mdat,
)
from claude_swap.session import keychain_service_name

PS_OUT = """\
  101     1 Sat Sep  5 09:48:00 2026 /Users/u/.local/bin/claude --bg CLAUDE_CONFIG_DIR=/tmp/prof/23-x HOME=/Users/u PATH=/x
  102   101 Sat Sep  5 09:48:01 2026 claude daemon run --origin transient CLAUDE_CONFIG_DIR=/tmp/prof/23-x HOME=/Users/u
  103     1 Fri Sep  4 22:50:37 2026 /Users/u/.local/share/claude/versions/2.1.261 --bg-spare HOME=/Users/u PATH=/x
  104     1 Fri Sep  4 22:50:37 2026 /bin/zsh -c source snapshot.sh CLAUDE_CONFIG_DIR=/tmp/prof/23-x
  105   103 Sat Sep  5 10:00:00 2026 /Applications/ClaudeCode.app/Contents/MacOS/claude --bg-pty-host x CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-SECRET HOME=/Users/u
  106     1 Sat Sep  5 10:00:00 2026 /usr/bin/python3 /Users/u/bin/cswap auto CLAUDE_CONFIG_DIR=/tmp/prof/32-y
"""

# What a localized ps (LC_ALL=es_ES.UTF-8) prints for lstart — review r1 ran
# it live: 0 of 948 lines matched and the snapshot said nothing.
PS_OUT_ES = """\
  101     1 dom.  6 sept. 09:48:00 2026 /Users/u/.local/bin/claude --bg CLAUDE_CONFIG_DIR=/tmp/prof/23-x HOME=/Users/u
  103     1 vie.  5 sept. 22:50:37 2026 claude daemon run HOME=/Users/u
"""

SECURITY_OUT = """\
keychain: "/Users/u/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="u"
    "cdat"<timedate>=0x32303236303831353038343930345A00  "20260815084904Z\\000"
    "mdat"<timedate>=0x32303236303930353233353830375A00  "20260905235807Z\\000"
    "svce"<blob>="Claude Code-credentials"
"""


class TestParsers:
    def test_ps_keeps_only_claude_binaries_and_reads_their_env(self):
        procs = parse_ps_claude(PS_OUT)
        assert [p["pid"] for p in procs] == [101, 102, 103, 105]
        by = {p["pid"]: p for p in procs}
        assert by[101]["ppid"] == 1
        assert by[101]["started"] == "Sat Sep  5 09:48:00 2026"
        assert by[101]["configDir"] == "/tmp/prof/23-x"
        assert by[101]["home"] == "/Users/u"
        assert by[103]["configDir"] is None
        assert by[105]["tokenEnv"] is True
        assert by[101]["tokenEnv"] is False
        # argv only — never the environment (a token lives there).
        assert by[105]["command"].startswith("/Applications/ClaudeCode.app")
        assert "SECRET" not in json.dumps(procs)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in by[105]["command"]

    def test_ps_caps_the_list(self):
        many = "".join(
            f"  {1000 + i}     1 Sat Sep  5 09:48:00 2026 claude --bg HOME=/Users/u\n"
            for i in range(400)
        )
        procs = parse_ps_claude(many, cap=50)
        assert len(procs) == 50

    def test_security_mdat_is_iso_utc(self):
        assert parse_security_mdat(SECURITY_OUT) == "2026-09-05T23:58:07Z"
        assert parse_security_mdat("no such attribute") is None

    def test_count_ps_lines_sees_only_the_c_locale_format(self):
        assert count_ps_lines(PS_OUT) == 6
        assert count_ps_lines(PS_OUT_ES) == 0
        assert count_ps_lines("") == 0

    def test_lsof_field_output(self):
        assert parse_lsof("p123\ncclaude\np456\ncjq\n") == [
            {"pid": 123, "command": "claude"},
            {"pid": 456, "command": "jq"},
        ]
        assert parse_lsof("") == []


def _runner_factory(outputs: dict, *, raise_for: set | None = None):
    calls: list[list[str]] = []
    envs: dict[str, dict | None] = {}

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        name = Path(cmd[0]).name
        envs[name] = kwargs.get("env")
        if raise_for and name in raise_for:
            raise OSError(f"{name} missing")
        stdout, rc, *rest = outputs.get(name, ("", 1))
        stderr = rest[0] if rest else ""
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)

    runner.calls = calls  # type: ignore[attr-defined]
    runner.envs = envs  # type: ignore[attr-defined]
    return runner


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": "claude@amouen.com",
            "accountUuid": "df7f4e78-1111-2222-3333-444444444444",
            "organizationUuid": "f10a4593-aaaa-bbbb-cccc-dddddddddddd",
        },
        "projects": {"/a": {}, "/b": {}},
    }))
    return cfg


class TestCollect:
    def test_full_snapshot(self, config_file: Path, tmp_path: Path):
        session_dir = tmp_path / "prof" / "23-x"
        session_dir.mkdir(parents=True)
        (session_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude@amouen.com"},
        }))
        (config_file.parent / ".claude.json.lock").mkdir()
        runner = _runner_factory({
            "ps": (PS_OUT.replace("/tmp/prof/23-x", str(session_dir)), 0),
            "security": (SECURITY_OUT, 0),
            "lsof": ("p777\ncclaude\n", 0),
        })
        snap = collect_adopt_snapshot(
            config_file,
            session_dir=session_dir,
            keychain_account="u",
            profile_service="Claude Code-credentials-f9b6a171",
            runner=runner,
            platform="darwin",
        )
        assert snap["errors"] == []
        cf = snap["configFile"]
        assert cf["exists"] is True
        assert cf["oauthEmail"] == "claude@amouen.com"
        assert cf["accountUuid8"] == "df7f4e78"
        assert cf["projects"] == 2
        assert cf["mtime"].endswith("Z")
        assert snap["lock"]["exists"] is True
        assert snap["profileConfig"]["oauthEmail"] == "claude@amouen.com"
        assert snap["keychainLive"] == {
            "service": "Claude Code-credentials",
            "present": True,
            "mdat": "2026-09-05T23:58:07Z",
        }
        assert snap["keychainProfile"]["service"] == "Claude Code-credentials-f9b6a171"
        assert snap["keychainProfile"]["mdat"] == "2026-09-05T23:58:07Z"
        pr = snap["processes"]
        assert pr["claude"] == 4
        assert pr["withoutConfigDir"] == [103, 105]
        assert pr["withTokenEnv"] == [105]
        assert pr["onAdoptedProfile"] == [101, 102]
        assert [p["pid"] for p in pr["list"]] == [101, 102, 103, 105]
        assert snap["openers"] == [{"pid": 777, "command": "claude"}]
        assert isinstance(snap["elapsedMs"], int)
        assert "SECRET" not in json.dumps(snap)
        # The Keychain is asked for attributes only — never the secret (-w).
        for call in runner.calls:
            if Path(call[0]).name == "security":
                assert "-w" not in call
                assert call[1] == "find-generic-password"

    def test_ps_runs_in_the_c_locale(self, config_file: Path):
        runner = _runner_factory({"ps": (PS_OUT, 0), "security": (SECURITY_OUT, 0), "lsof": ("", 1)})
        collect_adopt_snapshot(
            config_file, session_dir=None, keychain_account="u",
            runner=runner, platform="darwin",
        )
        env = runner.envs["ps"]
        assert env["LC_ALL"] == "C" and env["LANG"] == "C"
        assert "PATH" in env  # the rest of the environment is inherited

    def test_localized_ps_output_is_an_error_not_silence(self, config_file: Path):
        # Review r1 (major): a non-C locale parsed nothing and reported nothing.
        runner = _runner_factory({"ps": (PS_OUT_ES, 0), "security": (SECURITY_OUT, 0), "lsof": ("", 1)})
        snap = collect_adopt_snapshot(
            config_file, session_dir=None, keychain_account="u",
            runner=runner, platform="darwin",
        )
        assert snap["processes"]["claude"] == 0
        assert any(e.startswith("ps: parsed 0 of 2 lines") for e in snap["errors"])

    def test_ps_nonzero_exit_is_an_error_line(self, config_file: Path):
        runner = _runner_factory({"ps": ("", 1, "ps: illegal option"), "security": (SECURITY_OUT, 0), "lsof": ("", 1)})
        snap = collect_adopt_snapshot(
            config_file, session_dir=None, keychain_account="u",
            runner=runner, platform="darwin",
        )
        assert snap["processes"]["list"] == []
        assert "ps: rc=1: ps: illegal option" in snap["errors"]

    def test_locked_keychain_is_an_error_not_an_absent_item(self, config_file: Path):
        # rc 44 = errSecItemNotFound (absent, silent); any other rc is a failed probe.
        runner = _runner_factory({
            "ps": ("", 0),
            "security": ("", 36, "security: SecKeychainSearchCopyNext: User interaction is not allowed."),
            "lsof": ("", 1),
        })
        snap = collect_adopt_snapshot(
            config_file, session_dir=None, keychain_account="u",
            runner=runner, platform="darwin",
        )
        assert snap["keychainLive"]["present"] is None
        assert snap["keychainLive"]["mdat"] is None
        assert any(e.startswith("security: rc=36: security: SecKeychainSearchCopyNext") for e in snap["errors"])

    def test_missing_tools_are_errors_not_exceptions(self, config_file: Path):
        runner = _runner_factory({}, raise_for={"ps", "security", "lsof"})
        snap = collect_adopt_snapshot(
            config_file, session_dir=None, keychain_account="u",
            runner=runner, platform="darwin",
        )
        assert snap["configFile"]["oauthEmail"] == "claude@amouen.com"
        assert snap["processes"]["list"] == []
        assert snap["openers"] == []
        assert snap["keychainLive"]["present"] is None
        assert any(e.startswith("ps:") for e in snap["errors"])
        assert any(e.startswith("security:") for e in snap["errors"])
        assert any(e.startswith("lsof:") for e in snap["errors"])

    def test_absent_keychain_item_and_config(self, tmp_path: Path):
        runner = _runner_factory({
            "ps": ("", 0),
            "security": ("security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.", 44),
            "lsof": ("", 1),
        })
        snap = collect_adopt_snapshot(
            tmp_path / "missing.json", session_dir=tmp_path / "nope",
            keychain_account="u", runner=runner, platform="darwin",
        )
        assert snap["configFile"] == {"path": str(tmp_path / "missing.json"), "exists": False}
        assert snap["keychainLive"]["present"] is False
        assert snap["keychainLive"]["mdat"] is None
        assert snap["profileConfig"]["exists"] is False
        assert snap["processes"]["claude"] == 0
        assert snap["errors"] == []

    def test_timeout_is_an_error_line(self, config_file: Path):
        def runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

        snap = collect_adopt_snapshot(
            config_file, session_dir=None, keychain_account="u",
            runner=runner, platform="darwin",
        )
        assert snap["processes"]["list"] == []
        assert any("timed out" in e for e in snap["errors"])

    def test_non_macos_skips_keychain(self, config_file: Path):
        runner = _runner_factory({"ps": ("", 0), "lsof": ("", 0)})
        snap = collect_adopt_snapshot(
            config_file, session_dir=None, keychain_account="u",
            runner=runner, platform="linux",
        )
        assert snap["keychainLive"] == {"service": "Claude Code-credentials", "present": None, "mdat": None, "skipped": "not macOS"}
        assert all(Path(c[0]).name != "security" for c in runner.calls)


class TestDefaultCollector:
    """The daemon's collector wires the live tool names the tests stub."""

    def test_default_passes_profile_service_and_account(self, tmp_path: Path, monkeypatch):
        seen: dict = {}

        def fake_collect(config_path, **kwargs):
            seen["config_path"] = config_path
            seen.update(kwargs)
            return {"ok": True}

        monkeypatch.setattr(adopt_snapshot, "collect_adopt_snapshot", fake_collect)
        monkeypatch.setattr(adopt_snapshot.macos_keychain, "keychain_account_name", lambda: "u")
        session_dir = tmp_path / "prof" / "23-x"
        out = default_adopt_snapshot(
            config_path=tmp_path / ".claude.json", session_dir=session_dir, prior=None, to_ref={}
        )
        assert out == {"ok": True}
        assert seen["config_path"] == tmp_path / ".claude.json"
        assert seen["session_dir"] == session_dir
        assert seen["keychain_account"] == "u"
        assert seen["profile_service"] == keychain_service_name(session_dir)

    def test_default_without_profile(self, tmp_path: Path, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(
            adopt_snapshot, "collect_adopt_snapshot",
            lambda config_path, **kw: seen.update(kw) or {},
        )
        monkeypatch.setattr(adopt_snapshot.macos_keychain, "keychain_account_name", lambda: "u")
        default_adopt_snapshot(config_path=tmp_path / ".claude.json", session_dir=None)
        assert seen["profile_service"] is None
        assert seen["session_dir"] is None
