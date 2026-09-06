"""``cswap reseed`` — the backup's newer login generation goes INTO a session
profile, live sessions included (CON-2030, second half).

The healer for a dead orchestrator login (config repo, fleet-sensors P)
restarts the session with ``cswap run <email> -- --resume``; the profile is
marked stale, but ``setup_session`` honors the marker only on an IDLE
profile, and the reuse probe (``claude auth status``) answers ``loggedIn:
true`` for an expired token. With the operator's terminal tabs living in
the same profile (never idle), the restart came back up on the profile's
consumed generation — "Login expired" again. The door writes the backup's
generation into the profile store under Claude Code's own locks for THAT
config dir, without waiting for the sessions to end.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.inference_token import inference_token_credentials
from claude_swap.macos_keychain import KeychainError
from claude_swap.models import Platform
from claude_swap.session import (
    SEED_FINGERPRINT_FILE,
    STALE_MARKER,
    keychain_service_name,
)
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord

NUM = "2"
EMAIL = "test@example.com"
IDENTITY = {NUM: (EMAIL, "")}

EXPIRED_MS = 1_000  # 1970: long expired
FRESH_MS = 32_503_680_000_000  # far future

LIVE_PIDS = [4242, 4343]


def _creds(access: str, refresh: str | None, expires: int = FRESH_MS) -> str:
    payload: dict = {"accessToken": access, "expiresAt": expires}
    if refresh is not None:
        payload["refreshToken"] = refresh
    return json.dumps({"claudeAiOauth": payload})


def _make_switcher(kind: str | None = None) -> ClaudeAccountSwitcher:
    s = ClaudeAccountSwitcher()
    s._setup_directories()
    record: dict = {
        "email": EMAIL,
        "uuid": "uuid-2",
        "added": "2026-01-01T00:00:00Z",
    }
    if kind:
        record["kind"] = kind
    s.sequence_file.write_text(
        json.dumps(
            {
                "activeAccountNumber": 1,
                "lastUpdated": "2026-01-01T00:00:00Z",
                "sequence": [2],
                "accounts": {NUM: record},
            }
        ),
        encoding="utf-8",
    )
    return s


def _profile(switcher, creds: str | None, seed_of: str | None, marker: bool = False):
    """A session profile on disk: plaintext ``creds`` (None = no credential
    material), seed stamp = fingerprint of ``seed_of`` (None = unstamped)."""
    session_dir = switcher._session_dir(NUM, EMAIL)
    session_dir.mkdir(parents=True, exist_ok=True)
    if creds is not None:
        (session_dir / ".credentials.json").write_text(creds, encoding="utf-8")
    if seed_of is not None:
        (session_dir / SEED_FINGERPRINT_FILE).write_text(
            oauth.credential_fingerprint(seed_of), encoding="utf-8"
        )
    if marker:
        (session_dir / STALE_MARKER).touch()
    return session_dir


def _incident_slot(switcher, backup_expires: int = FRESH_MS):
    """The incident shape: the profile holds the CONSUMED generation it was
    seeded with (stamp == that generation), the backup moved past it (a
    persisted rotation / re-login rewrote it under the live session — the
    stale marker is set), live sessions run in the profile."""
    consumed = _creds("at-consumed", "rt-consumed", expires=EXPIRED_MS)
    backup = _creds("at-backup", "rt-backup", expires=backup_expires)
    switcher.write_account_credentials(NUM, EMAIL, backup)
    session_dir = _profile(switcher, consumed, seed_of=consumed, marker=True)
    return consumed, backup, session_dir


@pytest.fixture(autouse=True)
def post():
    """The token endpoint never leaves the test."""
    with patch("claude_swap.oauth.try_refresh_oauth_credentials") as mock:
        yield mock


@pytest.fixture
def live():
    """Two live claude instances run in the slot's profile."""
    with patch.object(
        ClaudeAccountSwitcher, "_live_session_pids", return_value=list(LIVE_PIDS)
    ):
        yield list(LIVE_PIDS)


def _stamp(session_dir) -> str:
    return (session_dir / SEED_FINGERPRINT_FILE).read_text(encoding="utf-8")


def _plaintext(session_dir) -> str:
    return (session_dir / ".credentials.json").read_text(encoding="utf-8")


