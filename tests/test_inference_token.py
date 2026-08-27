"""Year-long inference token attached to a login slot (CON-1329).

Contract (one case per letter, mirrored in the fleet ticket):
  (a) store: write → read roundtrip, 0600 file keyed by the identity slug,
      delete reports whether anything was removed, garbage reads as None;
  (b) shape gate: only a raw ``sk-ant-oat…`` attaches — API keys, JSON and
      empty strings are refused, an API-key slot is refused;
  (c) ``list --json`` carries the additive ``inferenceToken: true`` on the
      attached slot only, never the value; detach removes it;
  (d) an identity leaving the account table (remove) drops its token;
  (e) ``cswap run`` (session mode) seeds the profile with the token and
      exports ``CLAUDE_CODE_OAUTH_TOKEN``; the login's refresh family is
      never POSTed;
  (f) both modes: a DEAD login family (invalid_grant) is fatal without a
      token (CON-849) and NOT fatal with one — the session launches on the
      token with zero refresh attempts;
  (g) without a token the launch env carries no ``CLAUDE_CODE_OAUTH_TOKEN``
      and the profile is seeded from the login as before;
  (h) the active-login fast path never injects the token;
  (i) attaching invalidates an already-seeded profile so the next run
      re-seeds it with the token;
  (j) CLI: ``attach-token N -`` reads stdin and dispatches, ``detach-token N``
      dispatches, three positionals are an error;
  (k) ``list --token-status`` names the attached token.

Review round-1 fixes (commit 25c69e4):
  (l) after ``cswap run`` on a token slot the usage collector measures with
      the BACKUP login (``is_active=False``), never with the inference token;
  (m) attach/detach with a profile holding a rotated login family (profile
      newer than backup, seed stamp == backup's fingerprint) adopts that
      generation into backup before invalidating the profile — the consumed
      grant is never POSTed; a backup that moved past the seed is newer and
      is never overwritten;
  (n) ``cswap refresh`` on a token slot judges the BACKUP family — an expired
      backup refreshes to REFRESHED while the profile stays on the token;
  (o) detach performs the same adoption as attach;
  (p) an unreadable token file reads as "not attached" everywhere (store,
      ``list``, token status), and an unknown slot beats a malformed token
      in the attach error order.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_swap import cli
from claude_swap import oauth
from claude_swap import session as session_mod
from claude_swap.exceptions import SessionError, ValidationError
from claude_swap.inference_token import (
    delete_inference_token,
    inference_token_credentials,
    looks_like_inference_token,
    read_inference_token,
    token_path,
    write_inference_token,
)
from claude_swap.models import Platform
from claude_swap.oauth import RefreshOutcome
from claude_swap.session import SessionManager, session_dir_for
from claude_swap.switcher import ClaudeAccountSwitcher

EMAIL = "account2@example.com"
NUM = "2"
ORG = "org-uuid-2"
TOKEN = "sk-ant-oat01-" + "x" * 95
LOGIN_CREDS = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "stored-access",
            "refreshToken": "stored-refresh",
            "expiresAt": 1,
        }
    }
)
ROTATED = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "fresh-access",
            "refreshToken": "rotated-refresh",
            "expiresAt": 9999999999999,
        }
    }
)
CONFIG = json.dumps(
    {
        "oauthAccount": {
            "emailAddress": EMAIL,
            "accountUuid": "uuid-2",
            "organizationUuid": ORG,
        },
        "theme": "light",
    }
)


@pytest.fixture
def macos_platform(monkeypatch):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))


@pytest.fixture
def switcher(temp_home: Path, macos_platform) -> ClaudeAccountSwitcher:
    sw = ClaudeAccountSwitcher(debug=True)
    sw._setup_directories()
    sw._write_json(
        sw.sequence_file,
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
                NUM: {
                    "email": EMAIL,
                    "uuid": "uuid-2",
                    "organizationUuid": ORG,
                    "organizationName": "Org Two",
                    "added": "2024-01-02T00:00:00Z",
                },
            },
        },
    )
    sw._write_account_credentials(NUM, EMAIL, LOGIN_CREDS)
    sw._write_account_config(NUM, EMAIL, CONFIG)
    sw._write_account_credentials("1", "account1@example.com", LOGIN_CREDS)
    sw._write_account_config("1", "account1@example.com", CONFIG)
    return sw


@pytest.fixture
def manager(switcher) -> SessionManager:
    return SessionManager(switcher)


class _ExecCalled(Exception):
    def __init__(self, binary, argv, env):
        self.binary, self.argv, self.env = binary, argv, env


@pytest.fixture
def capture_exec(monkeypatch):
    def fake_exec(self, claude_bin, claude_args, env):
        raise _ExecCalled(claude_bin, [claude_bin, *claude_args], env)

    monkeypatch.setattr(session_mod.SessionManager, "_exec", fake_exec)
    monkeypatch.setattr(session_mod.shutil, "which", lambda name: f"/fake/bin/{name}")


@pytest.fixture
def auth_status_tracks_seed(monkeypatch):
    """`claude auth status --json`: logged in iff the profile holds credentials."""

    def fake_run(cmd, env=None, **kwargs):
        config_dir = Path(env["CLAUDE_CONFIG_DIR"])
        if (config_dir / ".credentials.json").exists():
            payload = {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "email": EMAIL,
                "orgId": ORG,
            }
        else:
            payload = {"loggedIn": False, "authMethod": "none"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(session_mod.subprocess, "run", fake_run)


@pytest.fixture
def refresh_calls(monkeypatch):
    """Record every refresh POST the bootstrap makes (live family → rotates)."""
    calls: list[str] = []

    def fake(creds: str) -> RefreshOutcome:
        calls.append(creds)
        return RefreshOutcome(credentials=ROTATED, error=None)

    monkeypatch.setattr(session_mod, "try_refresh_oauth_credentials", fake)
    return calls


@pytest.fixture
def dead_family(monkeypatch):
    """The token endpoint answers invalid_grant to every refresh POST."""
    calls: list[str] = []

    def fake(creds: str) -> RefreshOutcome:
        calls.append(creds)
        return RefreshOutcome(credentials=None, error="invalid_grant")

    monkeypatch.setattr(session_mod, "try_refresh_oauth_credentials", fake)
    return calls


# --- (a) store ----------------------------------------------------------------


class TestStore:
    def test_roundtrip_0600_and_slug(self, tmp_path: Path):
        path = write_inference_token(tmp_path, EMAIL, TOKEN + "\n")
        assert path == token_path(tmp_path, EMAIL)
        assert path.name == "account2_example.com.enc"
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert read_inference_token(tmp_path, EMAIL) == TOKEN
        assert TOKEN not in path.read_text()  # base64 at rest, like the .enc backups

    def test_delete_reports_presence(self, tmp_path: Path):
        assert delete_inference_token(tmp_path, EMAIL) is False
        write_inference_token(tmp_path, EMAIL, TOKEN)
        assert delete_inference_token(tmp_path, EMAIL) is True
        assert read_inference_token(tmp_path, EMAIL) is None

    def test_garbage_reads_as_none(self, tmp_path: Path):
        path = token_path(tmp_path, EMAIL)
        path.parent.mkdir(parents=True)
        path.write_text("not base64 !!!")
        assert read_inference_token(tmp_path, EMAIL) is None
        path.write_text("")
        assert read_inference_token(tmp_path, EMAIL) is None

    def test_credentials_shape(self):
        data = json.loads(inference_token_credentials(TOKEN + " "))
        assert data == {
            "claudeAiOauth": {"accessToken": TOKEN, "scopes": ["user:inference"]}
        }


# --- (b) shape gate -----------------------------------------------------------


class TestAttachGate:
    @pytest.mark.parametrize(
        "bad",
        ["   ", "sk-ant-api03-key", '{"claudeAiOauth": {}}', "hello"],
    )
    def test_refuses_non_setup_tokens(self, switcher, bad):
        with pytest.raises(ValidationError):
            switcher.attach_inference_token(NUM, bad)
        assert not switcher.has_inference_token(EMAIL)

    def test_empty_prompt_answer_is_refused(self, switcher, monkeypatch):
        import getpass

        monkeypatch.setattr(getpass, "getpass", lambda prompt="": "")
        with pytest.raises(ValidationError, match="cannot be empty"):
            switcher.attach_inference_token(NUM, "")
        assert not switcher.has_inference_token(EMAIL)

    def test_looks_like(self):
        assert looks_like_inference_token(TOKEN)
        assert not looks_like_inference_token("sk-ant-api03-x")
        assert not looks_like_inference_token('{"claudeAiOauth": 1}')
        assert not looks_like_inference_token(None)

    def test_refuses_api_key_slot(self, switcher):
        data = switcher._get_sequence_data()
        data["accounts"][NUM]["kind"] = "api_key"
        switcher._write_json(switcher.sequence_file, data)
        with pytest.raises(ValidationError, match="API-key account"):
            switcher.attach_inference_token(NUM, TOKEN)

    def test_unknown_slot(self, switcher):
        from claude_swap.exceptions import AccountNotFoundError

        with pytest.raises(AccountNotFoundError):
            switcher.attach_inference_token("9", TOKEN)


# --- (c) list --json ----------------------------------------------------------


def _rows(switcher):
    payload = switcher.list_accounts(json_output=True)
    return {row["number"]: row for row in payload["accounts"]}


class TestListJson:
    def test_field_only_on_attached_slot_and_never_the_value(
        self, switcher, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            switcher, "_collect_usage_entries", _no_usage_entries(switcher)
        )
        switcher.attach_inference_token(NUM, TOKEN)
        assert "Attached" in capsys.readouterr().out
        rows = _rows(switcher)
        assert rows[2]["inferenceToken"] is True
        assert "inferenceToken" not in rows[1]
        assert TOKEN not in json.dumps(rows)

    def test_detach_drops_field(self, switcher, monkeypatch, capsys):
        monkeypatch.setattr(
            switcher, "_collect_usage_entries", _no_usage_entries(switcher)
        )
        switcher.attach_inference_token(NUM, TOKEN)
        switcher.detach_inference_token(NUM)
        assert "Detached" in capsys.readouterr().out
        assert "inferenceToken" not in _rows(switcher)[2]
        with pytest.raises(ValidationError, match="no attached inference token"):
            switcher.detach_inference_token(NUM)


def _no_usage_entries(switcher):
    """Hermetic usage collection: every slot 'no credentials', nothing fetched."""
    from claude_swap.usage_store import UsageEntry

    def fake(accounts_info, fetch=None, **_kw):
        return {str(num): UsageEntry() for num, *_rest in accounts_info}

    return fake


# --- (d) identity leaves → token gone -----------------------------------------


class TestPrune:
    def test_remove_account_drops_token(self, switcher, capsys):
        switcher.attach_inference_token(NUM, TOKEN)
        switcher.remove_account(NUM, assume_yes=True)
        assert not switcher.has_inference_token(EMAIL)
        assert "Removed the attached inference token" in capsys.readouterr().out


# --- (e)(f)(g)(h)(i) session mode ---------------------------------------------


class TestRun:
    def test_seeds_profile_with_token_and_exports_env(
        self, manager, switcher, capture_exec, auth_status_tracks_seed, refresh_calls, capsys
    ):
        switcher.attach_inference_token(NUM, TOKEN)
        with pytest.raises(_ExecCalled) as exc:
            manager.run(NUM, ["--resume"])
        env = exc.value.env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
        session_dir = session_dir_for(switcher.backup_dir, NUM, EMAIL)
        assert env["CLAUDE_CONFIG_DIR"] == str(session_dir)
        seeded = json.loads((session_dir / ".credentials.json").read_text())
        assert seeded["claudeAiOauth"]["accessToken"] == TOKEN
        assert "refreshToken" not in seeded["claudeAiOauth"]
        # the login's family is never POSTed from a token slot
        assert refresh_calls == []
        out = capsys.readouterr().out
        assert "Inference token attached" in out
        assert TOKEN not in out
        # the login stays in backup untouched — it is the quota gauge
        assert switcher.read_account_credentials(NUM, EMAIL) == LOGIN_CREDS

    def test_dead_family_is_fatal_without_token(
        self, manager, switcher, capture_exec, auth_status_tracks_seed, dead_family
    ):
        with pytest.raises(SessionError, match="invalid_grant"):
            manager.run(NUM, [])
        assert len(dead_family) == 1

    def test_dead_family_launches_on_token(
        self, manager, switcher, capture_exec, auth_status_tracks_seed, dead_family
    ):
        switcher.attach_inference_token(NUM, TOKEN)
        with pytest.raises(_ExecCalled) as exc:
            manager.run(NUM, [])
        assert exc.value.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
        assert dead_family == []  # zero POSTs of a dead grant

    def test_without_token_unchanged(
        self, manager, switcher, capture_exec, auth_status_tracks_seed, refresh_calls
    ):
        with pytest.raises(_ExecCalled) as exc:
            manager.run(NUM, [])
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in exc.value.env
        session_dir = session_dir_for(switcher.backup_dir, NUM, EMAIL)
        assert (session_dir / ".credentials.json").read_text() == ROTATED
        assert len(refresh_calls) == 1

    def test_fast_path_never_injects(
        self, manager, switcher, capture_exec, monkeypatch, capsys
    ):
        switcher.attach_inference_token(NUM, TOKEN)
        monkeypatch.setattr(switcher, "_get_current_account", lambda: (EMAIL, ORG))
        with pytest.raises(_ExecCalled) as exc:
            manager.run(NUM, [])
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in exc.value.env
        assert "CLAUDE_CONFIG_DIR" not in exc.value.env

    def test_attach_reseeds_existing_profile(
        self, manager, switcher, capture_exec, auth_status_tracks_seed, refresh_calls
    ):
        # 1) profile seeded from the login
        with pytest.raises(_ExecCalled):
            manager.run(NUM, [])
        session_dir = session_dir_for(switcher.backup_dir, NUM, EMAIL)
        assert (session_dir / ".credentials.json").read_text() == ROTATED
        # 2) attach → profile invalidated → next run re-seeds with the token
        switcher.attach_inference_token(NUM, TOKEN)
        with pytest.raises(_ExecCalled) as exc:
            manager.run(NUM, [])
        seeded = json.loads((session_dir / ".credentials.json").read_text())
        assert seeded["claudeAiOauth"]["accessToken"] == TOKEN
        assert exc.value.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
        assert len(refresh_calls) == 1  # only the first, token-less run


# --- (j) CLI -----------------------------------------------------------------


class TestCli:
    def test_attach_reads_stdin_and_dispatches(self, temp_home: Path, monkeypatch):
        monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN + "\n"))
        with patch.object(sys, "argv", ["claude-swap", "attach-token", "2", "-"]), patch.object(
            ClaudeAccountSwitcher, "attach_inference_token"
        ) as mock_attach:
            cli.main()
        mock_attach.assert_called_once_with("2", "-")

    def test_attach_without_token_prompts(self, temp_home: Path):
        with patch.object(sys, "argv", ["claude-swap", "attach-token", "2"]), patch.object(
            ClaudeAccountSwitcher, "attach_inference_token"
        ) as mock_attach:
            cli.main()
        mock_attach.assert_called_once_with("2", "")

    def test_attach_three_positionals_is_an_error(self, temp_home: Path, capsys):
        with patch.object(
            sys, "argv", ["claude-swap", "attach-token", "2", "tok", "extra"]
        ):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "attach-token takes" in capsys.readouterr().err

    def test_detach_dispatches(self, temp_home: Path):
        with patch.object(sys, "argv", ["claude-swap", "detach-token", "2"]), patch.object(
            ClaudeAccountSwitcher, "detach_inference_token"
        ) as mock_detach:
            cli.main()
        mock_detach.assert_called_once_with("2")

    def test_stdin_token_reaches_the_store(self, switcher, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN + "\n"))
        switcher.attach_inference_token(NUM, "-")
        assert read_inference_token(switcher.backup_dir, EMAIL) == TOKEN


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def readline(self) -> str:
        text, self._text = self._text, ""
        return text


# --- (k) token status ---------------------------------------------------------


class TestTokenStatus:
    def test_line_names_attached_token(self, switcher):
        switcher.attach_inference_token(NUM, TOKEN)
        info = (2, EMAIL, "Org Two", ORG, False, LOGIN_CREDS, "")
        lines = switcher._token_status_lines(info)
        assert any("inference token: attached" in line for line in lines)
        assert all(TOKEN not in line for line in lines)
        info1 = (1, "account1@example.com", "Org One", "org-uuid-1", False, LOGIN_CREDS, "")
        assert not any("inference token" in line for line in switcher._token_status_lines(info1))


# --- review round-1 fixes ------------------------------------------------------

USAGE = {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 40.0}}


def _seed_profile_with_family(switcher, credentials: str, seed_of: str) -> Path:
    """A session profile holding a login FAMILY (claude rotated it in place),
    with this slot's identity and a seed stamp naming ``seed_of``'s
    generation — the shape adopt_profile_family exists for."""
    session_dir = session_dir_for(switcher.backup_dir, NUM, EMAIL)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / ".credentials.json").write_text(credentials, encoding="utf-8")
    (session_dir / ".claude.json").write_text(CONFIG, encoding="utf-8")
    (session_dir / ".seed-fingerprint").write_text(
        oauth.credential_fingerprint(seed_of) or "", encoding="utf-8"
    )
    return session_dir


class TestCollectorOnTokenSlot:
    """(l) The quota gauge is the backup login, never the inference token."""

    def test_collector_measures_with_backup_login(
        self, manager, switcher, capture_exec, auth_status_tracks_seed
    ):
        switcher.attach_inference_token(NUM, TOKEN)
        with pytest.raises(_ExecCalled):
            manager.run(NUM, [])
        # the profile now runs on the token (no login family, no expiresAt)
        session_dir = session_dir_for(switcher.backup_dir, NUM, EMAIL)
        seeded = json.loads((session_dir / ".credentials.json").read_text())
        assert seeded["claudeAiOauth"]["accessToken"] == TOKEN

        info = [(2, EMAIL, "Org Two", ORG, False, LOGIN_CREDS, "")]
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome(USAGE),
        ) as fetch:
            entries = switcher._collect_usage_entries(info, fetch={NUM})
        fetch.assert_called_once()
        measured_creds = fetch.call_args.args[2]
        assert measured_creds == LOGIN_CREDS
        assert TOKEN not in measured_creds
        # read-only active mode is for profiles that OWN a family; the token
        # profile does not — the backup may be refreshed and persisted here
        assert fetch.call_args.kwargs["is_active"] is False
        assert entries[NUM].sentinel is None


class TestAdoptProfileFamily:
    """(m)(o) The profile's newer login generation survives attach/detach."""

    def test_attach_adopts_rotated_profile_family(self, switcher, capsys):
        _seed_profile_with_family(switcher, ROTATED, seed_of=LOGIN_CREDS)
        switcher.attach_inference_token(NUM, TOKEN)
        # backup now holds the profile's newer generation: the collector will
        # POST the live grant, not the consumed predecessor
        assert switcher.read_account_credentials(NUM, EMAIL) == ROTATED
        assert "Attached" in capsys.readouterr().out

    def test_attach_keeps_readded_backup(self, switcher):
        # the backup moved past the profile's seed (account re-added since):
        # the backup IS the newer family — never overwritten by the stale
        # profile generation
        _seed_profile_with_family(switcher, ROTATED, seed_of='{"claudeAiOauth": {"accessToken": "other", "refreshToken": "other-rt", "expiresAt": 5}}')
        switcher.attach_inference_token(NUM, TOKEN)
        assert switcher.read_account_credentials(NUM, EMAIL) == LOGIN_CREDS

    def test_detach_adopts_rotated_profile_family(self, switcher):
        switcher.attach_inference_token(NUM, TOKEN)
        # a stale profile from before the attach still holds the family
        _seed_profile_with_family(switcher, ROTATED, seed_of=LOGIN_CREDS)
        switcher.detach_inference_token(NUM)
        assert switcher.read_account_credentials(NUM, EMAIL) == ROTATED

    def test_token_profile_is_never_adopted(self, manager, switcher, capture_exec, auth_status_tracks_seed):
        # a token-seeded profile owns no family — detach must not fold the
        # bare inference token into the backup login
        switcher.attach_inference_token(NUM, TOKEN)
        with pytest.raises(_ExecCalled):
            manager.run(NUM, [])
        switcher.detach_inference_token(NUM)
        assert switcher.read_account_credentials(NUM, EMAIL) == LOGIN_CREDS


