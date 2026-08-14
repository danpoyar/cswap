"""CON-438: ``cswap add`` must register without activating.

A fresh ``claude /login`` replaces the live login; ``cswap add`` then used to
record the fresh account as the active one — an account swap that bypassed the
auto-switch drain path and cold-restarted the prompt cache of every running
agent (live episode 2026-08-14: drain of #27 in progress, ``cswap add`` made
#29 active with no switch event).

Policy under test: ``add`` snapshots the fresh login into its slot and puts
the recorded active account's login back as the live one. Only an explicit
``--activate`` (or the auto-switch drain path itself) may move the live login.
"""

from __future__ import annotations

import json
import logging
import sys
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap.switcher import ClaudeAccountSwitcher


PRIOR_EMAIL = "prior@example.com"
FRESH_EMAIL = "fresh@example.com"


def _seed_live_login(temp_home, switcher, email: str, token: str) -> None:
    """Simulate what ``claude /login`` leaves behind: live config + credentials."""
    config = {
        "oauthAccount": {
            "emailAddress": email,
            "accountUuid": f"uuid-{email}",
            "organizationUuid": "",
            "organizationName": "",
        }
    }
    (temp_home / ".claude.json").write_text(json.dumps(config))
    switcher._write_credentials(
        json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": f"r-{token}"}})
    )


def _live_access_token(switcher) -> str:
    return json.loads(switcher._read_credentials())["claudeAiOauth"]["accessToken"]


@pytest.fixture
def switcher_with_active(temp_home):
    """A switcher with one managed account (slot 1) that is the live login."""
    switcher = ClaudeAccountSwitcher()
    _seed_live_login(temp_home, switcher, PRIOR_EMAIL, "prior-token")
    switcher.add_account()  # first add: becomes account 1, the recorded active
    data = switcher._get_sequence_data()
    assert data["activeAccountNumber"] == 1
    return switcher


class TestAddDoesNotActivate:
    """The CON-438 red tests: add registers the slot, the active login stays."""

    def test_add_fresh_login_keeps_recorded_active_and_restores_live(
        self, temp_home, switcher_with_active
    ):
        """Adding a freshly logged-in account must not displace the active one."""
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")

        switcher.add_account()

        data = switcher._get_sequence_data()
        # The fresh account is registered…
        assert "2" in data["accounts"]
        assert data["accounts"]["2"]["email"] == FRESH_EMAIL
        # …but the recorded active account did not move…
        assert data["activeAccountNumber"] == 1
        # …and the live login went back to the active account.
        assert switcher._get_current_account() == (PRIOR_EMAIL, "")
        assert _live_access_token(switcher) == "prior-token"

    def test_add_does_not_drop_running_sessions_credentials(
        self, temp_home, switcher_with_active
    ):
        """The running park's credential (its warm prompt cache) survives add.

        Running agents hold the active account's token; any change of the live
        credential under them is the cache-burning swap CON-438 forbids. After
        add: same token live, daemon still sees the same active slot, and the
        fresh account is fully registered as a switch candidate for the drain
        path.
        """
        switcher = switcher_with_active
        park_token_before = _live_access_token(switcher)

        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")
        switcher.add_account()

        # The park's credential is untouched — no swap happened under the agents.
        assert _live_access_token(switcher) == park_token_before
        # The auto-switch daemon resolves the same active slot as before…
        assert switcher.current_account_number() == "1"
        # …and the fresh account's snapshot is ready for a drain-path switch.
        stored = switcher._read_account_credentials("2", FRESH_EMAIL)
        assert json.loads(stored)["claudeAiOauth"]["accessToken"] == "fresh-token"

    def test_relogin_to_other_managed_account_does_not_hijack_active(
        self, temp_home, switcher_with_active
    ):
        """The refresh-in-place path must not move the active pointer either."""
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")
        switcher.add_account()  # fresh@ registered as account 2

        # Danila re-logs into the already-managed account 2 and re-adds it.
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token-v2")
        switcher.add_account()

        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1
        assert switcher._get_current_account() == (PRIOR_EMAIL, "")
        # The refreshed credential still landed in the slot's backup.
        stored = switcher._read_account_credentials("2", FRESH_EMAIL)
        assert json.loads(stored)["claudeAiOauth"]["accessToken"] == "fresh-token-v2"

    def test_first_add_still_activates(self, temp_home):
        """With no recorded active account there is nothing to protect: the
        first add keeps making its account the active one."""
        switcher = ClaudeAccountSwitcher()
        _seed_live_login(temp_home, switcher, PRIOR_EMAIL, "prior-token")

        switcher.add_account()

        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1
        assert switcher._get_current_account() == (PRIOR_EMAIL, "")


