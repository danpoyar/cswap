"""Active slot-token refresh without a session landing (CON-1024).

Covers two coupled behaviors:

1. ``cswap refresh`` — refreshing a parked slot's expired access token with
   its refresh token, WITHOUT launching claude into the profile. The mured
   shape it must heal: a session profile whose token family rotated past the
   slot backup (backup == consumed seed), access token expired, no live
   session — the usage collector defers that shape forever by design (seed
   guard, CON-849), so only an explicit refresh can revive the slot.

2. Honest "token expired" status: once a collect pass has derived the
   TOKEN_EXPIRED sentinel, every later pass — including one in a different
   process that loses the fetch claim — must keep reporting it until a LIVE
   successful usage read clears it. Serving hours-old last-good as
   ``usageStatus: ok`` painted dead slots as healed on the dashboard.
"""

import json
import time
from unittest.mock import patch

from claude_swap import oauth
from claude_swap.json_output import (
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
    usage_fields,
)
from claude_swap.session import (
    SEED_FINGERPRINT_FILE,
    keychain_service_name,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord

NUM = "2"
EMAIL = "test@example.com"
IDENTITY = {NUM: (EMAIL, "")}
USAGE = {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 40.0}}

EXPIRED_MS = 1_000  # 1970: long expired
FRESH_MS = 32_503_680_000_000  # far future


def _creds(access="at", refresh="rt", expires=EXPIRED_MS):
    payload = {"accessToken": access, "expiresAt": expires}
    if refresh is not None:
        payload["refreshToken"] = refresh
    return json.dumps({"claudeAiOauth": payload})


def _info(creds):
    return [(int(NUM), EMAIL, "Org", "", False, creds, "")]