class TestRefreshOnTokenSlot:
    """(n) ``cswap refresh`` judges the backup family; the profile keeps the token."""

    def test_refresh_rotates_backup_and_slot_stays_on_token(
        self, manager, switcher, capture_exec, auth_status_tracks_seed, refresh_calls
    ):
        from claude_swap.refresh import REFRESHED, refresh_account

        switcher.attach_inference_token(NUM, TOKEN)
        with pytest.raises(_ExecCalled):
            manager.run(NUM, [])
        session_dir = session_dir_for(switcher.backup_dir, NUM, EMAIL)

        with (
            patch(
                "claude_swap.refresh.try_refresh_oauth_credentials",
                return_value=oauth.RefreshOutcome(ROTATED, None),
            ) as post,
            patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                return_value=oauth.UsageOutcome(USAGE),
            ),
        ):
            report = refresh_account(switcher, NUM)

        assert report.outcome == REFRESHED
        # the expired BACKUP login was judged and consumed — not the token,
        # and never the profile (before the fix: "no refresh token" →
        # relogin-required on a healthy slot)
        post.assert_called_once()
        assert post.call_args.args[0] == LOGIN_CREDS
        assert switcher.read_account_credentials(NUM, EMAIL) == ROTATED
        # the backup write invalidates the profile (the attach canon, case i);
        # the next run re-seeds it with the token — the slot stays on the
        # token and the fresh family is never POSTed by the bootstrap
        with pytest.raises(_ExecCalled) as exc:
            manager.run(NUM, [])
        assert exc.value.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
        seeded = json.loads((session_dir / ".credentials.json").read_text())
        assert seeded["claudeAiOauth"]["accessToken"] == TOKEN
        assert "refreshToken" not in seeded["claudeAiOauth"]
        assert refresh_calls == []


class TestBrokenTokenFile:
    """(p) An unreadable token file is "not attached" everywhere."""

    def _break_token_file(self, switcher):
        path = token_path(switcher.backup_dir, EMAIL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not base64 !!!")

    def test_has_inference_token_false_on_unreadable_file(self, switcher):
        self._break_token_file(switcher)
        assert switcher.has_inference_token(EMAIL) is False
        assert switcher.inference_token_for(NUM, EMAIL) is None

    def test_list_and_status_do_not_claim_attachment(self, switcher, monkeypatch):
        monkeypatch.setattr(
            switcher, "_collect_usage_entries", _no_usage_entries(switcher)
        )
        self._break_token_file(switcher)
        assert "inferenceToken" not in _rows(switcher)[2]
        info = (2, EMAIL, "Org Two", ORG, False, LOGIN_CREDS, "")
        assert not any(
            "inference token" in line for line in switcher._token_status_lines(info)
        )

    def test_unknown_slot_beats_malformed_token(self, switcher):
        from claude_swap.exceptions import AccountNotFoundError

        with pytest.raises(AccountNotFoundError):
            switcher.attach_inference_token("9", "not-a-token")