class TestReseedWritesBackupIntoLiveProfile:
    """(а) profile = consumed generation, backup fresher, live sessions."""

    def test_backup_generation_lands_in_the_profile_under_live_sessions(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import RESEEDED, reseed_account

        s = _make_switcher()
        _consumed, backup, session_dir = _incident_slot(s)

        report = reseed_account(s, NUM)

        assert report.outcome == RESEEDED
        assert report.reseeded is True
        assert report.source == "backup"
        assert report.live_pids == LIVE_PIDS
        assert report.generation == oauth.credential_fingerprint(backup)
        # The profile store now holds the backup's generation…
        assert oauth.extract_access_token(_plaintext(session_dir)) == "at-backup"
        # …the seed stamp names it (backup and profile are one generation
        # again — the collector's seed guard stays truthful)…
        assert _stamp(session_dir) == oauth.credential_fingerprint(backup)
        # …and the deferred-invalidation marker is gone: nothing is stale.
        assert not (session_dir / STALE_MARKER).exists()
        # No grant consumed, backup untouched.
        post.assert_not_called()
        assert s.read_account_credentials(NUM, EMAIL) == backup

    def test_reseed_keeps_the_profiles_own_shared_mcp_families(
        self, temp_home, post, live
    ):
        """The live session's mcpOAuth forks are its own current generation
        (CON-1432: each profile rotates its fork independently) — the reseed
        replaces the LOGIN family only."""
        from claude_swap.reseed import RESEEDED, reseed_account

        s = _make_switcher()
        consumed = json.loads(_creds("at-consumed", "rt-consumed", EXPIRED_MS))
        consumed["mcpOAuth"] = {"linear|abc": {"accessToken": "mcp-profile"}}
        consumed_raw = json.dumps(consumed)
        backup = json.loads(_creds("at-backup", "rt-backup"))
        backup["mcpOAuth"] = {"linear|abc": {"accessToken": "mcp-backup-stale"}}
        backup_raw = json.dumps(backup)
        s.write_account_credentials(NUM, EMAIL, backup_raw)
        session_dir = _profile(s, consumed_raw, seed_of=consumed_raw)

        report = reseed_account(s, NUM)

        assert report.outcome == RESEEDED
        written = json.loads(_plaintext(session_dir))
        assert written["claudeAiOauth"]["accessToken"] == "at-backup"
        assert written["claudeAiOauth"]["refreshToken"] == "rt-backup"
        assert written["mcpOAuth"] == {"linear|abc": {"accessToken": "mcp-profile"}}

    def test_reseed_on_macos_replaces_the_hashed_keychain_generation(
        self, temp_home, post, live, block_real_keychain, monkeypatch
    ):
        """claude rotated the profile family into the hashed keychain entry;
        that entry shadows the plaintext, so it must go (the bootstrap
        invariant) and the plaintext must carry the backup's generation."""
        from claude_swap import macos_keychain
        from claude_swap.reseed import RESEEDED, reseed_account

        monkeypatch.setattr(
            Platform, "detect", classmethod(lambda cls: Platform.MACOS)
        )
        s = _make_switcher()
        seed = _creds("at-seed", "rt-seed", expires=EXPIRED_MS)
        consumed = _creds("at-consumed", "rt-consumed", expires=EXPIRED_MS)
        backup = _creds("at-backup", "rt-backup")
        s.write_account_credentials(NUM, EMAIL, backup)
        # Stamp = the KEYCHAIN generation (an earlier adoption re-stamped it),
        # the plaintext below it is an even older seed.
        session_dir = _profile(s, seed, seed_of=consumed, marker=True)
        block_real_keychain.set_password(
            keychain_service_name(session_dir),
            macos_keychain.keychain_account_name(),
            consumed,
        )

        report = reseed_account(s, NUM)

        assert report.outcome == RESEEDED
        assert (
            block_real_keychain.get_password(
                keychain_service_name(session_dir),
                macos_keychain.keychain_account_name(),
            )
            is None
        )
        assert oauth.extract_access_token(_plaintext(session_dir)) == "at-backup"
        assert _stamp(session_dir) == oauth.credential_fingerprint(backup)

    def test_reseed_of_an_idle_profile_works_the_same(self, temp_home, post):
        from claude_swap.reseed import RESEEDED, reseed_account

        s = _make_switcher()
        _consumed, _backup, session_dir = _incident_slot(s)

        with patch.object(s, "_live_session_pids", return_value=[]):
            report = reseed_account(s, NUM)

        assert report.outcome == RESEEDED
        assert report.live_pids == []
        assert oauth.extract_access_token(_plaintext(session_dir)) == "at-backup"

    def test_wiped_profile_over_a_moved_backup_is_reseeded(
        self, temp_home, post, live
    ):
        """claude's invalid_grant wipe empties the token fields in place
        (observed on 2.1.181) — the profile holds no usable pair, the backup
        moved past the seed: the backup is the only generation left."""
        from claude_swap.reseed import RESEEDED, reseed_account

        s = _make_switcher()
        seed = _creds("at-seed", "rt-seed", expires=EXPIRED_MS)
        backup = _creds("at-backup", "rt-backup")
        s.write_account_credentials(NUM, EMAIL, backup)
        wiped = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})
        session_dir = _profile(s, wiped, seed_of=seed)

        report = reseed_account(s, NUM)

        assert report.outcome == RESEEDED
        assert oauth.extract_access_token(_plaintext(session_dir)) == "at-backup"


