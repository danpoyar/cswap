"""Tests for CON-1432 access parity: rules/plugins sharing, per-project
auto-memory links, and live-wins seeding of machine-shared MCP OAuth."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claude_swap import session as session_mod
from claude_swap.models import Platform
from claude_swap.session import (
    SHARE_MANIFEST,
    SHARED_ITEMS,
    STALE_MARKER,
    SessionManager,
    session_dir_for,
)
from claude_swap.switcher import ClaudeAccountSwitcher

ACCOUNT_EMAIL = "account2@example.com"
ACCOUNT_NUM = "2"
ORG_UUID = "org-uuid-2"

CREDS = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "stored-access",
            "refreshToken": "stored-refresh",
            "expiresAt": 1,
        }
    }
)
CONFIG = json.dumps(
    {
        "oauthAccount": {
            "emailAddress": ACCOUNT_EMAIL,
            "accountUuid": "uuid-2",
            "organizationUuid": ORG_UUID,
        },
        "theme": "light",
    }
)

LIVE_WITH_MCP = json.dumps(
    {
        "claudeAiOauth": {"accessToken": "live-access"},
        "mcpOAuth": {
            "figma|abc": {"accessToken": "live-figma"},
            "mobbin|def": {"accessToken": "live-mobbin"},
        },
    }
)


@pytest.fixture
def macos_platform(monkeypatch):
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))


@pytest.fixture
def seeded_switcher(temp_home: Path, macos_platform) -> ClaudeAccountSwitcher:
    switcher = ClaudeAccountSwitcher(debug=True)
    switcher._setup_directories()
    switcher._write_json(
        switcher.sequence_file,
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
    switcher._write_account_credentials(ACCOUNT_NUM, ACCOUNT_EMAIL, CREDS)
    switcher._write_account_config(ACCOUNT_NUM, ACCOUNT_EMAIL, CONFIG)
    return switcher


@pytest.fixture
def share_setup(temp_home: Path, seeded_switcher):
    """Source items in ~/.claude and an existing session dir (parity edition)."""
    source = temp_home / ".claude"
    (source / "settings.json").write_text("{}")
    session_dir = session_dir_for(
        seeded_switcher.backup_dir, ACCOUNT_NUM, ACCOUNT_EMAIL
    )
    session_dir.mkdir(parents=True)
    return source, session_dir, SessionManager(seeded_switcher)


def test_shared_items_cover_rules_and_plugins():
    """CON-1432: the parity classes rules/ and plugins/ are shared items."""
    assert "rules" in SHARED_ITEMS
    assert "plugins" in SHARED_ITEMS


@pytest.mark.skipif(sys.platform == "win32", reason="symlink mode is POSIX-only")
class TestRulesAndPluginsSharing:
    def test_links_rules_dir(self, share_setup):
        source, session_dir, mgr = share_setup
        (source / "rules").mkdir()
        (source / "rules" / "law.md").write_text("rule")

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "rules").is_symlink()
        assert (session_dir / "rules").readlink() == source / "rules"
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "rules" in manifest["items"]

    def test_stashes_preexisting_plugins_and_links(self, share_setup):
        source, session_dir, mgr = share_setup
        (source / "plugins").mkdir()
        (source / "plugins" / "installed_plugins.json").write_text("{}")
        (session_dir / "plugins").mkdir()
        (session_dir / "plugins" / "installed_plugins.json").write_text(
            '{"old": true}'
        )

        mgr._sync_sharing(session_dir, share=True)

        assert (session_dir / "plugins").is_symlink()
        assert (session_dir / "plugins").readlink() == source / "plugins"
        stashes = sorted(session_dir.glob("plugins.pre-share-*"))
        assert len(stashes) == 1
        assert (stashes[0] / "installed_plugins.json").read_text() == '{"old": true}'
        manifest = json.loads((session_dir / SHARE_MANIFEST).read_text())
        assert "plugins" in manifest["items"]

    def test_plugins_stash_skipped_under_live_session(self, share_setup, monkeypatch):
        source, session_dir, mgr = share_setup
        (source / "plugins").mkdir()
        (session_dir / "plugins").mkdir()
        (session_dir / "plugins" / "keep.json").write_text("{}")
        monkeypatch.setattr(
            session_mod, "live_sessions_for", lambda _dir: ["live"]
        )

        mgr._sync_sharing(session_dir, share=True)

        # Untouched under a live session: still a real dir, no stash.
        assert not (session_dir / "plugins").is_symlink()
        assert (session_dir / "plugins" / "keep.json").exists()
        assert list(session_dir.glob("plugins.pre-share-*")) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlink mode is POSIX-only")
class TestProjectMemorySharing:
    def test_links_every_source_memory(self, share_setup):
        source, session_dir, mgr = share_setup
        mem = source / "projects" / "-proj-a" / "memory"
        mem.mkdir(parents=True)
        (mem / "fact.md").write_text("m")

        mgr._sync_sharing(session_dir, share=True)

        dest = session_dir / "projects" / "-proj-a" / "memory"
        assert dest.is_symlink()
        assert dest.readlink() == mem

    def test_stashes_profile_local_memory(self, share_setup):
        source, session_dir, mgr = share_setup
        mem = source / "projects" / "-proj-a" / "memory"
        mem.mkdir(parents=True)
        local = session_dir / "projects" / "-proj-a" / "memory"
        local.mkdir(parents=True)
        (local / "own.md").write_text("local memory")

        mgr._sync_sharing(session_dir, share=True)

        dest = session_dir / "projects" / "-proj-a" / "memory"
        assert dest.is_symlink() and dest.readlink() == mem
        stashes = sorted((session_dir / "projects" / "-proj-a").glob("memory.local-*"))
        assert len(stashes) == 1
        assert (stashes[0] / "own.md").read_text() == "local memory"

    def test_local_memory_kept_under_live_session(self, share_setup, monkeypatch):
        source, session_dir, mgr = share_setup
        (source / "projects" / "-proj-a" / "memory").mkdir(parents=True)
        local = session_dir / "projects" / "-proj-a" / "memory"
        local.mkdir(parents=True)
        (local / "own.md").write_text("local memory")
        monkeypatch.setattr(
            session_mod, "live_sessions_for", lambda _dir: ["live"]
        )

        mgr._sync_sharing(session_dir, share=True)

        assert not local.is_symlink()
        assert (local / "own.md").exists()

    def test_share_history_projects_symlink_untouched(self, share_setup):
        source, session_dir, mgr = share_setup
        (source / "projects" / "-proj-a" / "memory").mkdir(parents=True)
        (session_dir / "projects").symlink_to(source / "projects")

        mgr._sync_project_memory(session_dir)

        # projects/ belongs to --share-history: no per-project links inside.
        assert (session_dir / "projects").is_symlink()

    def test_no_share_leaves_memory_alone(self, share_setup):
        source, session_dir, mgr = share_setup
        mem = source / "projects" / "-proj-a" / "memory"
        mem.mkdir(parents=True)

        mgr._sync_sharing(session_dir, share=False)

        assert not (session_dir / "projects" / "-proj-a" / "memory").exists()


class TestSharedCredentialSeeding:
    def test_missing_live_shared_keys_names_absent_keys(
        self, share_setup, monkeypatch
    ):
        source, session_dir, mgr = share_setup
        monkeypatch.setattr(
            mgr.switcher, "_read_credentials", lambda: LIVE_WITH_MCP
        )
        (session_dir / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {"accessToken": "p"},
                    "mcpOAuth": {"figma|abc": {"accessToken": "profile-figma"}},
                }
            )
        )

        assert mgr._missing_live_shared_keys(session_dir) == ["mcpOAuth:mobbin|def"]

    def test_value_drift_is_not_staleness(self, share_setup, monkeypatch):
        source, session_dir, mgr = share_setup
        monkeypatch.setattr(
            mgr.switcher, "_read_credentials", lambda: LIVE_WITH_MCP
        )
        (session_dir / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {"accessToken": "p"},
                    "mcpOAuth": {
                        "figma|abc": {"accessToken": "rotated-differently"},
                        "mobbin|def": {"accessToken": "also-different"},
                    },
                }
            )
        )

        assert mgr._missing_live_shared_keys(session_dir) == []

    def test_unreadable_sides_never_flag(self, share_setup, monkeypatch):
        source, session_dir, mgr = share_setup
        monkeypatch.setattr(mgr.switcher, "_read_credentials", lambda: None)
        assert mgr._missing_live_shared_keys(session_dir) == []

        monkeypatch.setattr(
            mgr.switcher, "_read_credentials", lambda: LIVE_WITH_MCP
        )
        # no profile credentials at all → bootstrap path owns seeding
        assert mgr._missing_live_shared_keys(session_dir) == []

    def test_setup_session_reseeds_on_missing_keys(self, share_setup, monkeypatch):
        """A valid profile lacking a live shared key re-bootstraps via the
        ordinary stale-marker machinery (adopt + invalidate + reseed)."""
        source, session_dir, mgr = share_setup
        calls: list[str] = []
        monkeypatch.setattr(
            mgr, "_missing_live_shared_keys", lambda _d: ["mcpOAuth:figma|abc"]
        )
        monkeypatch.setattr(session_mod, "live_sessions_for", lambda _d: [])
        monkeypatch.setattr(
            mgr, "_is_session_valid", lambda *a, **k: True
        )
        monkeypatch.setattr(
            mgr.switcher,
            "adopt_profile_family",
            lambda *a, **k: calls.append("adopt"),
        )
        monkeypatch.setattr(
            mgr.switcher,
            "_invalidate_session_credentials",
            lambda *a, **k: calls.append("invalidate"),
        )
        monkeypatch.setattr(
            mgr, "_bootstrap", lambda *a, **k: calls.append("bootstrap")
        )
        monkeypatch.setattr(
            mgr, "_sync_sharing", lambda *a, **k: calls.append("sync")
        )

        mgr.setup_session("2", share=True)

        assert "adopt" in calls and "invalidate" in calls
        assert not (session_dir / STALE_MARKER).exists()

    def test_bootstrap_seeds_live_shared_fields(self, share_setup, monkeypatch):
        """The profile seed carries the machine's CURRENT mcpOAuth set."""
        source, session_dir, mgr = share_setup
        monkeypatch.setattr(
            mgr.switcher, "_read_credentials", lambda: LIVE_WITH_MCP
        )
        # No refresh round-trip in this unit: the stored creds pass through.
        monkeypatch.setattr(
            mgr, "_seed_credentials_from_backup", lambda *a, **k: CREDS
        )

        mgr._bootstrap(session_dir, ACCOUNT_NUM, ACCOUNT_EMAIL, ORG_UUID)

        seeded = json.loads((session_dir / ".credentials.json").read_text())
        assert set(seeded["mcpOAuth"].keys()) == {"figma|abc", "mobbin|def"}
        assert seeded["mcpOAuth"]["figma|abc"]["accessToken"] == "live-figma"
        # The slot's own login family is untouched by the merge.
        assert seeded["claudeAiOauth"]["refreshToken"] == "stored-refresh"