def _make_switcher():
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    s.sequence_file.write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2026-01-01T00:00:00Z",
                "sequence": [2],
                "accounts": {
                    NUM: {
                        "email": EMAIL,
                        "uuid": "uuid-2",
                        "added": "2026-01-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return s


def _mured_slot(switcher):
    """The stuck shape from CON-1024: profile family rotated past the backup,
    both generations' access tokens expired, no live session."""
    backup = _creds(access="at-seed", refresh="rt-seed")
    switcher.write_account_credentials(NUM, EMAIL, backup)
    session_dir = switcher._session_dir(NUM, EMAIL)
    session_dir.mkdir(parents=True)
    profile = _creds(access="at-profile", refresh="rt-profile")
    (session_dir / ".credentials.json").write_text(profile, encoding="utf-8")
    (session_dir / SEED_FINGERPRINT_FILE).write_text(
        oauth.credential_fingerprint(backup), encoding="utf-8"
    )
    return backup, profile, session_dir


def _seed_last_good(switcher, age_s):
    """Persist a last-good measurement ``age_s`` seconds in the past."""
    store = switcher._usage_store
    store.record({NUM: FetchRecord(usage=USAGE)}, IDENTITY)
    base = time.time()
    store.clock = lambda: base + age_s


class TestMuredSlotRepro:
    """The CON-1024 dead-end, quoted as a living repro: `cswap list` passes
    never refresh a parked session-profile slot — every pass re-derives the
    'retries automatically' sentinel and defers."""

    def test_collect_pass_never_refreshes_mured_slot(self, temp_home):
        switcher = _make_switcher()
        backup, _profile, _dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=6 * 3600)

        with (
            patch("claude_swap.oauth.try_refresh_oauth_credentials") as post,
            patch("claude_swap.oauth.try_fetch_usage_for_account") as fetch,
        ):
            first = switcher._collect_usage_entries(_info(backup))
            second = switcher._collect_usage_entries(_info(backup))

        post.assert_not_called()  # the promised "retries" never POST a refresh
        fetch.assert_not_called()  # and never even reach the usage endpoint
        assert first[NUM].sentinel == USAGE_TOKEN_EXPIRED
        assert second[NUM].sentinel == USAGE_TOKEN_EXPIRED


class TestHonestTokenExpiredStatus:
    """'Fixed' may only come from a live successful usage read."""

    def test_expired_status_sticks_when_claim_is_lost(self, temp_home):
        # Pass 1 derives the sentinel. Pass 2 loses the fetch claim to a
        # concurrent collector (the panel's `cswap list` racing `cswap auto`)
        # — today the sentinel vanishes and minutes-old last-good serves as
        # usageStatus "ok": the dashboard's fake heal.
        switcher = _make_switcher()
        backup, _profile, _dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=240.0)

        first = switcher._collect_usage_entries(_info(backup))
        assert first[NUM].sentinel == USAGE_TOKEN_EXPIRED

        switcher._usage_store.claim([NUM], IDENTITY)  # another process's lease
        second = switcher._collect_usage_entries(_info(backup))
        assert second[NUM].sentinel == USAGE_TOKEN_EXPIRED
        status, _ = usage_fields(second[NUM].decision_value())
        assert status == "token_expired"

    def test_expired_status_sticks_across_processes(self, temp_home):
        # A fresh process (new store read model) must still see the expired
        # state even when its own pass cannot fetch — the marker must be
        # persisted, not a per-process overlay.
        switcher = _make_switcher()
        backup, _profile, _dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=240.0)
        switcher._collect_usage_entries(_info(backup))

        fresh = ClaudeAccountSwitcher()
        base = time.time()
        fresh._usage_store.clock = lambda: base + 240.0
        fresh._usage_store.claim([NUM], IDENTITY)
        entries = fresh._collect_usage_entries(_info(backup))
        assert entries[NUM].sentinel == USAGE_TOKEN_EXPIRED

    def test_live_success_clears_expired_status(self, temp_home):
        switcher = _make_switcher()
        backup, _profile, _dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=240.0)
        switcher._collect_usage_entries(_info(backup))

        store = switcher._usage_store
        store.record({NUM: FetchRecord(usage=USAGE)}, IDENTITY)
        entries = switcher._collect_usage_entries(_info(backup))
        assert entries[NUM].sentinel is None
        status, _ = usage_fields(entries[NUM].decision_value())
        assert status == "ok"

    def test_list_payload_carries_token_expired_age(self, temp_home):
        # The dashboard needs "Token expired · <age>": the row must name when
        # the expired state was first observed.
        switcher = _make_switcher()
        backup, _profile, _dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=240.0)
        switcher._collect_usage_entries(_info(backup))

        info = _info(backup)
        entries = switcher._collect_usage_entries(info)
        payload = switcher._build_list_payload(info, entries)
        row = payload["accounts"][0]
        assert row["usageStatus"] == "token_expired"
        assert "tokenExpiredAt" in row


