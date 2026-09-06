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
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.models import Platform
from claude_swap.oauth import RefreshOutcome
from claude_swap.session import SEED_FINGERPRINT_FILE, STALE_MARKER
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

    def test_live_session_ahead_is_adopted_under_even_if_live(
        self, temp_home, mock_claude_config, no_network
    ):
        """CON-2069: the fleet's guard returns the login to the home slot with
        `--even-if-live` while the home's own `cswap run` session is live and
        rotated ahead of the backup. Instead of the refusal above the backup
        adopts the session's generation (no POST) and THAT generation lands;
        the live profile is left alone."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            result = s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert result["switched"] is True
        landed = _live_path(temp_home).read_text(encoding="utf-8")
        assert oauth.extract_access_token(landed) == oauth.extract_access_token(profile)
        assert oauth.extract_access_token(landed) != oauth.extract_access_token(backup)
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile
        assert any("adopted the session's generation" in w for w in result.get("warnings", []))
        no_network.assert_not_called()
        data = s._get_sequence_data()
        assert data["activeAccountNumber"] == int(TARGET_NUM)

    def test_adoption_under_even_if_live_leaves_the_live_profile_current(
        self, temp_home, mock_claude_config, no_network
    ):
        """Review r.2 of PR #35 (Important): the backup write behind the
        adoption marked the LIVE profile stale (``_post_backup_write``), and
        ``_backup_is_newer`` reads that marker first — so the guard's SECOND
        ``switch <home> --even-if-live`` (the session rotated again meanwhile,
        the login died and failed over) landed the consumed backup silently:
        the CON-1579 shape the heal exists to exclude. After an adoption
        backup == profile: no marker, the seed re-stamped, and the next visit
        adopts the session's NEXT generation again."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            first = s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert first["switched"] is True
        assert not (session_dir / STALE_MARKER).exists(), (
            "an adoption must not mark the live profile stale — backup == profile"
        )
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(profile)

        # The login leaves home (a failover) and the live session rotates
        # the family once more.
        s.switch_to(ACTIVE_NUM, json_output=True)
        profile_3 = _creds("at-profile-3", "rt-profile-3")
        (session_dir / ".credentials.json").write_text(profile_3, encoding="utf-8")

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            second = s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert second["switched"] is True
        landed = _live_path(temp_home).read_text(encoding="utf-8")
        assert oauth.extract_access_token(landed) == "at-profile-3", (
            "the second visit must land the session's newest generation, "
            "never the consumed one"
        )
        assert oauth.extract_access_token(
            s.read_account_credentials(TARGET_NUM, TARGET_EMAIL)
        ) == "at-profile-3"
        assert any("adopted the session's generation" in w for w in second.get("warnings", []))
        assert not (session_dir / STALE_MARKER).exists()
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile_3
        no_network.assert_not_called()

    def test_spilled_adoption_under_even_if_live_is_refused_not_landed_consumed(
        self, temp_home, mock_claude_config, no_network
    ):
        """Review r.2 (minor): ``adopt_profile_family`` reported an adoption
        even when the pair went to the spill sidecar (backup still the consumed
        generation) — the heal then activated the consumed backup under an
        "adopted" notice. A spilled adoption is no adoption: the refusal stands."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        live_before = _live_path(temp_home).read_text(encoding="utf-8")

        with (
            patch.object(s, "_live_session_pids", return_value=[4242]),
            patch.object(s, "persist_backup_credentials", return_value=False),
        ):
            with pytest.raises(SwitchError) as exc:
                s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert "4242" in str(exc.value)
        assert _live_path(temp_home).read_text(encoding="utf-8") == live_before
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == backup
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        no_network.assert_not_called()

    def test_nothing_to_adopt_under_even_if_live_falls_back_to_the_refusal(
        self, temp_home, mock_claude_config, no_network
    ):
        """Review r.2 (nit): the ``adopted=False`` branch under the override
        keeps the typed refusal with its recipe — no silent landing."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        live_before = _live_path(temp_home).read_text(encoding="utf-8")

        with (
            patch.object(s, "_live_session_pids", return_value=[4242]),
            patch.object(s, "adopt_profile_family", return_value=False),
        ):
            with pytest.raises(SwitchError) as exc:
                s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert f"cswap run {TARGET_NUM}" in str(exc.value)
        assert _live_path(temp_home).read_text(encoding="utf-8") == live_before
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == backup
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile
        no_network.assert_not_called()

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


