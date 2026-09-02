"""A re-bootstrap must never POST the backup's consumed grant over a live
profile family (CON-1740; live incident 2026-09-02, slot 29).

The shape: a ``cswap run`` session rotated the token family inside its
profile (the backup is the consumed seed generation, the seed stamp still
names it). Under machine overload the profile's Keychain reads time out, so
the next ``cswap run`` on the slot sees ``claude auth status`` fail its
reuse probe, takes the bootstrap path and POSTs the BACKUP's refresh grant —
a consumed generation. The server answers ``invalid_grant``, the store
condemns the lineage (one strike is death), every later spawn is refused as
"condemned", the fleet's refresh job files a P1 "re-login by hand" ticket —
while the live session on the profile keeps working the whole time and the
family is alive. 86 minutes of false alarm, healed only when the live
session exited and the next bootstrap adopted the profile generation.

Rules pinned here:

- probe inconclusive + live session on the profile → JOIN the profile
  (its claude owns the family; a second process reads the same store) — no
  bootstrap, no POST;
- probe inconclusive, no live session, profile readable and AHEAD of the
  backup → adopt the profile generation first, then bootstrap from THAT
  (the only grant POSTed is the family's newest);
- profile UNREADABLE (Keychain busy) over a backup that equals the seed →
  refuse the run transiently — the backup may be a consumed grant and the
  profile may hold the successor; guessing consumes the family;
- ``heal_backup_before_activation`` treats a Keychain lookup that cannot
  tell as ``deferred``, never as "profile holds no credential" (that verdict
  let ``_freshen_target`` POST the consumed backup);
- the collector's parole judges the PROFILE generation too: a condemned
  backup under a live, fresh profile is healed by one read-only usage fetch
  with the profile credential instead of waiting for the session to exit.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap import macos_keychain, oauth
from claude_swap import session as session_mod
from claude_swap.exceptions import SessionError
from claude_swap.json_output import USAGE_RELOGIN_REQUIRED
from claude_swap.macos_keychain import KeychainError
from claude_swap.models import Platform
from claude_swap.refresh import DEFERRED, heal_backup_before_activation
from claude_swap.session import (
    SEED_FINGERPRINT_FILE,
    SessionManager,
    keychain_service_name,
    session_dir_for,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord
from tests.test_session import (
    ACCOUNT_EMAIL,
    ACCOUNT_NUM,
    CONFIG,
    ORG_UUID,
    make_live,
)

FRESH_MS = 32_503_680_000_000  # far future
EXPIRED_MS = 1_000


def _creds(access: str, refresh: str | None, expires: int = FRESH_MS) -> str:
    inner: dict = {"accessToken": access, "expiresAt": expires}
    if refresh is not None:
        inner["refreshToken"] = refresh
    return json.dumps({"claudeAiOauth": inner})


BACKUP = _creds("at-seed", "rt-seed", expires=EXPIRED_MS)  # consumed seed generation
PROFILE_GEN = _creds("at-live", "rt-live")  # what the live claude rotated into


@pytest.fixture
def macos_platform(monkeypatch):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda _cls: Platform.MACOS))


@pytest.fixture
def switcher(temp_home: Path, macos_platform) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher(debug=True)
    s._setup_directories()
    s._write_json(
        s.sequence_file,
        {
            "activeAccountNumber": 1,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1, 2],
            "accounts": {
                "1": {
                    "email": "account1@example.com",
                    "uuid": "uuid-1",
                    "organizationUuid": "org-uuid-1",
                    "organizationName": "Org One",
                    "added": "2024-01-01T00:00:00Z",
                },
                ACCOUNT_NUM: {
                    "email": ACCOUNT_EMAIL,
                    "uuid": "uuid-2",
                    "organizationUuid": ORG_UUID,
                    "organizationName": "Org Two",
                    "added": "2024-01-02T00:00:00Z",
                },
            },
        },
    )
    s._write_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL, BACKUP)
    s._write_account_config(ACCOUNT_NUM, ACCOUNT_EMAIL, CONFIG)
    return s


@pytest.fixture
def manager(switcher) -> SessionManager:
    return SessionManager(switcher)


@pytest.fixture
def rotated_profile(switcher, block_real_keychain) -> Path:
    """The incident shape: profile seeded from BACKUP (seed stamp = its
    fingerprint), then claude rotated the family into the profile's hashed
    keychain entry; the backup is now the consumed predecessor."""
    session_dir = session_dir_for(switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL)
    session_dir.mkdir(parents=True)
    (session_dir / ".credentials.json").write_text(BACKUP, encoding="utf-8")
    (session_dir / SEED_FINGERPRINT_FILE).write_text(
        oauth.credential_fingerprint(BACKUP), encoding="utf-8"
    )
    (session_dir / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": ACCOUNT_EMAIL,
                    "organizationUuid": ORG_UUID,
                },
                "hasCompletedOnboarding": True,
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )
    block_real_keychain.set_password(
        keychain_service_name(session_dir),
        macos_keychain.keychain_account_name(),
        PROFILE_GEN,
    )
    return session_dir


@pytest.fixture
def probe_times_out(monkeypatch):
    """`claude auth status` under overload: the CLI hangs past the budget
    while the profile still carries the stale seed; a re-seeded profile
    (any other plaintext generation) probes as logged in."""

    def fake_run(cmd, env=None, **_kwargs):
        config_dir = Path(env["CLAUDE_CONFIG_DIR"])
        creds_path = config_dir / ".credentials.json"
        if creds_path.exists() and creds_path.read_text(encoding="utf-8") != BACKUP:
            payload = {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "email": ACCOUNT_EMAIL,
                "orgId": ORG_UUID,
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    monkeypatch.setattr(session_mod.subprocess, "run", fake_run)


@pytest.fixture
def post_spy(monkeypatch):
    """Every refresh-grant POST, recorded; the server rejects the consumed
    seed generation and rotates anything else."""
    posts: list[str] = []

    def fake_refresh(creds: str) -> oauth.RefreshOutcome:
        posts.append(creds)
        if creds == BACKUP:
            return oauth.RefreshOutcome(None, "invalid_grant")
        return oauth.RefreshOutcome(_creds("at-rotated", "rt-rotated"), None)

    monkeypatch.setattr(session_mod, "try_refresh_oauth_credentials", fake_refresh)
    monkeypatch.setattr(
        "claude_swap.refresh.try_refresh_oauth_credentials", fake_refresh
    )
    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", fake_refresh)
    return posts


def _busy_keychain(session_dir: Path):
    """The profile's hashed entry times out (overload); everything else reads
    from the test's in-memory store. A ``patch.object`` context, so it is undone
    inside the test body — before the autouse keychain fake is torn down."""
    service = keychain_service_name(session_dir)
    real_get = macos_keychain.get_password

    def busy_get(svc, account):
        if svc == service:
            raise KeychainError("security find-generic-password timed out after 5.0s")
        return real_get(svc, account)

    return patch.object(macos_keychain, "get_password", busy_get)


def _entry(switcher):
    identity = {ACCOUNT_NUM: (ACCOUNT_EMAIL, ORG_UUID)}
    return switcher._usage_store.entries(identity)[ACCOUNT_NUM]


class TestSetupSessionOverRotatedProfile:
    def test_probe_failure_with_live_session_joins_profile_without_post(
        self, manager, switcher, rotated_profile, probe_times_out, post_spy,
        block_real_keychain,
    ):
        """RED on main: the bootstrap POSTs the consumed backup grant →
        invalid_grant → lineage condemned while the live session works."""
        make_live(rotated_profile)

        session_dir, num, email = manager.setup_session("2", share=False)

        assert session_dir == rotated_profile
        assert post_spy == []  # nothing consumed
        # The live claude's generation is untouched.
        assert (
            block_real_keychain.get_password(
                keychain_service_name(rotated_profile),
                macos_keychain.keychain_account_name(),
            )
            == PROFILE_GEN
        )
        assert not _entry(switcher).token_dead()

    def test_idle_rotated_profile_is_adopted_and_seeded_from_its_generation(
        self, manager, switcher, rotated_profile, probe_times_out, post_spy,
    ):
        """No live session: the profile's newer generation is adopted into
        the backup first; the one bootstrap POST consumes THAT grant, never
        the consumed seed."""
        session_dir, _, _ = manager.setup_session("2", share=False)

        assert BACKUP not in post_spy, "consumed seed generation was POSTed"
        assert post_spy == [PROFILE_GEN]
        rotated = _creds("at-rotated", "rt-rotated")
        assert switcher.read_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL) == rotated
        assert (session_dir / ".credentials.json").read_text(encoding="utf-8") == rotated
        assert not _entry(switcher).token_dead()

    def test_unreadable_profile_over_seed_backup_refuses_without_post(
        self, manager, switcher, rotated_profile, probe_times_out, post_spy,
    ):
        """Keychain busy: the profile may hold the successor, the backup may
        be consumed — refuse the run transiently, consume nothing."""
        # Patched inside the test body (not via the monkeypatch fixture): the
        # autouse keychain fake is torn down by its own MonkeyPatch, and a
        # fixture-scoped patch of the same attribute leaked the fake into the
        # no_keychain_fake tests of test_macos_keychain.py.
        with _busy_keychain(rotated_profile), pytest.raises(
            SessionError, match="(?i)keychain"
        ):
            manager.setup_session("2", share=False)

        assert post_spy == []
        assert not _entry(switcher).token_dead()

    def test_wiped_profile_over_seed_backup_still_judges_the_backup(
        self, manager, switcher, rotated_profile, probe_times_out, post_spy,
        block_real_keychain,
    ):
        """claude's invalid_grant wipe leaves an entry without a token pair:
        the family is dead in the profile, the backup is the only grant left —
        POSTing it is the honest verdict (condemned), not a transient refusal."""
        block_real_keychain.set_password(
            keychain_service_name(rotated_profile),
            macos_keychain.keychain_account_name(),
            json.dumps({"claudeAiOauth": {"expiresAt": 1}}),
        )

        with pytest.raises(SessionError, match="invalid_grant"):
            manager.setup_session("2", share=False)

        assert post_spy == [BACKUP]
        assert _entry(switcher).token_dead()


class TestTokenProfileReuseWithoutProbe:
    def test_token_seeded_profile_is_valid_without_spawning_claude(
        self, manager, switcher, rotated_profile, probe_times_out, post_spy,
    ):
        """A slot on the year-long inference token: the profile holds the
        token credential, not a login family — nothing to probe with
        ``claude auth status`` (which drops the token from its env and, under
        overload, hangs on the Keychain). Reuse on the seed alone; the
        session authenticates by env precedence (CON-1740, K2)."""
        from claude_swap.inference_token import inference_token_credentials

        token = "sk-ant-oat01-" + "x" * 96
        with patch.object(switcher, "inference_token_for", return_value=token):
            (rotated_profile / ".credentials.json").write_text(
                inference_token_credentials(token), encoding="utf-8"
            )
            session_dir, _, _ = manager.setup_session("2", share=False)

        assert session_dir == rotated_profile
        assert post_spy == []
        assert (rotated_profile / ".credentials.json").read_text(
            encoding="utf-8"
        ) == inference_token_credentials(token)


class TestHealDefersOnUnknownKeychain:
    def test_lookup_that_cannot_tell_is_deferred_not_backup_current(
        self, switcher, rotated_profile,
    ):
        """RED on main: ``item_exists`` swallows a timeout as "absent", the
        heal reads the plaintext seed (= backup), answers ``backup-current``
        and ``_freshen_target`` POSTs the consumed backup grant."""
        with (
            patch.object(macos_keychain, "item_exists", lambda _s, _a: False),
            _busy_keychain(rotated_profile),
            patch("claude_swap.refresh.try_refresh_oauth_credentials") as post,
        ):
            report = heal_backup_before_activation(
                switcher, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID
            )

        assert report.outcome == DEFERRED
        post.assert_not_called()


class TestParoleByProfileGeneration:
    def test_condemned_backup_under_fresh_profile_heals_by_a_profile_read(
        self, switcher, rotated_profile,
    ):
        """RED on main: parole judges the BACKUP fingerprint only — the
        condemned generation — so the slot reads "re-login needed" for as
        long as the live session lives, although its profile credential is
        fresh and a read-only usage fetch with it proves the family alive."""
        identity = {ACCOUNT_NUM: (ACCOUNT_EMAIL, ORG_UUID)}
        switcher._usage_store.record(
            {
                ACCOUNT_NUM: FetchRecord(
                    error="invalid_grant",
                    credential_fingerprint=oauth.credential_fingerprint(BACKUP),
                )
            },
            identity,
        )
        switcher._usage_store.clock = lambda: time.time() + 3600.0
        info = [(2, ACCOUNT_EMAIL, "Org Two", ORG_UUID, False, BACKUP, "")]
        usage = {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 10.0}}

        with (
            patch.object(switcher, "_live_session_pids", return_value=[4242]),
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(usage),
            ) as fetch,
        ):
            entries = switcher._collect_usage_entries(info)

        fetch.assert_called_once()
        assert fetch.call_args.args[2] == PROFILE_GEN
        assert fetch.call_args.kwargs.get("is_active") is True  # read-only
        assert entries[ACCOUNT_NUM].sentinel is None
        assert entries[ACCOUNT_NUM].last_good == usage
        assert not entries[ACCOUNT_NUM].token_dead()

    def test_condemned_backup_without_a_profile_stays_quarantined(
        self, switcher,
    ):
        """No profile generation to parole with: the verdict stands."""
        identity = {ACCOUNT_NUM: (ACCOUNT_EMAIL, ORG_UUID)}
        switcher._usage_store.record(
            {
                ACCOUNT_NUM: FetchRecord(
                    error="invalid_grant",
                    credential_fingerprint=oauth.credential_fingerprint(BACKUP),
                )
            },
            identity,
        )
        switcher._usage_store.clock = lambda: time.time() + 3600.0
        info = [(2, ACCOUNT_EMAIL, "Org Two", ORG_UUID, False, BACKUP, "")]

        with patch("claude_swap.oauth.try_fetch_usage_for_account") as fetch:
            entries = switcher._collect_usage_entries(info)

        fetch.assert_not_called()
        assert entries[ACCOUNT_NUM].sentinel == USAGE_RELOGIN_REQUIRED