class TestRefreshWithoutLanding:
    """`cswap refresh` heals a parked slot with its own refresh token."""

    def _rotated(self):
        return _creds(access="at-new", refresh="rt-new", expires=FRESH_MS)

    def test_refresh_heals_mured_slot_without_landing(
        self, temp_home, block_real_keychain
    ):
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        _backup, profile, session_dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=6 * 3600)
        rotated = self._rotated()

        with (
            patch(
                "claude_swap.refresh.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(rotated, None),
            ) as post,
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(USAGE),
            ),
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == "refreshed"
        # The profile family (newest generation) was the one consumed.
        post.assert_called_once()
        assert post.call_args.args[0] == profile
        # Successor persisted everywhere the bootstrap shape expects it:
        creds_path = session_dir / ".credentials.json"
        assert creds_path.read_text(encoding="utf-8") == rotated
        seed = (session_dir / SEED_FINGERPRINT_FILE).read_text(encoding="utf-8")
        assert seed == oauth.credential_fingerprint(rotated)
        assert switcher.read_account_credentials(NUM, EMAIL) == rotated
        # Live proof: the store's measurement moved without any landing.
        entry = switcher._usage_store.entries(IDENTITY)[NUM]
        assert entry.last_good == USAGE
        assert entry.age_s is not None and entry.age_s < 5.0

    def test_refresh_consumes_keychain_generation_when_present(
        self, temp_home, block_real_keychain, monkeypatch
    ):
        # claude rotates the profile family into the hashed keychain entry;
        # the plaintext seed is then a CONSUMED generation. POSTing it risks
        # the documented reuse reaction (whole-login revocation), so the
        # keychain generation must win. Keychain paths are macOS-only —
        # force the platform so the case runs on any CI host.
        from claude_swap import macos_keychain
        from claude_swap.models import Platform
        from claude_swap.refresh import refresh_account

        monkeypatch.setattr(
            Platform, "detect", classmethod(lambda cls: Platform.MACOS)
        )

        switcher = _make_switcher()
        _backup, _profile, session_dir = _mured_slot(switcher)
        keychain_gen = _creds(access="at-kc", refresh="rt-kc")
        block_real_keychain.set_password(
            keychain_service_name(session_dir),
            macos_keychain.keychain_account_name(),
            keychain_gen,
        )
        rotated = self._rotated()

        with (
            patch(
                "claude_swap.refresh.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(rotated, None),
            ) as post,
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(USAGE),
            ),
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == "refreshed"
        post.assert_called_once()
        assert post.call_args.args[0] == keychain_gen
        # The stale hashed entry would shadow the fresh plaintext seed —
        # it must be gone (the bootstrap invariant).
        assert (
            block_real_keychain.get_password(
                keychain_service_name(session_dir),
                macos_keychain.keychain_account_name(),
            )
            is None
        )

    def test_refresh_leaves_revoked_slot_untouched(self, temp_home):
        # No refresh token anywhere: only a human re-login can help. The
        # refresher must say so honestly and must not touch the stores.
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        revoked_backup = _creds(access="at-seed", refresh=None)
        switcher.write_account_credentials(NUM, EMAIL, revoked_backup)
        session_dir = switcher._session_dir(NUM, EMAIL)
        session_dir.mkdir(parents=True)
        revoked_profile = _creds(access="at-profile", refresh=None)
        (session_dir / ".credentials.json").write_text(
            revoked_profile, encoding="utf-8"
        )
        (session_dir / SEED_FINGERPRINT_FILE).write_text(
            oauth.credential_fingerprint(revoked_backup), encoding="utf-8"
        )

        with patch(
            "claude_swap.refresh.try_refresh_oauth_credentials"
        ) as post:
            report = refresh_account(switcher, NUM)

        assert report.outcome == "relogin-required"
        post.assert_not_called()
        assert (
            session_dir / ".credentials.json"
        ).read_text(encoding="utf-8") == revoked_profile
        assert switcher.read_account_credentials(NUM, EMAIL) == revoked_backup

    def test_refresh_skips_active_slot(self, temp_home):
        # The active slot's live credential is Claude Code's store; the
        # backup this path would POST is a consumed predecessor whenever CC
        # rotated in place — refreshing it can strike (or revoke) a LIVE
        # login. The active account heals through its own locked path.
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        _mured_slot(switcher)

        with (
            patch.object(
                switcher, "_get_current_account", return_value=(EMAIL, "")
            ),
            patch(
                "claude_swap.refresh.try_refresh_oauth_credentials"
            ) as post,
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == "active-slot"
        post.assert_not_called()

    def test_usage_read_false_without_fresh_measurement(self, temp_home):
        # Old last-good must not masquerade as the post-refresh live proof:
        # when the proof fetch fails, usage_read is False even though the
        # slot still carries an hours-old measurement.
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        _backup, _profile, _dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=6 * 3600)

        with (
            patch(
                "claude_swap.refresh.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(self._rotated(), None),
            ),
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(None, error="transient"),
            ),
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == "refreshed"
        assert report.usage_read is False

    def test_refresh_skips_live_session(self, temp_home):
        # A live claude refreshes lazily on its next API call; consuming the
        # profile's grant under it would log the session out (CON-849).
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        _mured_slot(switcher)

        with (
            patch.object(switcher, "_live_session_pids", return_value=[4242]),
            patch(
                "claude_swap.refresh.try_refresh_oauth_credentials"
            ) as post,
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == "live-session"
        post.assert_not_called()

    def test_refresh_respects_quarantine(self, temp_home):
        # A lineage the store already condemned (invalid_grant strike) must
        # not be re-POSTed — same parole rule as the collector.
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        _backup, profile, _dir = _mured_slot(switcher)
        switcher._usage_store.record(
            {
                NUM: FetchRecord(
                    error="invalid_grant",
                    credential_fingerprint=oauth.credential_fingerprint(
                        profile
                    ),
                )
            },
            IDENTITY,
        )

        with patch(
            "claude_swap.refresh.try_refresh_oauth_credentials"
        ) as post:
            report = refresh_account(switcher, NUM)

        assert report.outcome == "relogin-required"
        post.assert_not_called()

    def test_refresh_invalid_grant_quarantines_honestly(self, temp_home):
        # A dead lineage discovered BY the refresh advances the store's
        # strike so every surface flips to "re-login needed" instead of
        # retrying a grant the server already rejected.
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        backup, _profile, _dir = _mured_slot(switcher)

        with patch(
            "claude_swap.refresh.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "invalid_grant"),
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == "relogin-required"
        entries = switcher._collect_usage_entries(_info(backup))
        assert entries[NUM].sentinel == USAGE_RELOGIN_REQUIRED

    def test_refresh_skips_fresh_token(self, temp_home):
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        fresh_backup = _creds(expires=FRESH_MS)
        switcher.write_account_credentials(NUM, EMAIL, fresh_backup)

        with patch(
            "claude_swap.refresh.try_refresh_oauth_credentials"
        ) as post:
            report = refresh_account(switcher, NUM)

        assert report.outcome == "fresh"
        post.assert_not_called()

    def test_refresh_heals_profileless_slot_from_backup(self, temp_home):
        # A parked slot with no session profile: the backup family is the
        # slot's only (and freshest) generation — refresh and persist it.
        from claude_swap.refresh import refresh_account

        switcher = _make_switcher()
        backup = _creds(access="at-seed", refresh="rt-seed")
        switcher.write_account_credentials(NUM, EMAIL, backup)
        rotated = self._rotated()

        with (
            patch(
                "claude_swap.refresh.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(rotated, None),
            ) as post,
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(USAGE),
            ),
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == "refreshed"
        post.assert_called_once()
        assert post.call_args.args[0] == backup
        assert switcher.read_account_credentials(NUM, EMAIL) == rotated


class TestRefreshCli:
    def test_refresh_command_json_reports_outcomes(self, temp_home, capsys):
        from claude_swap import cli
        from claude_swap.refresh import RefreshReport

        _make_switcher()
        with patch(
            "claude_swap.refresh.refresh_accounts",
            return_value=[
                RefreshReport(NUM, EMAIL, "refreshed", None, True)
            ],
        ) as run:
            cli._refresh_command([NUM, "--json"])

        run.assert_called_once()
        assert run.call_args.args[1] == [NUM]
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"] == [
            {
                "number": 2,
                "email": EMAIL,
                "outcome": "refreshed",
                "detail": None,
                "usageRead": True,
            }
        ]

    def test_refresh_all_excludes_active_slot(self, temp_home, capsys):
        from claude_swap import cli

        switcher = _make_switcher()
        data = json.loads(switcher.sequence_file.read_text(encoding="utf-8"))
        data["activeAccountNumber"] = 2
        switcher.sequence_file.write_text(json.dumps(data), encoding="utf-8")

        with patch(
            "claude_swap.refresh.refresh_accounts", return_value=[]
        ) as run:
            cli._refresh_command(["--all", "--json"])

        run.assert_called_once()
        assert run.call_args.args[1] == []  # slot 2 is active — dropped

    def test_refresh_all_stale_min_skips_recently_measured(
        self, temp_home, capsys
    ):
        from claude_swap import cli

        switcher = _make_switcher()
        # Freshly measured slot: --stale-min filters it out entirely.
        switcher._usage_store.record({NUM: FetchRecord(usage=USAGE)}, IDENTITY)

        with patch(
            "claude_swap.refresh.refresh_accounts", return_value=[]
        ) as run:
            cli._refresh_command(["--all", "--stale-min", "30", "--json"])

        run.assert_called_once()
        assert run.call_args.args[1] == []

    def test_refresh_all_stale_min_keeps_expired_marked_slot(
        self, temp_home, capsys
    ):
        from claude_swap import cli
        from claude_swap.refresh import RefreshReport

        switcher = _make_switcher()
        backup, _profile, _dir = _mured_slot(switcher)
        _seed_last_good(switcher, age_s=240.0)
        switcher._collect_usage_entries(_info(backup))  # stamps the marker

        with patch(
            "claude_swap.refresh.refresh_accounts",
            return_value=[
                RefreshReport(NUM, EMAIL, "refreshed", None, True)
            ],
        ) as run:
            cli._refresh_command(["--all", "--stale-min", "30", "--json"])

        assert run.call_args.args[1] == [NUM]


def _keychain_busy_at_the_landing(switcher):
    """The profile reads fine for the caller's own read, and the Keychain
    goes busy exactly at the landing's re-judge (CON-2100) inside
    ``reconcile_pending_rotation_locked`` — the two-read race the locked
    callers guard against (CON-2375). A context manager."""
    from contextlib import contextmanager

    from claude_swap import session as _session_mod

    real_reconcile = switcher.reconcile_pending_rotation_locked
    real_read = _session_mod.read_profile_generation
    landing = {"on": False}

    def reconcile(*args, **kwargs):
        landing["on"] = True
        try:
            return real_reconcile(*args, **kwargs)
        finally:
            landing["on"] = False

    def read(session_dir):
        if landing["on"]:
            return None, "keychain unavailable"
        return real_read(session_dir)

    @contextmanager
    def gate():
        with (
            patch.object(switcher, "reconcile_pending_rotation_locked", reconcile),
            patch.object(_session_mod, "read_profile_generation", read),
        ):
            yield

    return gate()


class TestSpilledAdoptionOverUnreadableProfile:
    """CON-2375: ``reconcile_pending_rotation_locked`` defers (``None``) when
    a spilled ADOPTION's profile cannot be read at the landing. The refresh
    read no credential in the profile a moment earlier, so it reached the
    reconcile; it must answer DEFERRED — not NO_CREDENTIALS (the family is
    alive in the sidecar) — and POST nothing: the backup it holds is the
    sidecar's consumed predecessor."""

    def test_refresh_defers_when_the_landing_cannot_read_the_profile(
        self, temp_home
    ):
        from claude_swap.refresh import DEFERRED, refresh_account
        from claude_swap.switcher import SPILL_ORIGIN_PROFILE

        switcher = _make_switcher()
        backup = _creds(access="at-seed", refresh="rt-seed")
        switcher.write_account_credentials(NUM, EMAIL, backup)
        session_dir = switcher._session_dir(NUM, EMAIL)
        session_dir.mkdir(parents=True)  # the slot's profile, no credential now
        spill = switcher._pending_rotation_path(NUM)
        spill.parent.mkdir(parents=True, exist_ok=True)
        spill.write_text(
            json.dumps(
                {
                    "credentials": _creds(access="at-spilled", refresh="rt-spilled"),
                    "predecessorFingerprint": oauth.credential_fingerprint(backup),
                    "email": EMAIL,
                    "createdAt": "2026-09-06T08:00:00Z",
                    "origin": SPILL_ORIGIN_PROFILE,
                }
            ),
            encoding="utf-8",
        )
        sidecar = spill.read_text(encoding="utf-8")

        with (
            _keychain_busy_at_the_landing(switcher),
            patch("claude_swap.refresh.try_refresh_oauth_credentials") as post,
            patch("claude_swap.oauth.try_fetch_usage_for_account") as fetch,
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == DEFERRED
        assert "cannot be read" in (report.detail or "")
        post.assert_not_called()
        fetch.assert_not_called()
        assert spill.read_text(encoding="utf-8") == sidecar  # sidecar untouched
        assert switcher.read_account_credentials(NUM, EMAIL) == backup
        assert not (session_dir / ".credentials.json").exists()
        assert switcher.list_unclaimed_credentials() == {}