def _superseded_profile(s: ClaudeAccountSwitcher, profile_expires: int = FRESH_MS,
                        marker: bool = True, backup_expires: int = FRESH_MS):
    """Review r.1 shape: the BACKUP is the newer login. A re-add/re-login while
    the profile's session was live rewrote the backup (B) and left the profile
    with its older family (A) plus the stale marker; the seed stamp names the
    generation both copies started from (neither A nor B).

    ``backup_expires=EXPIRED_MS`` is the CON-2345 shape: the same three
    generations, but B is a parked copy nobody refreshed (the daemon's
    failover backed the home slot up from the global store) while A is the
    live session's fresh generation."""
    seed = _creds("at-seed-2", "rt-seed-2", expires=EXPIRED_MS)
    new_login = _creds("at-new-2", "rt-new-2", expires=backup_expires)
    s.write_account_credentials(TARGET_NUM, TARGET_EMAIL, new_login)  # B
    session_dir = s._session_dir(TARGET_NUM, TARGET_EMAIL)
    session_dir.mkdir(parents=True)
    old_family = _creds("at-old-2", "rt-old-2", expires=profile_expires)  # A
    (session_dir / ".credentials.json").write_text(old_family, encoding="utf-8")
    (session_dir / SEED_FINGERPRINT_FILE).write_text(
        oauth.credential_fingerprint(seed), encoding="utf-8"
    )
    if marker:
        (session_dir / STALE_MARKER).touch()
    return new_login, old_family, session_dir


class TestBackupNewerThanProfile:
    """Fingerprint inequality alone does not say who ran ahead (review r.1)."""

    def test_stale_marked_profile_never_overwrites_the_newer_backup(
        self, temp_home, mock_claude_config, no_network
    ):
        s = _make_switcher(temp_home)
        new_login, _old, session_dir = _superseded_profile(s)

        result = s.switch_to(TARGET_NUM, json_output=True)

        assert result["switched"] is True
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-new-2"
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == new_login
        no_network.assert_not_called()
        assert not any("healed" in w for w in result["warnings"]), result["warnings"]
        # The superseded copy is dropped (no live session) — setup_session's
        # own rule, applied now: one family, one copy.
        assert not (session_dir / ".credentials.json").exists()
        assert not (session_dir / STALE_MARKER).exists()

    def test_expired_stale_profile_does_not_refuse_the_fresh_backup(
        self, temp_home, mock_claude_config, no_network
    ):
        s = _make_switcher(temp_home)
        new_login, _old, _dir = _superseded_profile(s, profile_expires=EXPIRED_MS)
        no_network.return_value = RefreshOutcome(None, "invalid_grant")  # A is dead

        result = s.switch_to(TARGET_NUM, json_output=True)

        assert result["switched"] is True
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-new-2"
        no_network.assert_not_called()  # the dead old family is never POSTed

    def test_seed_stamp_moved_backup_is_newer_without_marker(
        self, temp_home, mock_claude_config, no_network
    ):
        """Marker gone (e.g. hand-cleaned) but the seed stamp != backup: the
        backup moved after seeding — still the newer generation."""
        s = _make_switcher(temp_home)
        new_login, _old, session_dir = _superseded_profile(s, marker=False)

        result = s.switch_to(TARGET_NUM, json_output=True)

        assert result["switched"] is True
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-new-2"
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == new_login
        no_network.assert_not_called()

    def test_stale_marked_profile_with_live_session_is_refused_then_kept_under_override(
        self, temp_home, mock_claude_config, no_network
    ):
        """Three generations (seed, A in the live profile, B in the backup):
        since CON-2052 the switch REFUSES — by fingerprints this is an honest
        re-add over a lingering old-family session or one family forked in
        two stores, and the 2026-09-04 incident was the latter. Under the
        explicit `--even-if-live` override the heal's law stands: activate
        the backup, leave the live session's copy alone (setup_session
        re-bootstraps it once idle)."""
        from claude_swap.exceptions import LiveSessionRefusal

        s = _make_switcher(temp_home)
        new_login, old_family, session_dir = _superseded_profile(s)

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            with pytest.raises(LiveSessionRefusal):
                s.switch_to(TARGET_NUM, json_output=True)
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == old_family
        no_network.assert_not_called()

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            result = s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert result["switched"] is True
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-new-2"
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == old_family
        assert (session_dir / STALE_MARKER).exists()
        no_network.assert_not_called()


