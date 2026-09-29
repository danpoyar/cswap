"""``cswap offsite`` / ``cswap onsite`` (CON-4019): one network exit point per
account. While an account lives on another machine, this machine keeps its
stored login but never polls, refreshes, switches to or runs it — two
addresses using one login at once looks like a stolen token. The egress
journal (``claude-swap-egress.log``) is the proof: one line per request sent
on an account's behalf, keyed by slot number."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.cli import _translate_subcommand
from claude_swap.exceptions import ConfigError, SessionError, ValidationError
from claude_swap.json_output import USAGE_OFFSITE, account_row, usage_fields
from claude_swap.logging_config import EGRESS_LOG_NAME, setup_logging
from claude_swap.models import Platform
from claude_swap.refresh import OFFSITE, refresh_account
from claude_swap.reseed import ReseedRefusal, reseed_account
from claude_swap.session import SessionManager
from claude_swap.settings import settings_path
from claude_swap.switcher import ClaudeAccountSwitcher

HOST = "148.251.131.11"


def _setup() -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    return s


def _seed(s: ClaudeAccountSwitcher, num: int, email: str) -> None:
    s._write_account_credentials(
        str(num), email,
        json.dumps({"claudeAiOauth": {"accessToken": f"sk-{num}", "refreshToken": f"rt-{num}",
                                      "expiresAt": 9999999999999}}),
    )
    s._write_account_config(
        str(num), email,
        json.dumps({"oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"}}),
    )
    data = s._get_sequence_data() or {"activeAccountNumber": None, "lastUpdated": "",
                                      "sequence": [], "accounts": {}}
    data["accounts"][str(num)] = {"email": email, "uuid": f"uuid-{num}", "organizationUuid": "",
                                  "organizationName": "", "added": "2024-01-01T00:00:00Z"}
    if num not in data["sequence"]:
        data["sequence"].append(num)
        data["sequence"].sort()
    if data["activeAccountNumber"] is None:
        data["activeAccountNumber"] = num
    s._write_json(s.sequence_file, data)


@pytest.fixture
def fleet(temp_home: Path) -> ClaudeAccountSwitcher:  # noqa: ARG001 — temp_home isolates HOME
    s = _setup()
    for num, email in ((1, "a@example.com"), (2, "b@example.com"), (3, "c@example.com")):
        _seed(s, num, email)
    return s


def test_offsite_holds_the_account_out_and_onsite_returns_it(fleet, capsys):
    fleet.set_account_offsite("2", HOST)
    record = fleet._get_sequence_data()["accounts"]["2"]
    assert record["offsite"]["host"] == HOST and record["offsite"]["since"]
    assert fleet.offsite_host("2") == HOST
    assert fleet.offsite_account_numbers() == ["2"]
    assert fleet.switchable_account_numbers() == ["1", "3"]
    assert f"offsite on {HOST}" in capsys.readouterr().out

    fleet.set_account_offsite("b@example.com", None)
    assert "offsite" not in fleet._get_sequence_data()["accounts"]["2"]
    assert fleet.offsite_host("2") is None
    assert fleet.switchable_account_numbers() == ["1", "2", "3"]


def test_active_home_and_live_accounts_stay_on_this_machine(fleet):
    with pytest.raises(ConfigError, match="active login"):
        fleet.set_account_offsite("1", HOST)
    settings_path(fleet.backup_dir).write_text(json.dumps({"autoswitch": {"homeAccount": "3"}}))
    with pytest.raises(ConfigError, match="homeAccount"):
        fleet.set_account_offsite("3", HOST)
    with pytest.raises(ValidationError):
        fleet.set_account_offsite("2", "two words")
    with patch.object(fleet, "_live_session_pids", return_value=[4242]):
        with pytest.raises(SessionError, match="4242"):
            fleet.set_account_offsite("2", HOST)
    assert fleet.offsite_account_numbers() == []


def test_collector_never_fetches_an_offsite_account(fleet):
    fleet.set_account_offsite("2", HOST)
    infos = [(n, e, "", "", False, json.dumps({"claudeAiOauth": {"accessToken": f"sk-{n}",
                                                                   "refreshToken": f"rt-{n}",
                                                                   "expiresAt": 9999999999999}}), "")
             for n, e in ((2, "b@example.com"), (3, "c@example.com"))]
    seen = []

    def fake_fetch(num, *_args, **_kwargs):
        seen.append(num)
        return oauth.UsageOutcome({"five_hour": {"pct": 1.0}, "seven_day": {"pct": 2.0}})

    with patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=fake_fetch):
        entries = fleet._collect_usage_entries(infos)
    assert seen == ["3"]
    assert entries["2"].sentinel == USAGE_OFFSITE
    assert usage_fields(USAGE_OFFSITE) == ("offsite", None)


def test_list_row_names_the_owner_machine():
    row = account_row(2, "b@example.com", "", "", False, USAGE_OFFSITE,
                      offsite={"host": HOST, "since": "2026-09-29T21:00:00Z"})
    assert row["usageStatus"] == "offsite"
    assert row["offsite"] == {"host": HOST, "since": "2026-09-29T21:00:00Z"}
    assert "cswap onsite" in row["usageStatusText"]
    assert "offsite" not in account_row(3, "c@example.com", "", "", False, None)


def test_switch_refuses_an_offsite_account(fleet):
    fleet.set_account_offsite("2", HOST)
    with pytest.raises(ConfigError, match=f"lives on {HOST}"):
        fleet.switch_to("2")
    with pytest.raises(ConfigError, match="cswap onsite 2"):
        fleet.ensure_onsite("2", "b@example.com", "a session")
    fleet.ensure_onsite("3", "c@example.com", "a session")


def test_session_refuses_an_offsite_account(fleet):
    fleet.set_account_offsite("2", HOST)
    manager = SessionManager(fleet)
    with pytest.raises(ConfigError, match=f"lives on {HOST}"):
        manager.setup_session("2", share=True)
    with patch("claude_swap.session.shutil.which", return_value="/usr/bin/true"):
        with pytest.raises(ConfigError, match=f"lives on {HOST}"):
            manager.run("2", [])


def test_refresh_and_reseed_do_not_touch_an_offsite_account(fleet):
    fleet.set_account_offsite("2", HOST)
    with patch("claude_swap.oauth.try_refresh_oauth_credentials") as refresh:
        report = refresh_account(fleet, "2")
    refresh.assert_not_called()
    assert report.outcome == OFFSITE and HOST in (report.detail or "")
    with pytest.raises(ReseedRefusal) as refused:
        reseed_account(fleet, "2")
    assert refused.value.outcome == "offsite"


def test_egress_journal_counts_requests_per_account(tmp_path):
    setup_logging(tmp_path)
    creds = json.dumps({"claudeAiOauth": {"accessToken": "sk-7", "refreshToken": "rt-7",
                                          "expiresAt": 9999999999999}})
    with patch("claude_swap.oauth.request_usage_data", return_value={}):
        oauth.try_fetch_usage_for_account("7", "g@example.com", creds, is_active=False)
    for handler in logging.getLogger("claude-swap.egress").handlers:
        handler.flush()
    lines = (tmp_path / EGRESS_LOG_NAME).read_text().splitlines()
    assert len(lines) == 1 and lines[0].endswith("usage account=7")
    assert "usage account=7" not in (tmp_path / "claude-swap.log").read_text() \
        if (tmp_path / "claude-swap.log").exists() else True


def test_cli_verbs_translate():
    assert _translate_subcommand(["offsite", "2", "--host", HOST]) == ["--offsite-account", "2", "--host", HOST]
    assert _translate_subcommand(["onsite", "2"]) == ["--onsite-account", "2"]
