"""Activating a slot whose session profile rotated past the stored backup (CON-1579).

Live incident 2026-08-31 09:53–09:56 on the fleet machine: after a reboot the
operator switched the default login by hand onto slots 31, 29 and 21. Each had
a session profile (`cswap run`) whose claude had rotated the token family in
its own keychain entry; the slot backup still held the consumed seed
generation. `switch` copied that consumed generation into the live store,
Claude Code's refresh was rejected and the terminal printed
"Login expired · Please run /login" on every slot. The fix: before
activation, a slot backup that lags its session profile is healed from the
profile — adopted as-is when the profile generation is fresh, refreshed via
the existing `cswap refresh` machinery when it is expired — and a slot whose
live session owns the family is refused instead of handed out dead.
"""

import json
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.exceptions import SwitchError
from claude_swap.models import Platform
from claude_swap.oauth import RefreshOutcome
from claude_swap.session import SEED_FINGERPRINT_FILE
from claude_swap.switcher import ClaudeAccountSwitcher

ACTIVE_NUM, ACTIVE_EMAIL = "1", "test@example.com"  # identity of mock_claude_config
TARGET_NUM, TARGET_EMAIL = "2", "account2@example.com"

EXPIRED_MS = 1_000  # 1970: long expired
FRESH_MS = 32_503_680_000_000  # far future


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


def _live_path(temp_home):
    return temp_home / ".claude" / ".credentials.json"


def _make_switcher(temp_home) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s.platform = Platform.LINUX  # live credential = ~/.claude/.credentials.json
    s._write_json(
        s.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2026-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                ACTIVE_NUM: {
                    "email": ACTIVE_EMAIL,
                    "uuid": "test-uuid-1234",
                    "added": "2026-01-01T00:00:00Z",
                },
                TARGET_NUM: {
                    "email": TARGET_EMAIL,
                    "uuid": "uuid-2",
                    "added": "2026-01-02T00:00:00Z",
                },
            },
        },
    )
    live = _creds("at-live-1", "rt-live-1")
    _live_path(temp_home).write_text(live, encoding="utf-8")
    # The outgoing slot's backup equals the live bytes → "own-bytes" at switch
    # time: config-only backup, no identity oracle needed.
    s.write_account_credentials(ACTIVE_NUM, ACTIVE_EMAIL, live)
    s._write_account_config(
        ACTIVE_NUM,
        ACTIVE_EMAIL,
        json.dumps({"oauthAccount": {"emailAddress": ACTIVE_EMAIL,
                                     "accountUuid": "test-uuid-1234"}}),
    )
    s._write_account_config(
        TARGET_NUM,
        TARGET_EMAIL,
        json.dumps({"oauthAccount": {"emailAddress": TARGET_EMAIL,
                                     "accountUuid": "uuid-2"}}),
    )
    return s


def _rotated_profile(s: ClaudeAccountSwitcher, profile_expires: int = FRESH_MS):
    """The incident shape: backup = consumed seed generation (expired), the
    session profile holds the rotated successor. Returns (backup, profile, dir)."""
    backup = _creds("at-seed-2", "rt-seed-2", expires=EXPIRED_MS)
    s.write_account_credentials(TARGET_NUM, TARGET_EMAIL, backup)
    session_dir = s._session_dir(TARGET_NUM, TARGET_EMAIL)
    session_dir.mkdir(parents=True)
    profile = _creds("at-profile-2", "rt-profile-2", expires=profile_expires)
    (session_dir / ".credentials.json").write_text(profile, encoding="utf-8")
    (session_dir / SEED_FINGERPRINT_FILE).write_text(
        oauth.credential_fingerprint(backup), encoding="utf-8"
    )
    return backup, profile, session_dir


@pytest.fixture
def no_network():
    """The identity oracle and the token endpoint never leave the test."""
    with (
        patch("claude_swap.oauth.fetch_oauth_profile", return_value=None),
        patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
        # refresh.py binds the name at import time — patch its reference too,
        # and make both hand out the same outcome.
        patch("claude_swap.refresh.try_refresh_oauth_credentials", post),
    ):
        yield post