class TestLiveSessionOutranksTheOrderingOracles:
    """CON-2345 (live 2026-09-06 03:43–05:10 on the fleet machine): the
    daemon's failover backed the home slot up from the global store under the
    orchestrator's live profile session — stale marker set, backup a consumed
    generation — and the fleet guard's `switch 32 --even-if-live` landed that
    dead backup on every return (three returns, three failovers): both
    ordering oracles (marker, seed stamp) said "the backup is newer" while the
    LIVE session's generation was fresh and the backup's expired. Under a live
    session that live evidence outranks the oracles: the heal reports the
    live session (refusal without the override), and the `--even-if-live`
    adoption lands the session's generation past the seed guard."""

    def test_fresh_live_profile_over_expired_backup_is_adopted_under_even_if_live(
        self, temp_home, mock_claude_config, no_network
    ):
        from claude_swap.exceptions import LiveSessionRefusal

        s = _make_switcher(temp_home)
        stale_backup, live_family, session_dir = _superseded_profile(
            s, backup_expires=EXPIRED_MS
        )

        # Without the override a live session owning the family is refused —
        # naming the shape and the override (review r.1 nit), not "a dead
        # login" the generic text would claim of a possibly valid re-login.
        with (
            patch.object(s, "_live_session_pids", return_value=[4242]),
            pytest.raises(LiveSessionRefusal) as refusal,
        ):
            s.switch_to(TARGET_NUM, json_output=True)
        assert "--even-if-live" in str(refusal.value)
        assert "stored backup's has expired" in str(refusal.value)
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == stale_backup
        assert (session_dir / STALE_MARKER).exists()
        no_network.assert_not_called()

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            result = s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert result["switched"] is True
        landed = _live_path(temp_home).read_text(encoding="utf-8")
        assert oauth.extract_access_token(landed) == "at-old-2", (
            "the live session's fresh generation must land, never the expired backup"
        )
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == live_family
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == live_family
        assert not (session_dir / STALE_MARKER).exists()
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(live_family)
        assert any("adopted the session's generation" in w for w in result.get("warnings", []))
        no_network.assert_not_called()
        assert s._get_sequence_data()["activeAccountNumber"] == int(TARGET_NUM)

    def test_heal_reports_the_live_session_ahead_on_live_evidence(
        self, temp_home, mock_claude_config, no_network
    ):
        from claude_swap.refresh import LIVE_SESSION, heal_backup_before_activation

        s = _make_switcher(temp_home)
        _stale_backup, live_family, session_dir = _superseded_profile(
            s, backup_expires=EXPIRED_MS
        )

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            report = heal_backup_before_activation(s, TARGET_NUM, TARGET_EMAIL, "")

        assert report.outcome == LIVE_SESSION
        assert report.detail == "4242"
        assert report.profile_ahead is True
        # Judging mutates nothing: the live profile and the marker stay.
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == live_family
        assert (session_dir / STALE_MARKER).exists()
        no_network.assert_not_called()

    @pytest.mark.parametrize(
        "profile_expires,backup_expires",
        [
            (FRESH_MS, FRESH_MS),  # both fresh: who ran ahead is undecidable
            (EXPIRED_MS, EXPIRED_MS),  # both expired: no live evidence either way
            (EXPIRED_MS, FRESH_MS),  # the backup is the fresh one
        ],
    )
    def test_without_live_evidence_the_oracles_keep_their_verdict(
        self, temp_home, mock_claude_config, no_network, profile_expires, backup_expires
    ):
        from claude_swap.refresh import BACKUP_CURRENT, heal_backup_before_activation

        s = _make_switcher(temp_home)
        _stale_backup, live_family, session_dir = _superseded_profile(
            s, profile_expires=profile_expires, backup_expires=backup_expires
        )

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            report = heal_backup_before_activation(s, TARGET_NUM, TARGET_EMAIL, "")

        assert report.outcome == BACKUP_CURRENT
        assert report.profile_ahead is False
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == live_family
        assert (session_dir / STALE_MARKER).exists()
        no_network.assert_not_called()

    def test_backup_without_an_expiry_is_not_live_evidence(
        self, temp_home, mock_claude_config, no_network
    ):
        from claude_swap.refresh import BACKUP_CURRENT, heal_backup_before_activation

        s = _make_switcher(temp_home)
        _stale_backup, live_family, session_dir = _superseded_profile(
            s, backup_expires=EXPIRED_MS
        )
        no_expiry = json.dumps(
            {"claudeAiOauth": {"accessToken": "at-new-2", "refreshToken": "rt-new-2"}}
        )
        # Written UNDER the live session (review r.1, Major 1): an idle
        # write hook would drop the profile's copy and the heal would stop
        # at "profile holds no credential" before ever judging the expiry.
        with patch.object(s, "_live_session_pids", return_value=[4242]):
            s.write_account_credentials(TARGET_NUM, TARGET_EMAIL, no_expiry)
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == live_family
        assert (session_dir / STALE_MARKER).exists()

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            report = heal_backup_before_activation(s, TARGET_NUM, TARGET_EMAIL, "")

        assert report.outcome == BACKUP_CURRENT
        assert report.detail == "profile marked stale — the backup is the newer login"
        assert report.profile_ahead is False
        no_network.assert_not_called()

    def test_profile_still_at_its_seed_is_not_evidence(
        self, temp_home, mock_claude_config, no_network
    ):
        """The session never rotated (profile == seed stamp): a backup written
        after the seeding is the newer login by construction (re-add/
        re-login), however expired its access token — the oracles stand."""
        from claude_swap.refresh import BACKUP_CURRENT, heal_backup_before_activation

        s = _make_switcher(temp_home)
        _stale_backup, live_family, session_dir = _superseded_profile(
            s, backup_expires=EXPIRED_MS
        )
        (session_dir / SEED_FINGERPRINT_FILE).write_text(
            oauth.credential_fingerprint(live_family), encoding="utf-8"
        )

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            report = heal_backup_before_activation(s, TARGET_NUM, TARGET_EMAIL, "")

        assert report.outcome == BACKUP_CURRENT
        assert report.profile_ahead is False
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == live_family
        no_network.assert_not_called()

    def test_idle_profile_is_still_judged_by_the_oracles(
        self, temp_home, mock_claude_config, no_network
    ):
        """No live session: nothing rotates the profile any more, so the
        oracles' verdict stands and the superseded copy is dropped as before
        (the idle shape is CON-2100, not this ticket)."""
        from claude_swap.refresh import BACKUP_CURRENT, heal_backup_before_activation

        s = _make_switcher(temp_home)
        stale_backup, _live_family, session_dir = _superseded_profile(
            s, backup_expires=EXPIRED_MS
        )

        with patch.object(s, "_live_session_pids", return_value=[]):
            report = heal_backup_before_activation(s, TARGET_NUM, TARGET_EMAIL, "")

        assert report.outcome == BACKUP_CURRENT
        assert report.profile_ahead is False
        assert not (session_dir / ".credentials.json").exists()
        assert not (session_dir / STALE_MARKER).exists()
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == stale_backup
        no_network.assert_not_called()

    def test_adoption_past_the_seed_guard_needs_the_heal_verdict(
        self, temp_home, mock_claude_config, no_network
    ):
        """The seed guard of ``adopt_profile_family`` stands for every other
        caller (bootstrap, reseed): only the heal's live verdict lifts it."""
        s = _make_switcher(temp_home)
        stale_backup, live_family, session_dir = _superseded_profile(
            s, backup_expires=EXPIRED_MS
        )

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            assert s.adopt_profile_family(TARGET_NUM, TARGET_EMAIL, "") is False
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == stale_backup

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            assert s.adopt_profile_family(
                TARGET_NUM, TARGET_EMAIL, "", profile_ahead=True
            ) is True
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == live_family
        assert not (session_dir / STALE_MARKER).exists()
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(live_family)
        no_network.assert_not_called()


