"""Two more consumers of one token family heal the consumed backup (CON-1595).

CON-1579 taught ``switch`` that a slot backup is a CONSUMED generation once a
``cswap run`` session rotated the family inside its profile — and made the
switch heal or refuse. Two other readers of the same backup were left blind:

- the auto-switch engine's ``_freshen_target`` refreshed a candidate from the
  BACKUP: for a slot with a rotated profile and no live session that POSTs a
  consumed grant, the server answers ``invalid_grant``, and the engine
  quarantined a perfectly alive slot (a false quarantine, and one candidate
  fewer for the rotation);
- ``cswap refresh`` judged the PROFILE's freshness only: a fresh profile over
  a dead backup was ``fresh`` ("nothing to touch"), so the divergence the
  fleet's refresh job runs into every 10 minutes was never named or healed,
  and ``cswap list --json`` carried no machine-readable trace of it.

Both now go through the same pre-activation heal (``refresh.heal_backup_before_activation``)
or its resync shape, and ``list --json --token-status`` reports the family
state so the fleet's sensors can read the signal the human output already
printed ("session profile: fresh / stored backup: expired").
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from claude_swap import cli, oauth
from claude_swap.autoswitch import QuarantineEvent, TickOutcome
from claude_swap.oauth import RefreshOutcome
from claude_swap.refresh import FRESH, LIVE_SESSION, RESYNCED, refresh_account
from claude_swap.session import SEED_FINGERPRINT_FILE, STALE_MARKER
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, _usage

EXPIRED_MS = 1_000  # 1970: long expired
FRESH_MS = 32_503_680_000_000  # far future
NUM, EMAIL = "2", "b@example.com"


def _creds(access: str, refresh: str, expires: int = FRESH_MS) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": expires,
            }
        }
    )


def _rotated_profile(
    switcher: ClaudeAccountSwitcher,
    num: str = NUM,
    email: str = EMAIL,
    profile_expires: int = FRESH_MS,
):
    """The incident shape: backup = consumed seed generation (expired), the
    session profile holds the rotated successor, no live session."""
    backup = _creds(f"at-seed-{num}", f"rt-seed-{num}", expires=EXPIRED_MS)
    switcher.write_account_credentials(num, email, backup)
    session_dir = switcher._session_dir(num, email)
    session_dir.mkdir(parents=True)
    profile = _creds(f"at-profile-{num}", f"rt-profile-{num}", expires=profile_expires)
    (session_dir / ".credentials.json").write_text(profile, encoding="utf-8")
    (session_dir / SEED_FINGERPRINT_FILE).write_text(
        oauth.credential_fingerprint(backup), encoding="utf-8"
    )
    return backup, profile, session_dir


@pytest.fixture
def no_network():
    """Identity oracle and token endpoint never leave the test. ``refresh.py``
    binds the refresh name at import time — patch its reference too."""
    with (
        patch("claude_swap.oauth.fetch_oauth_profile", return_value=None),
        patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
        patch("claude_swap.refresh.try_refresh_oauth_credentials", post),
    ):
        # What the server says to a consumed grant — the answer the engine
        # used to turn into a false quarantine.
        post.return_value = RefreshOutcome(None, "invalid_grant")
        yield post


@pytest.fixture
def harness(temp_home) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, EMAIL)
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestFreshenHealsRotatedBackup:
    """``_freshen_target`` reads the backup — heal it from the profile first."""

    def test_fresh_profile_generation_is_adopted_without_a_post(
        self, harness, no_network
    ):
        """RED on main: the consumed backup grant is POSTed → invalid_grant."""
        _backup, profile, _dir = _rotated_profile(harness.switcher)

        status = harness.engine._freshen_target(NUM, EMAIL)

        assert status == "ok"
        no_network.assert_not_called()  # adopted, no grant consumed
        assert harness.switcher.read_account_credentials(NUM, EMAIL) == profile

    def test_expired_profile_generation_is_refreshed_with_the_profiles_grant(
        self, harness, no_network
    ):
        """Both generations expired: exactly one POST, with the PROFILE's grant
        (the backup's is consumed); the successor becomes the backup."""
        _backup, profile, _dir = _rotated_profile(
            harness.switcher, profile_expires=EXPIRED_MS
        )
        rotated = _creds("at-rotated-2", "rt-rotated-2")

        def post(creds, **_kw):
            if creds == profile:
                return RefreshOutcome(rotated, None)
            return RefreshOutcome(None, "invalid_grant")

        no_network.side_effect = post

        status = harness.engine._freshen_target(NUM, EMAIL)

        assert status == "ok"
        assert no_network.call_count == 1
        assert no_network.call_args.args[0] == profile, "POST must use the profile's grant"
        assert harness.switcher.read_account_credentials(NUM, EMAIL) == rotated

    def test_live_session_still_skips_without_touching_either_copy(
        self, harness, no_network
    ):
        backup, profile, session_dir = _rotated_profile(harness.switcher)

        with patch.object(harness.switcher, "_live_session_pids", return_value=[4242]):
            status = harness.engine._freshen_target(NUM, EMAIL)

        assert status == "skip-live-session"
        no_network.assert_not_called()
        assert harness.switcher.read_account_credentials(NUM, EMAIL) == backup
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile

    def test_tick_lands_on_the_rotated_slot_instead_of_quarantining_it(
        self, harness, no_network
    ):
        """RED on main: slot 2 (most headroom) is quarantined as invalid_grant
        off its consumed backup and the rotation lands on slot 3 instead."""
        _backup, profile, _dir = _rotated_profile(harness.switcher)

        outcome = harness.tick_with_usage(
            {"1": _usage(95), "2": _usage(10), "3": _usage(50)}
        )

        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2
        assert not any(isinstance(e, QuarantineEvent) for e in harness.events)
        no_network.assert_not_called()
        # The live login received the profile's rotated generation, not the seed.
        live = (harness.temp_home / ".claude" / ".credentials.json").read_text(
            encoding="utf-8"
        )
        assert oauth.extract_access_token(live) == "at-profile-2"


def _make_parked_switcher() -> ClaudeAccountSwitcher:
    """Slots 1 (fresh backup, no profile) and 2 (parked). No live login, so
    neither is the active slot the refresh path leaves alone."""
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s.sequence_file.write_text(
        json.dumps(
            {
                "activeAccountNumber": None,
                "lastUpdated": "2026-01-01T00:00:00Z",
                "sequence": [1, 2],
                "accounts": {
                    "1": {
                        "email": "a@example.com",
                        "uuid": "uuid-1",
                        "added": "2026-01-01T00:00:00Z",
                    },
                    NUM: {
                        "email": EMAIL,
                        "uuid": "uuid-2",
                        "added": "2026-01-02T00:00:00Z",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    s.write_account_credentials("1", "a@example.com", _creds("at-1", "rt-1"))
    return s


class TestRefreshResyncsIdleDivergedSlot:
    """``cswap refresh N`` is the hand recipe for a diverged idle slot."""

    def test_fresh_diverged_idle_profile_is_resynced_without_a_post(
        self, temp_home, no_network
    ):
        """RED on main: ``fresh`` — "nothing to touch" over a consumed backup."""
        s = _make_parked_switcher()
        _backup, profile, session_dir = _rotated_profile(s)

        report = refresh_account(s, NUM)

        assert report.outcome == RESYNCED
        no_network.assert_not_called()
        assert s.read_account_credentials(NUM, EMAIL) == profile
        # Bootstrap-shaped reseed: the profile stays ready for the next
        # `cswap run`, and the seed stamp names the generation both copies hold.
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(profile)

    def test_second_pass_is_fresh_again(self, temp_home, no_network):
        s = _make_parked_switcher()
        _rotated_profile(s)
        assert refresh_account(s, NUM).outcome == RESYNCED

        assert refresh_account(s, NUM).outcome == FRESH
        no_network.assert_not_called()

    def test_live_session_is_still_left_alone(self, temp_home, no_network):
        s = _make_parked_switcher()
        backup, profile, session_dir = _rotated_profile(s)

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            report = refresh_account(s, NUM)

        assert report.outcome == LIVE_SESSION
        assert s.read_account_credentials(NUM, EMAIL) == backup
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile
        no_network.assert_not_called()

    def test_backup_newer_than_profile_is_never_overwritten(
        self, temp_home, no_network
    ):
        """Review r.1 of CON-1579: a re-login rewrote the BACKUP under a live
        session; the profile keeps the older family plus the stale marker.
        Inequality alone must not make the stale profile win."""
        s = _make_parked_switcher()
        seed = _creds("at-seed-2", "rt-seed-2", expires=EXPIRED_MS)
        new_login = _creds("at-new-2", "rt-new-2")
        s.write_account_credentials(NUM, EMAIL, new_login)
        session_dir = s._session_dir(NUM, EMAIL)
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(
            _creds("at-old-2", "rt-old-2"), encoding="utf-8"
        )
        (session_dir / SEED_FINGERPRINT_FILE).write_text(
            oauth.credential_fingerprint(seed), encoding="utf-8"
        )
        (session_dir / STALE_MARKER).touch()

        report = refresh_account(s, NUM)

        assert report.outcome != RESYNCED
        assert s.read_account_credentials(NUM, EMAIL) == new_login
        no_network.assert_not_called()


class TestListJsonTokenFamily:
    """``list --json --token-status``: the human diagnostics, machine-readable."""

    def _entries(self, switcher):
        data = switcher._get_sequence_data()
        return {num: UsageEntry() for num in data["accounts"]}

    def test_token_status_json_carries_the_family_state(self, temp_home, no_network):
        """RED on main: no ``tokenFamily`` in the payload."""
        s = _make_parked_switcher()
        _rotated_profile(s)

        with patch.object(s, "_collect_usage_entries", return_value=self._entries(s)):
            payload = s.list_accounts(show_token_status=True, json_output=True)

        rows = {row["number"]: row for row in payload["accounts"]}
        assert rows[2]["tokenFamily"] == {
            "backup": "expired",
            "profile": "fresh",
            "diverged": True,
            "liveSession": False,
        }
        assert rows[1]["tokenFamily"] == {"backup": "fresh", "profile": "missing"}

    def test_live_session_is_reported_on_the_family(self, temp_home, no_network):
        s = _make_parked_switcher()
        _rotated_profile(s)

        with (
            patch.object(s, "_collect_usage_entries", return_value=self._entries(s)),
            patch.object(s, "_live_session_pids", return_value=[4242]),
        ):
            payload = s.list_accounts(show_token_status=True, json_output=True)

        row = next(r for r in payload["accounts"] if r["number"] == 2)
        assert row["tokenFamily"]["liveSession"] is True
        assert row["tokenFamily"]["diverged"] is True

    def test_plain_json_is_unchanged(self, temp_home, no_network):
        s = _make_parked_switcher()
        _rotated_profile(s)

        with patch.object(s, "_collect_usage_entries", return_value=self._entries(s)):
            payload = s.list_accounts(json_output=True)

        assert all("tokenFamily" not in row for row in payload["accounts"])

    def test_cli_accepts_json_with_token_status(self, temp_home, capsys):
        """RED on main: ``--token-status cannot be combined with --json``."""
        with (
            patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls,
            patch("os.geteuid", return_value=1000, create=True),
            patch.object(sys, "argv", ["claude-swap", "list", "--json", "--token-status"]),
        ):
            switcher_cls.return_value.list_accounts.return_value = {
                "schemaVersion": 1,
                "activeAccountNumber": None,
                "accounts": [],
            }
            cli.main()

        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=True, json_output=True
        )
        out = capsys.readouterr().out
        assert json.loads(out)["accounts"] == []