class TestProfileAhead:
    """(б) the profile rotated past the backup: the profile owns the newest
    generation — nothing is written into its store; the backup adopts it."""

    def test_live_profile_ahead_adopts_into_backup_and_leaves_the_profile(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import PROFILE_AHEAD, reseed_account

        s = _make_switcher()
        seed = _creds("at-seed", "rt-seed", expires=EXPIRED_MS)
        rotated = _creds("at-rotated", "rt-rotated")
        s.write_account_credentials(NUM, EMAIL, seed)
        session_dir = _profile(s, rotated, seed_of=seed)

        report = reseed_account(s, NUM)

        assert report.outcome == PROFILE_AHEAD
        assert report.reseeded is False
        assert report.live_pids == LIVE_PIDS
        assert _plaintext(session_dir) == rotated  # profile store untouched
        assert s.read_account_credentials(NUM, EMAIL) == rotated  # adopted
        assert _stamp(session_dir) == oauth.credential_fingerprint(rotated)
        # The backup-write hook marks a live profile stale on every backup
        # rewrite; after an adoption backup == profile, so the marker would
        # lie — it must not survive the door.
        assert not (session_dir / STALE_MARKER).exists()
        post.assert_not_called()

    def test_idle_profile_ahead_adopts_and_the_hook_drops_the_idle_copy(
        self, temp_home, post
    ):
        from claude_swap.reseed import PROFILE_AHEAD, reseed_account

        s = _make_switcher()
        seed = _creds("at-seed", "rt-seed", expires=EXPIRED_MS)
        rotated = _creds("at-rotated", "rt-rotated")
        s.write_account_credentials(NUM, EMAIL, seed)
        session_dir = _profile(s, rotated, seed_of=seed)

        with patch.object(s, "_live_session_pids", return_value=[]):
            report = reseed_account(s, NUM)

        assert report.outcome == PROFILE_AHEAD
        assert s.read_account_credentials(NUM, EMAIL) == rotated
        # One family, one copy (the codebase's backup-write invariant for an
        # idle profile): the next `cswap run` seeds it back from the backup.
        assert not (session_dir / ".credentials.json").exists()
        # A profile without credentials owns no family — the seed stamp the
        # adoption re-wrote must not survive, or it freezes the freshly
        # adopted backup ("the backup is the profile's consumed seed") for
        # the collector's seed guard, `cswap refresh` and this door
        # (review r.1, Major 1).
        assert not (session_dir / SEED_FINGERPRINT_FILE).exists()
        assert "dropped" in (report.detail or "")
        post.assert_not_called()

    def test_idle_profile_ahead_leaves_a_slot_the_door_and_refresh_can_use(
        self, temp_home, post
    ):
        """Review r.1 repro: after the idle adoption a second reseed answered
        `relogin-required` and `cswap refresh` `deferred` — the backup was
        frozen behind a stamp over no credential."""
        from claude_swap.refresh import FRESH, refresh_account
        from claude_swap.reseed import PROFILE_AHEAD, RESEEDED, reseed_account

        s = _make_switcher()
        seed = _creds("at-seed", "rt-seed", expires=EXPIRED_MS)
        rotated = _creds("at-rotated", "rt-rotated")
        s.write_account_credentials(NUM, EMAIL, seed)
        session_dir = _profile(s, rotated, seed_of=seed)

        with patch.object(s, "_live_session_pids", return_value=[]):
            first = reseed_account(s, NUM)
            second = reseed_account(s, NUM)
            refreshed = refresh_account(s, NUM)

        assert first.outcome == PROFILE_AHEAD
        # The dropped copy comes back from the adopted backup — no POST.
        assert second.outcome == RESEEDED
        assert _plaintext(session_dir) == rotated
        assert _stamp(session_dir) == oauth.credential_fingerprint(rotated)
        assert refreshed.outcome == FRESH
        post.assert_not_called()

    def test_same_generation_is_in_sync_and_nothing_is_written(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import IN_SYNC, reseed_account

        s = _make_switcher()
        same = _creds("at-same", "rt-same")
        s.write_account_credentials(NUM, EMAIL, same)
        session_dir = _profile(s, same, seed_of=same, marker=True)
        before = _plaintext(session_dir)

        report = reseed_account(s, NUM)

        assert report.outcome == IN_SYNC
        assert report.reseeded is False
        assert _plaintext(session_dir) == before
        assert (session_dir / STALE_MARKER).exists()  # not the door's call
        post.assert_not_called()


class TestRefusals:
    def test_token_profile_is_refused(self, temp_home, post, live):
        """(в) a token-seeded profile holds no login family — the backup
        login IS its family (CON-1329); nothing to reseed, nothing touched."""
        from claude_swap.reseed import TOKEN_PROFILE, ReseedRefusal, reseed_account

        s = _make_switcher()
        backup = _creds("at-backup", "rt-backup")
        s.write_account_credentials(NUM, EMAIL, backup)
        token_creds = inference_token_credentials("sk-ant-oat01-year-long")
        session_dir = _profile(s, token_creds, seed_of=token_creds)

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == TOKEN_PROFILE
        assert "inference token" in str(exc.value)
        assert _plaintext(session_dir) == token_creds
        post.assert_not_called()

    def test_expired_backup_whose_refresh_is_rejected_leaves_the_profile(
        self, temp_home, post, live
    ):
        """(г) the backup is expired: proof of life first — one POST with the
        BACKUP's grant (the profile's is consumed); rejected → refusal,
        profile untouched, lineage condemned in the store."""
        from claude_swap.reseed import RELOGIN_REQUIRED, ReseedRefusal, reseed_account

        s = _make_switcher()
        consumed, backup, session_dir = _incident_slot(s, backup_expires=EXPIRED_MS)
        post.return_value = oauth.RefreshOutcome(None, "invalid_grant")

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == RELOGIN_REQUIRED
        assert exc.value.live_pids == LIVE_PIDS
        post.assert_called_once()
        assert post.call_args.args[0] == backup
        assert _plaintext(session_dir) == consumed
        assert _stamp(session_dir) == oauth.credential_fingerprint(consumed)
        assert (session_dir / STALE_MARKER).exists()
        assert s.read_account_credentials(NUM, EMAIL) == backup
        assert s._usage_store.entries(IDENTITY)[NUM].token_dead()

    def test_expired_backup_is_refreshed_then_reseeded(self, temp_home, post, live):
        from claude_swap.reseed import RESEEDED, reseed_account

        s = _make_switcher()
        _consumed, backup, session_dir = _incident_slot(s, backup_expires=EXPIRED_MS)
        successor = _creds("at-successor", "rt-successor")
        post.return_value = oauth.RefreshOutcome(successor, None)

        report = reseed_account(s, NUM)

        assert report.outcome == RESEEDED
        assert report.generation == oauth.credential_fingerprint(successor)
        post.assert_called_once()
        assert post.call_args.args[0] == backup
        # The grant is consumed: the successor survives in BOTH stores.
        assert s.read_account_credentials(NUM, EMAIL) == successor
        assert oauth.extract_access_token(_plaintext(session_dir)) == "at-successor"
        assert _stamp(session_dir) == oauth.credential_fingerprint(successor)
        assert not (session_dir / STALE_MARKER).exists()

    def test_expired_backup_transient_refresh_failure_is_honest(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import TRANSIENT_ERROR, ReseedRefusal, reseed_account

        s = _make_switcher()
        consumed, _backup, session_dir = _incident_slot(s, backup_expires=EXPIRED_MS)
        post.return_value = oauth.RefreshOutcome(None, "transient")

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == TRANSIENT_ERROR
        assert _plaintext(session_dir) == consumed
        assert not s._usage_store.entries(IDENTITY)[NUM].token_dead()

    def test_condemned_backup_lineage_is_never_reseeded(self, temp_home, post, live):
        from claude_swap.reseed import RELOGIN_REQUIRED, ReseedRefusal, reseed_account

        s = _make_switcher()
        consumed, backup, session_dir = _incident_slot(s)
        s._usage_store.record(
            {
                NUM: FetchRecord(
                    error="invalid_grant",
                    credential_fingerprint=oauth.credential_fingerprint(backup),
                )
            },
            IDENTITY,
        )

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == RELOGIN_REQUIRED
        assert "condemned" in str(exc.value)
        assert _plaintext(session_dir) == consumed
        post.assert_not_called()

    def test_active_slot_is_refused_like_refresh_does(self, temp_home, post, live):
        """Review r.1, Major 2: the active default login's live credential is
        Claude Code's store; the stored copy is a consumed predecessor
        whenever CC rotated in place — POSTing or copying it strikes a LIVE
        login (`_refresh_resolved` refuses `active-slot` the same way)."""
        from claude_swap.reseed import ACTIVE_SLOT, ReseedRefusal, reseed_account

        s = _make_switcher()
        consumed, backup, session_dir = _incident_slot(s, backup_expires=EXPIRED_MS)

        with (
            patch.object(s, "_get_current_account", return_value=(EMAIL, "")),
            pytest.raises(ReseedRefusal) as exc,
        ):
            reseed_account(s, NUM)

        assert exc.value.outcome == ACTIVE_SLOT
        post.assert_not_called()
        assert _plaintext(session_dir) == consumed
        assert s.read_account_credentials(NUM, EMAIL) == backup

    def test_active_slot_with_another_org_is_not_this_slot(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import RESEEDED, reseed_account

        s = _make_switcher()
        _incident_slot(s)
        data = json.loads(s.sequence_file.read_text(encoding="utf-8"))
        data["accounts"][NUM]["organizationUuid"] = "org-2"
        s.sequence_file.write_text(json.dumps(data), encoding="utf-8")

        with patch.object(s, "_get_current_account", return_value=(EMAIL, "org-1")):
            report = reseed_account(s, NUM)

        assert report.outcome == RESEEDED

    def test_profile_is_read_under_its_credential_locks(self, temp_home, post, live):
        """Review r.1, Minor 3: the shared-fields merge writes back what the
        live claude holds NOW — the profile read must happen after both of
        Claude Code's credential locks for that config dir are held."""
        from contextlib import contextmanager

        from claude_swap import claude_locks, reseed

        s = _make_switcher()
        _incident_slot(s)
        events: list[str] = []
        real_lock = claude_locks.proper_lockfile
        real_read = reseed.read_profile_generation

        @contextmanager
        def recording_lock(lock_dir, **kwargs):
            with real_lock(lock_dir, **kwargs):
                events.append(f"lock:{lock_dir.name}")
                yield

        def recording_read(session_dir):
            events.append("read")
            return real_read(session_dir)

        with (
            patch.object(claude_locks, "proper_lockfile", recording_lock),
            patch.object(reseed, "read_profile_generation", recording_read),
        ):
            report = reseed.reseed_account(s, NUM)

        assert report.outcome == reseed.RESEEDED
        session_dir = s._session_dir(NUM, EMAIL)
        assert events[:3] == [
            "lock:.oauth_refresh.lock",
            f"lock:{session_dir.name}.lock",
            "read",
        ], events

    def test_no_profile_is_nothing_to_reseed(self, temp_home, post):
        from claude_swap.reseed import NO_PROFILE, ReseedRefusal, reseed_account

        s = _make_switcher()
        s.write_account_credentials(NUM, EMAIL, _creds("at-backup", "rt-backup"))

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == NO_PROFILE
        assert "nothing to reseed" in str(exc.value)

    def test_api_key_slot_is_refused(self, temp_home, post):
        from claude_swap.reseed import API_KEY, ReseedRefusal, reseed_account

        s = _make_switcher(kind="api_key")
        _profile(s, None, seed_of=None)

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == API_KEY

    def test_profile_logged_in_as_another_account_is_refused(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import IDENTITY_DRIFTED, ReseedRefusal, reseed_account

        s = _make_switcher()
        consumed, _backup, session_dir = _incident_slot(s)
        (session_dir / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "other@example.com"}}),
            encoding="utf-8",
        )

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == IDENTITY_DRIFTED
        assert _plaintext(session_dir) == consumed

    def test_unreadable_keychain_generation_defers(
        self, temp_home, post, live, block_real_keychain, monkeypatch
    ):
        """The profile's own newest generation cannot be read right now
        (Keychain busy): who ran ahead is undecidable — never overwrite what
        may be the family's newest generation (CON-1740)."""
        from claude_swap import macos_keychain
        from claude_swap.reseed import DEFERRED, ReseedRefusal, reseed_account

        monkeypatch.setattr(
            Platform, "detect", classmethod(lambda cls: Platform.MACOS)
        )
        s = _make_switcher()
        consumed, _backup, session_dir = _incident_slot(s)

        def busy(service, account):
            raise KeychainError("timeout")

        monkeypatch.setattr(macos_keychain, "get_password", busy)

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == DEFERRED
        assert _plaintext(session_dir) == consumed

    def test_unstamped_profile_with_a_different_generation_is_undecidable(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import UNDECIDABLE, ReseedRefusal, reseed_account

        s = _make_switcher()
        s.write_account_credentials(NUM, EMAIL, _creds("at-backup", "rt-backup"))
        other = _creds("at-other", "rt-other")
        session_dir = _profile(s, other, seed_of=None)

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == UNDECIDABLE
        assert _plaintext(session_dir) == other

    def test_wiped_profile_over_its_consumed_seed_needs_a_relogin(
        self, temp_home, post, live
    ):
        """Backup == the generation that seeded the profile, the profile's
        family died after rotating past it: the backup is the consumed
        predecessor — replaying it is the documented reuse signal."""
        from claude_swap.reseed import RELOGIN_REQUIRED, ReseedRefusal, reseed_account

        s = _make_switcher()
        seed = _creds("at-seed", "rt-seed")
        s.write_account_credentials(NUM, EMAIL, seed)
        wiped = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})
        session_dir = _profile(s, wiped, seed_of=seed)

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == RELOGIN_REQUIRED
        assert "consumed" in str(exc.value)
        assert _plaintext(session_dir) == wiped
        post.assert_not_called()

    def test_backup_without_a_token_pair_is_no_credentials(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import NO_CREDENTIALS, ReseedRefusal, reseed_account

        s = _make_switcher()
        s.write_account_credentials(NUM, EMAIL, _creds("at-only", None))
        consumed = _creds("at-consumed", "rt-consumed", expires=EXPIRED_MS)
        _profile(s, consumed, seed_of=consumed)

        with pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == NO_CREDENTIALS


class TestReseedCli:
    """(д) `cswap reseed <num|email> [--json]`."""

    def test_reseed_without_an_account_is_a_usage_error(self, temp_home, capsys):
        from claude_swap import cli

        with pytest.raises(SystemExit) as exc:
            cli._reseed_command([])

        assert exc.value.code == 2
        assert "NUM|EMAIL" in capsys.readouterr().err

    def test_reseed_json_payload(self, temp_home, capsys, live):
        from claude_swap import cli

        s = _make_switcher()
        _consumed, backup, _dir = _incident_slot(s)

        cli._reseed_command([NUM, "--json"])

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "schemaVersion": 1,
            "number": 2,
            "email": EMAIL,
            "reseeded": True,
            "outcome": "reseeded",
            "from": "backup",
            "generation": oauth.credential_fingerprint(backup),
            "livePids": LIVE_PIDS,
            "detail": None,
        }

    def test_reseed_json_error_envelope_on_refusal(self, temp_home, capsys, live):
        """A refusal in JSON mode is the switch-style error envelope on stdout
        (exit 1), carrying the stable outcome for scripts."""
        from claude_swap import cli

        s = _make_switcher()
        backup = _creds("at-backup", "rt-backup")
        s.write_account_credentials(NUM, EMAIL, backup)
        token_creds = inference_token_credentials("sk-ant-oat01-year-long")
        _profile(s, token_creds, seed_of=token_creds)

        with pytest.raises(SystemExit) as exc:
            cli._reseed_command([NUM, "--json"])

        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["schemaVersion"] == 1
        assert payload["error"]["type"] == "ReseedRefusal"
        assert payload["error"]["outcome"] == "token-profile"
        assert payload["error"]["livePids"] == LIVE_PIDS
        assert "inference token" in payload["error"]["message"]

    def test_reseed_json_error_envelope_on_unknown_account(self, temp_home, capsys):
        from claude_swap import cli

        _make_switcher()

        with pytest.raises(SystemExit) as exc:
            cli._reseed_command(["999", "--json"])

        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["type"] == "AccountNotFoundError"

    def test_reseed_human_output_names_the_live_pids(self, temp_home, capsys, live):
        from claude_swap import cli

        s = _make_switcher()
        _incident_slot(s)

        cli._reseed_command([NUM])

        out = capsys.readouterr().out
        assert "reseeded" in out
        assert "4242" in out and "4343" in out

    def test_main_dispatches_reseed(self, monkeypatch):
        from claude_swap import cli

        calls: list[list[str]] = []
        monkeypatch.setattr(cli, "_reseed_command", lambda argv: calls.append(argv))
        monkeypatch.setattr("sys.argv", ["cswap", "reseed", NUM, "--json"])

        cli.main()

        assert calls == [[NUM, "--json"]]

    def test_main_help_mentions_reseed(self, capsys):
        from claude_swap import cli

        with patch("sys.argv", ["cswap", "--help"]), pytest.raises(SystemExit):
            cli.main()

        assert "reseed <num|email>" in capsys.readouterr().out


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
    a spilled ADOPTION's profile cannot be read at the landing. The door read
    the profile fine a moment earlier (it holds the sidecar's predecessor —
    re-seeded from the old backup), so it reached the reconcile; it must
    refuse DEFERRED with the sidecar, the backup and the profile untouched."""

    def test_reseed_defers_when_the_landing_cannot_read_the_profile(
        self, temp_home, post, live
    ):
        from claude_swap.reseed import DEFERRED, ReseedRefusal, reseed_account
        from claude_swap.switcher import SPILL_ORIGIN_PROFILE

        s = _make_switcher()
        backup = _creds("at-backup", "rt-backup")
        s.write_account_credentials(NUM, EMAIL, backup)
        session_dir = _profile(s, backup, seed_of=backup)
        spill = s._pending_rotation_path(NUM)
        spill.parent.mkdir(parents=True, exist_ok=True)
        spill.write_text(
            json.dumps(
                {
                    "credentials": _creds("at-spilled", "rt-spilled"),
                    "predecessorFingerprint": oauth.credential_fingerprint(backup),
                    "email": EMAIL,
                    "createdAt": "2026-09-06T08:00:00Z",
                    "origin": SPILL_ORIGIN_PROFILE,
                }
            ),
            encoding="utf-8",
        )
        sidecar = spill.read_text(encoding="utf-8")

        with _keychain_busy_at_the_landing(s), pytest.raises(ReseedRefusal) as exc:
            reseed_account(s, NUM)

        assert exc.value.outcome == DEFERRED
        assert "deferred" in str(exc.value)
        post.assert_not_called()
        assert spill.read_text(encoding="utf-8") == sidecar  # sidecar untouched
        assert s.read_account_credentials(NUM, EMAIL) == backup
        assert _plaintext(session_dir) == backup
        assert _stamp(session_dir) == oauth.credential_fingerprint(backup)
        assert s.list_unclaimed_credentials() == {}