class TestDriftNoticeOrdering:
    def test_refusal_precedes_the_drift_notice(
        self, temp_home, mock_claude_config, no_network
    ):
        """The advisory drift notice must not be emitted for a switch that is
        refused: warnings never leak out of a raised SwitchError path, and in
        human mode nothing is printed before the error."""
        s = _make_switcher(temp_home)
        _rotated_profile(s)
        with patch.object(s, "_live_session_pids", return_value=[4242]):
            with patch("claude_swap.switcher.warning") as warn:
                with pytest.raises(SwitchError):
                    s.switch_to(TARGET_NUM, json_output=False)
        warn.assert_not_called()


def _spilled_adoption(s: ClaudeAccountSwitcher):
    """`switch --even-if-live` while the store lock is held elsewhere: the
    adoption's persist loses the lock race twice and the profile's generation
    goes to the spill sidecar (CON-849); the switch is refused. Returns the
    sidecar path."""
    from claude_swap.locking import FileLock as _FL

    holder = _FL(s.lock_file, timeout=0.1)
    assert holder.acquire()
    try:
        with (
            patch.object(s, "_live_session_pids", return_value=[4242]),
            patch(
                "claude_swap.switcher.FileLock",
                lambda path, **kw: _FL(path, timeout=0.1),
            ),
        ):
            with pytest.raises(SwitchError):
                s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)
    finally:
        holder.release()
    spill = s._pending_rotation_path(TARGET_NUM)
    assert spill.exists(), "the adoption must have spilled"
    return spill