class TestAddActivateFlag:
    """--activate is the conscious, logged bypass of the drain path."""

    def test_activate_flag_swaps_live_and_leaves_log_trace(
        self, temp_home, switcher_with_active, caplog
    ):
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")

        with caplog.at_level(logging.INFO, logger="claude-swap"):
            switcher.add_account(activate=True)

        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 2
        assert switcher._get_current_account() == (FRESH_EMAIL, "")
        assert _live_access_token(switcher) == "fresh-token"
        assert "--activate" in caplog.text

    def test_cli_add_forwards_activate_flag(self):
        with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["claude-swap", "add", "--activate"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("claude_swap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.add_account.assert_called_once_with(
            slot=None, alias=None, activate=True
        )

    def test_activate_flag_requires_add(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--list", "--activate"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--activate can only be used with 'add'" in capsys.readouterr().err


class TestAddRestoreFallback:
    """When the recorded active account cannot be restored, add falls back to
    the honest state: the fresh login stays live and is recorded as active."""

    def test_unreadable_prior_backup_activates_fresh_account(
        self, temp_home, switcher_with_active, capsys
    ):
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")

        with patch.object(switcher, "_read_account_credentials", return_value=""):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 2
        assert switcher._get_current_account() == (FRESH_EMAIL, "")
        out = capsys.readouterr().out
        assert "Added" in out


class TestAddConcurrencyGuards:
    """The restore and the active-pointer write both re-verify the live state
    under the locks: concurrent daemon switches and Claude Code's own token
    rotation must not be clobbered by stale pre-lock reads."""

    def test_drift_leaves_live_and_pointer_untouched(
        self, temp_home, switcher_with_active, capsys
    ):
        """A concurrent switch mid-add owns the live state — add must not
        restore over it, and must not move the active pointer."""
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")

        with patch.object(switcher, "_live_identity_matches", return_value=False):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert "2" in data["accounts"]  # registration still happened
        assert data["activeAccountNumber"] == 1  # pointer untouched
        # The live login was not overwritten by a restore.
        assert _live_access_token(switcher) == "fresh-token"
        assert "leaving it as is" in capsys.readouterr().out

    def test_restore_resnapshots_rotated_live_into_added_slot(
        self, temp_home, switcher_with_active
    ):
        """The live credential may rotate between add's pre-lock snapshot and
        the restore; the slot backup must carry the current generation, or the
        rotation is destroyed and the slot dies on its next switch."""
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")

        real_matches = switcher._live_identity_matches

        def rotate_then_match(email, org_uuid):
            # Simulates Claude Code's refresh committing a rotated credential
            # just before the restore takes its under-lock snapshot.
            switcher._write_credentials(json.dumps({
                "claudeAiOauth": {
                    "accessToken": "fresh-token-rotated",
                    "refreshToken": "r-fresh-token-rotated",
                }
            }))
            return real_matches(email, org_uuid)

        with patch.object(
            switcher, "_live_identity_matches", side_effect=rotate_then_match
        ):
            switcher.add_account()

        # The rotated generation survived in the slot backup…
        stored = switcher._read_account_credentials("2", FRESH_EMAIL)
        assert json.loads(stored)["claudeAiOauth"]["accessToken"] == "fresh-token-rotated"
        # …and the restore itself still landed on the active account.
        assert _live_access_token(switcher) == "prior-token"
        assert switcher._get_sequence_data()["activeAccountNumber"] == 1

    def test_activation_fallback_skips_pointer_on_drift(
        self, temp_home, switcher_with_active, capsys
    ):
        """Unreadable backup normally activates the fresh login — but not when
        a concurrent switch moved the live state in the meantime."""
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")

        with patch.object(switcher, "_read_account_credentials", return_value=""), \
             patch.object(
                 switcher, "_live_identity_matches", side_effect=[True, False]
             ):
            switcher.add_account()

        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1  # not hijacked
        assert "leaving it as is" in capsys.readouterr().out


class TestSwitchAutoAddMessage:
    """switch()'s auto-add of an unmanaged live login must name the slot the
    login actually landed in — the active pointer no longer follows an add."""

    def test_switch_auto_add_names_the_added_slot(
        self, temp_home, switcher_with_active, capsys
    ):
        switcher = switcher_with_active
        _seed_live_login(temp_home, switcher, FRESH_EMAIL, "fresh-token")

        switcher.switch()

        out = capsys.readouterr().out
        assert "automatically added as Account-2" in out
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == FRESH_EMAIL
        assert data["activeAccountNumber"] == 1