class TestSwitchHealsConsumedBackup:
    def test_fresh_profile_generation_is_adopted_without_a_post(
        self, temp_home, mock_claude_config, no_network
    ):
        """RED on main: the live store received the consumed seed generation."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)

        result = s.switch_to(TARGET_NUM, json_output=True)

        assert result["switched"] is True
        live = _live_path(temp_home).read_text(encoding="utf-8")
        assert oauth.extract_access_token(live) == "at-profile-2", (
            "the live login must be the profile's rotated generation, "
            "not the consumed seed the backup held"
        )
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile
        no_network.assert_not_called()  # adopted, no grant consumed
        # One family, one live copy: the idle profile no longer carries it.
        assert not (session_dir / ".credentials.json").exists()
        assert any("healed" in w for w in result["warnings"]), result["warnings"]

    def test_backup_in_step_with_profile_switches_as_before(
        self, temp_home, mock_claude_config, no_network
    ):
        s = _make_switcher(temp_home)
        same = _creds("at-2", "rt-2")
        s.write_account_credentials(TARGET_NUM, TARGET_EMAIL, same)
        session_dir = s._session_dir(TARGET_NUM, TARGET_EMAIL)
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(same, encoding="utf-8")

        result = s.switch_to(TARGET_NUM, json_output=True)

        assert result["switched"] is True
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-2"
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == same
        no_network.assert_not_called()
        assert not any("healed" in w for w in result["warnings"])
        # Untouched profile: nothing lagged, nothing invalidated.
        assert (session_dir / ".credentials.json").exists()

    def test_no_profile_switches_as_before(
        self, temp_home, mock_claude_config, no_network
    ):
        s = _make_switcher(temp_home)
        stored = _creds("at-2", "rt-2")
        s.write_account_credentials(TARGET_NUM, TARGET_EMAIL, stored)

        result = s.switch_to(TARGET_NUM, json_output=True)

        assert result["switched"] is True
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-2"
        no_network.assert_not_called()

    def test_expired_profile_generation_is_refreshed_from_the_profile(
        self, temp_home, mock_claude_config, no_network
    ):
        """Both generations expired: one POST with the PROFILE's grant (the
        backup's is consumed), successor persisted, then activated."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s, profile_expires=EXPIRED_MS)
        rotated = _creds("at-rotated-2", "rt-rotated-2")
        no_network.return_value = RefreshOutcome(rotated, None)

        result = s.switch_to(TARGET_NUM, json_output=True)

        assert result["switched"] is True
        assert no_network.call_count == 1
        assert no_network.call_args[0][0] == profile, "POST must use the profile's grant"
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-rotated-2"
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == rotated
        assert not (session_dir / ".credentials.json").exists()


class TestSwitchRefusesDeadLanding:
    def test_live_session_owning_the_family_is_refused_with_recipe(
        self, temp_home, mock_claude_config, no_network
    ):
        """RED on main: warn-and-proceed handed out the consumed generation."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        live_before = _live_path(temp_home).read_text(encoding="utf-8")

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            with pytest.raises(SwitchError) as exc:
                s.switch_to(TARGET_NUM, json_output=True)

        msg = str(exc.value)
        assert f"cswap run {TARGET_NUM}" in msg
        assert "4242" in msg
        assert _live_path(temp_home).read_text(encoding="utf-8") == live_before
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == backup
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile
        no_network.assert_not_called()
        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_rejected_grant_is_refused_with_relogin_recipe(
        self, temp_home, mock_claude_config, no_network
    ):
        s = _make_switcher(temp_home)
        backup, profile, _dir = _rotated_profile(s, profile_expires=EXPIRED_MS)
        no_network.return_value = RefreshOutcome(None, "invalid_grant")
        live_before = _live_path(temp_home).read_text(encoding="utf-8")

        with pytest.raises(SwitchError) as exc:
            s.switch_to(TARGET_NUM, json_output=True)

        assert f"cswap add --slot {TARGET_NUM}" in str(exc.value)
        assert _live_path(temp_home).read_text(encoding="utf-8") == live_before
        assert s._get_sequence_data()["activeAccountNumber"] == 1

    def test_transient_refresh_failure_is_refused_not_landed_dead(
        self, temp_home, mock_claude_config, no_network
    ):
        s = _make_switcher(temp_home)
        _rotated_profile(s, profile_expires=EXPIRED_MS)
        no_network.return_value = RefreshOutcome(None, "transient")
        live_before = _live_path(temp_home).read_text(encoding="utf-8")

        with pytest.raises(SwitchError) as exc:
            s.switch_to(TARGET_NUM, json_output=True)

        assert "cswap refresh 2" in str(exc.value)
        assert _live_path(temp_home).read_text(encoding="utf-8") == live_before