def _collect_pass(s: ClaudeAccountSwitcher, backup: str, pids: list[int]):
    """One usage-collector visit of the parked slot — the reconcile pass
    that folds a pending spill into the backup before any network touch."""
    with (
        patch.object(s, "_live_session_pids", return_value=pids),
        patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome({"five_hour": {"pct": 9}}),
        ),
    ):
        return s._fetch_account_usage(
            (int(TARGET_NUM), TARGET_EMAIL, "Org", "", False, backup, "")
        )


class TestSpilledAdoptionReconcile:
    """CON-2075 (review r.3 of PR #35, below Important → ticket): an adoption
    under `--even-if-live` that SPILLED lands in the backup later, through
    `_reconcile_spilled_rotation_locked` → `_write_account_credentials` →
    `_post_backup_write`, whose live-PID branch marks the profile stale and
    which never re-stamps the seed. After that landing backup == profile
    (one generation), yet BOTH ordering oracles of `_backup_is_newer` say
    "the backup is newer" — the marker lies and the seed stamp is the
    predecessor. The next `switch <home> --even-if-live`, after the session
    rotated once more, would land the consumed generation: the CON-1579
    shape the heal exists to exclude. The spill is a deferred adoption: its
    landing must do the adoption's bookkeeping (seed re-stamped, marker
    dropped) for a live profile that holds — or rotated onward from — the
    spilled generation."""

    def test_spilled_adoption_reconciled_under_the_live_session_leaves_no_lying_marker(
        self, temp_home, mock_claude_config, no_network
    ):
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        spill = _spilled_adoption(s)
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == backup
        assert json.loads(spill.read_text(encoding="utf-8"))["credentials"] == profile

        _collect_pass(s, backup, pids=[4242])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile
        assert not spill.exists()
        assert not (session_dir / STALE_MARKER).exists(), (
            "the landed spill IS the live profile's generation — the marker lies"
        )
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(profile), (
            "backup and profile are one generation again — the seed must say so"
        )
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile

        # The live session rotates the family once more; the guard returns
        # the login home — it must land the session's NEWEST generation.
        profile_3 = _creds("at-profile-3", "rt-profile-3")
        (session_dir / ".credentials.json").write_text(profile_3, encoding="utf-8")
        with patch.object(s, "_live_session_pids", return_value=[4242]):
            result = s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert result["switched"] is True
        landed = _live_path(temp_home).read_text(encoding="utf-8")
        assert oauth.extract_access_token(landed) == "at-profile-3", (
            "the return home landed the consumed generation"
        )
        assert oauth.extract_access_token(
            s.read_account_credentials(TARGET_NUM, TARGET_EMAIL)
        ) == "at-profile-3"
        assert any("adopted the session's generation" in w for w in result.get("warnings", []))
        assert not (session_dir / STALE_MARKER).exists()
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile_3
        no_network.assert_not_called()

    def test_session_rotated_between_spill_and_reconcile_still_lands_its_newest_generation(
        self, temp_home, mock_claude_config, no_network
    ):
        """The spill's bytes came from the profile; when the profile rotates
        onward BEFORE the reconcile lands them, the sidecar is the profile's
        consumed predecessor. The landing re-judges the profile and lands ITS
        newest generation (CON-2100 — the same rule that saves an idle
        profile's copy), so backup and live profile are one generation: no
        marker, seed == backup, and the next return home lands that
        generation without a heal. (Before CON-2100 the sidecar landed and
        the oracles had to read "the profile ran ahead" for the return home
        to adopt the newest generation — that landing is now the profile's
        own.)"""
        s = _make_switcher(temp_home)
        backup, _profile, session_dir = _rotated_profile(s)
        _spilled_adoption(s)
        profile_3 = _creds("at-profile-3", "rt-profile-3")
        (session_dir / ".credentials.json").write_text(profile_3, encoding="utf-8")

        _collect_pass(s, backup, pids=[4242])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile_3, (
            "the landing must be the profile's newest generation, not the sidecar's"
        )
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile_3
        assert not (session_dir / STALE_MARKER).exists()
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(profile_3)

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            result = s.switch_to(TARGET_NUM, json_output=True, even_if_live=True)

        assert result["switched"] is True
        assert oauth.extract_access_token(
            _live_path(temp_home).read_text(encoding="utf-8")
        ) == "at-profile-3"
        assert oauth.extract_access_token(
            s.read_account_credentials(TARGET_NUM, TARGET_EMAIL)
        ) == "at-profile-3"
        assert not (session_dir / STALE_MARKER).exists()
        no_network.assert_not_called()

    def test_legacy_spill_without_origin_matching_the_live_profile_is_settled(
        self, temp_home, mock_claude_config, no_network
    ):
        """A sidecar written before the origin field existed: the ticket's
        own rule — the landed generation equals the live profile's — is
        enough to settle the profile."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        spill = s._pending_rotation_path(TARGET_NUM)
        spill.write_text(json.dumps({
            "credentials": profile,
            "predecessorFingerprint": oauth.credential_fingerprint(backup),
            "email": TARGET_EMAIL,
        }), encoding="utf-8")

        _collect_pass(s, backup, pids=[4242])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile
        assert not spill.exists()
        assert not (session_dir / STALE_MARKER).exists()
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(profile)
        no_network.assert_not_called()

    def test_foreign_spill_keeps_the_truthful_marker_on_a_live_profile(
        self, temp_home, mock_claude_config, no_network
    ):
        """A spill that did NOT come from the profile (a network refresh of
        the backup family) lands a generation the live profile does not hold:
        the backup really is the newer login, the marker is truthful and the
        seed stamp stays — the settling must not over-reach."""
        s = _make_switcher(temp_home)
        seeded = _creds("at-seed-2", "rt-seed-2")
        s.write_account_credentials(TARGET_NUM, TARGET_EMAIL, seeded)
        session_dir = s._session_dir(TARGET_NUM, TARGET_EMAIL)
        session_dir.mkdir(parents=True)
        (session_dir / ".credentials.json").write_text(seeded, encoding="utf-8")
        (session_dir / SEED_FINGERPRINT_FILE).write_text(
            oauth.credential_fingerprint(seeded), encoding="utf-8"
        )
        rotated_by_network = _creds("at-net-3", "rt-net-3")
        spill = s._pending_rotation_path(TARGET_NUM)
        spill.write_text(json.dumps({
            "credentials": rotated_by_network,
            "predecessorFingerprint": oauth.credential_fingerprint(seeded),
            "email": TARGET_EMAIL,
        }), encoding="utf-8")

        _collect_pass(s, seeded, pids=[4242])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == rotated_by_network
        assert not spill.exists()
        assert (session_dir / STALE_MARKER).exists(), (
            "the profile holds an older generation — the marker is truthful"
        )
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(seeded)
        from claude_swap.refresh import BACKUP_CURRENT, heal_backup_before_activation

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            report = heal_backup_before_activation(s, TARGET_NUM, TARGET_EMAIL, "")
        assert report.outcome == BACKUP_CURRENT
        no_network.assert_not_called()

    def test_spilled_adoption_reconciled_on_an_idle_profile_leaves_no_stamp_over_no_credential(
        self, temp_home, mock_claude_config, no_network
    ):
        """The session exited before the reconcile: the write hook drops the
        idle profile's copy (one family, one copy). The settling must not
        re-create a seed stamp over no credential — that freezes the backup
        for the collector's seed guard (reseed door, review r.1 Major 1)."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        _spilled_adoption(s)

        _collect_pass(s, backup, pids=[])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile
        assert not (session_dir / ".credentials.json").exists()
        assert not (session_dir / STALE_MARKER).exists()
        assert not (session_dir / SEED_FINGERPRINT_FILE).exists()
        no_network.assert_not_called()

    def test_session_rotated_and_exited_before_the_reconcile_lands_the_profile_generation_not_the_sidecar(
        self, temp_home, mock_claude_config, no_network
    ):
        """CON-2100: the spill came from the profile; the session rotates the
        family once more and EXITS before the reconcile lands the sidecar.
        Landing the sidecar's (now consumed) generation runs the write hook's
        idle branch, which destroys the profile's copy — the only copy of the
        family's newest generation. The landing must re-judge the profile
        first and land ITS generation; the sidecar is superseded, not landed.
        Afterwards the family has ONE copy (the backup) and the idle profile
        carries neither a marker nor a stamp over no credential."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        spill = _spilled_adoption(s)
        profile_3 = _creds("at-profile-3", "rt-profile-3")
        (session_dir / ".credentials.json").write_text(profile_3, encoding="utf-8")

        _collect_pass(s, backup, pids=[])

        landed = s.read_account_credentials(TARGET_NUM, TARGET_EMAIL)
        assert oauth.extract_access_token(landed) == "at-profile-3", (
            "the reconcile landed the sidecar's consumed generation; the idle "
            "profile's newest generation was destroyed by the write hook"
        )
        assert landed == profile_3
        assert not spill.exists()
        assert not (session_dir / ".credentials.json").exists(), "one family, one copy"
        assert not (session_dir / STALE_MARKER).exists()
        assert not (session_dir / SEED_FINGERPRINT_FILE).exists()
        superseded = s.list_unclaimed_credentials()
        assert [e["fingerprint"] for e in superseded.values()] == [
            oauth.credential_fingerprint(profile)
        ], "the superseded sidecar is preserved as an unclaimed copy, not destroyed"
        assert {e["reason"] for e in superseded.values()} == {"superseded-rotation-spill"}
        # The next `cswap run` bootstraps from the backup — the newest generation.
        from claude_swap.refresh import BACKUP_CURRENT, heal_backup_before_activation

        report = heal_backup_before_activation(s, TARGET_NUM, TARGET_EMAIL, "")
        assert report.outcome == BACKUP_CURRENT
        no_network.assert_not_called()

    def test_unreadable_profile_keychain_defers_the_landing_and_keeps_every_copy(
        self, temp_home, mock_claude_config, no_network, caplog
    ):
        """CON-2375 (review of PR #45): an existing-but-unreadable keychain
        entry is not "no profile" — the profile may hold the family's newest
        generation and the sidecar its consumed predecessor. Landing the
        sidecar anyway (the pin CON-2100 left) ran the write hook's idle
        branch, which dropped the profile's copy — the only copy of the
        newest generation — and left the backup with the consumed one: the
        next `cswap run` bootstrapped a dead login. The landing is DEFERRED
        instead: the sidecar stays for the next pass, the profile's copy and
        the backup are untouched, nothing is stashed, the collector fetches
        nothing with the consumed backup and says why. The next pass with a
        readable profile lands the profile's generation (the CON-2100 rule)."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        spill = _spilled_adoption(s)
        sidecar = spill.read_text(encoding="utf-8")
        profile_3 = _creds("at-profile-3", "rt-profile-3")
        (session_dir / ".credentials.json").write_text(profile_3, encoding="utf-8")

        with (
            patch(
                "claude_swap.session.read_profile_generation",
                return_value=(None, "keychain unavailable"),
            ),
            caplog.at_level("WARNING", logger="claude-swap"),
        ):
            record = _collect_pass(s, backup, pids=[])

        assert record.sentinel == USAGE_TOKEN_EXPIRED, (
            "the backup is the sidecar's consumed predecessor — nothing may "
            "be fetched with it while the landing is deferred"
        )
        assert spill.read_text(encoding="utf-8") == sidecar, (
            "the sidecar must stay for the next pass"
        )
        assert (session_dir / ".credentials.json").read_text(
            encoding="utf-8"
        ) == profile_3, "the profile's copy — the family's newest — must survive"
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == backup
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(backup)
        assert s.list_unclaimed_credentials() == {}
        assert any(
            "could not be read while landing its spilled adoption" in r.getMessage()
            and "keychain unavailable" in r.getMessage()
            and "deferr" in r.getMessage()
            for r in caplog.records
        ), "the deferred landing must be said aloud"
        no_network.assert_not_called()

        # The next pass reads the profile: its generation lands, the sidecar
        # is superseded into the unclaimed store (CON-2100).
        _collect_pass(s, backup, pids=[])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile_3
        assert not spill.exists()
        assert not (session_dir / ".credentials.json").exists(), "one family, one copy"
        assert [
            e["fingerprint"] for e in s.list_unclaimed_credentials().values()
        ] == [oauth.credential_fingerprint(profile)]
        no_network.assert_not_called()

    def test_profile_reseeded_from_the_predecessor_before_the_reconcile_still_lands_the_sidecar(
        self, temp_home, mock_claude_config, no_network
    ):
        """The guard of the re-judge: a profile that holds the spill's
        PREDECESSOR (re-bootstrapped from the old backup after the session
        exited) is not "ahead" — the sidecar is the family's newest
        generation and must land as before; the re-seeded copy is dropped."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        spill = _spilled_adoption(s)
        (session_dir / ".credentials.json").write_text(backup, encoding="utf-8")

        _collect_pass(s, backup, pids=[])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile
        assert not spill.exists()
        assert not (session_dir / ".credentials.json").exists()
        assert s.list_unclaimed_credentials() == {}
        no_network.assert_not_called()

    def test_profile_rotated_onward_but_drifted_to_another_login_lands_the_sidecar(
        self, temp_home, mock_claude_config, no_network
    ):
        """A profile logged in as ANOTHER account holds a generation of some
        other family: never adopted into this slot's backup, whatever its
        fingerprint says — the sidecar lands as before."""
        s = _make_switcher(temp_home)
        backup, profile, session_dir = _rotated_profile(s)
        spill = _spilled_adoption(s)
        (session_dir / ".credentials.json").write_text(
            _creds("at-other-9", "rt-other-9"), encoding="utf-8"
        )
        (session_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "other@example.com",
                             "accountUuid": "uuid-other"}
        }), encoding="utf-8")

        _collect_pass(s, backup, pids=[])

        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile
        assert not spill.exists()
        assert s.list_unclaimed_credentials() == {}
        no_network.assert_not_called()

    def test_session_exiting_between_the_hook_and_the_settling_is_still_settled(
        self, temp_home, mock_claude_config, no_network
    ):
        """Review r.1 of PR #37 (minor): the write hook and the settling must
        not judge liveness independently. Hook sees the session live (marks
        the profile stale, keeps its copy); the session exits before the
        settling runs. A second process scan would see nothing live and leave
        the lying marker on an idle profile that rotated onward — the next
        `cswap run` would destroy that newest generation. Liveness is judged
        once, by the hook: the marker it left IS the proof."""
        s = _make_switcher(temp_home)
        backup, _profile, session_dir = _rotated_profile(s)
        _spilled_adoption(s)
        profile_3 = _creds("at-profile-3", "rt-profile-3")
        (session_dir / ".credentials.json").write_text(profile_3, encoding="utf-8")

        scans = iter([[4242]])  # the hook's scan sees the session; later ones do not

        def _pids(*_a, **_k):
            return next(scans, [])

        with (
            patch.object(s, "_live_session_pids", side_effect=_pids),
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome({"five_hour": {"pct": 9}}),
            ),
        ):
            s._fetch_account_usage(
                (int(TARGET_NUM), TARGET_EMAIL, "Org", "", False, backup, "")
            )

        # CON-2100: the landing is the profile's newest generation (the
        # sidecar was its consumed predecessor); the hook saw the session
        # live and marked the profile stale — the settling must still clear
        # that marker and stamp the landed generation, judging liveness by
        # the marker alone.
        assert s.read_account_credentials(TARGET_NUM, TARGET_EMAIL) == profile_3
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == profile_3
        assert not (session_dir / STALE_MARKER).exists()
        assert (session_dir / SEED_FINGERPRINT_FILE).read_text(
            encoding="utf-8"
        ) == oauth.credential_fingerprint(profile_3)
        from claude_swap.refresh import _backup_is_newer

        assert _backup_is_newer(
            session_dir, s.read_account_credentials(TARGET_NUM, TARGET_EMAIL)
        ) is None, "the oracles must read: the profile ran ahead"
        no_network.assert_not_called()
