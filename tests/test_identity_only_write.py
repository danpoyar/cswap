"""An identity-only write never seeds a slot backup with another slot's
token pair (CON-2332).

Forensics of the 2026-09-05 episode (CON-2323): something wrote ONE
identity (``oauthAccount`` of Account-B) into ``~/.claude.json`` while the
live credential store kept Account-A's token pair. The engine judges "the
real login" by that identity alone, so it (1) adopted the record A→B and
(2) ``_resync_rotated_backup`` — identity re-check green, full pair present,
lineage differing from B's backup — wrote A's pair INTO B's backup. The
home-pin sensor then switched back to A with the older generation, the
family's next refresh answered ``invalid_grant`` and Claude Code wiped the
live store: failover cascades for the rest of the evening.

The lineage guard: when the live pair's fingerprint matches the backup of a
slot OTHER than the one the identity names, the write is identity-only —
the adoption is refused (record kept), the resync is refused (backup
untouched), a WARN names both slots once per episode, and the engine says
so with one ``identity-only-write`` event.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

from claude_swap import oauth
from claude_swap.autoswitch import AdoptRealLoginEvent, TickOutcome
from tests.test_autoswitch import EngineHarness, _usage

EMAILS = {1: "a@example.com", 2: "b@example.com", 3: "c@example.com"}
HEALTHY = {"1": _usage(10), "2": _usage(10), "3": _usage(10)}


def _pair(num: int | str) -> str:
    """The token pair ``EngineHarness.seed`` stores for a slot (same bytes)."""
    return json.dumps({
        "claudeAiOauth": {"accessToken": f"sk-{num}", "refreshToken": f"rt-{num}"},
    })


def _write_live(h: EngineHarness, *, identity: int, pair: str) -> None:
    """Live store = ``pair`` bytes; ``~/.claude.json`` names ``identity``."""
    (h.temp_home / ".claude" / ".credentials.json").write_text(pair)
    (h.temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": EMAILS[identity],
            "accountUuid": f"uuid-{identity}",
        },
    }))


def _harness(
    temp_home: Path, *, identity: int, pair_of: int, record: int
) -> EngineHarness:
    """Three seeded slots; the live store carries slot ``pair_of``'s pair
    while the config identity names slot ``identity``; record says
    ``record``. ``identity == pair_of`` is an ordinary consistent login."""
    h = EngineHarness(temp_home)
    for num, email in EMAILS.items():
        h.seed(num, email)
    _write_live(h, identity=identity, pair=_pair(pair_of))
    data = h.switcher._get_sequence_data()
    assert data is not None
    data["activeAccountNumber"] = record
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _adopts(h: EngineHarness) -> list[AdoptRealLoginEvent]:
    return [e for e in h.events if isinstance(e, AdoptRealLoginEvent)]


def _identity_only_events(h: EngineHarness) -> list:
    from claude_swap.autoswitch import IdentityOnlyWriteEvent

    return [e for e in h.events if isinstance(e, IdentityOnlyWriteEvent)]


class _Warnings(logging.Handler):
    """Collects WARNING+ records straight off the switcher's logger (the
    ``claude-swap`` logger does not propagate to root)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _capture(h: EngineHarness) -> _Warnings:
    handler = _Warnings()
    h.switcher._logger.addHandler(handler)
    return handler


def _is_identity_only_warning(msg: str, *, owner: int, identity: int) -> bool:
    return (
        "identity-only write" in msg
        and f"belongs to Account-{owner}" in msg
        and f"identity says Account-{identity}" in msg
    )


# ---------------------------------------------------------------------------
# The resync path (the write that poisoned Account-23's backup on 05-09).
# ---------------------------------------------------------------------------


class TestResyncIdentityOnly:
    def test_resync_refuses_identity_only_write(self, temp_home):
        # Live store = Account-1's pair, identity says Account-3, record 1
        # (the measured shape: the recorded active slot owns the live pair).
        h = _harness(temp_home, identity=3, pair_of=1, record=1)
        sw = h.switcher
        warnings = _capture(h)
        live = sw._read_credentials()
        assert live == _pair(1)

        sw._resync_rotated_backup("3", EMAILS[3], "", live)

        # Account-3's backup keeps its own pair; Account-1's is untouched.
        assert sw._read_account_credentials("3", EMAILS[3]) == _pair(3)
        assert sw._read_account_credentials("1", EMAILS[1]) == _pair(1)
        assert any(
            _is_identity_only_warning(m, owner=1, identity=3)
            for m in warnings.messages
        ), warnings.messages

    def test_resync_identity_only_scan_covers_slots_other_than_the_record(
        self, temp_home,
    ):
        # The owning slot is neither the identity nor the recorded active
        # slot: the guard must look at every managed slot, not just the
        # record's prior.
        h = _harness(temp_home, identity=3, pair_of=2, record=1)
        sw = h.switcher
        warnings = _capture(h)

        sw._resync_rotated_backup("3", EMAILS[3], "", sw._read_credentials())

        assert sw._read_account_credentials("3", EMAILS[3]) == _pair(3)
        assert any(
            _is_identity_only_warning(m, owner=2, identity=3)
            for m in warnings.messages
        ), warnings.messages

    def test_resync_identity_only_refuses_on_lineage_not_bytes(self, temp_home):
        # A rotated generation of Account-1's family (same refresh token,
        # new access token) is still Account-1's lineage.
        h = _harness(temp_home, identity=3, pair_of=1, record=1)
        sw = h.switcher
        rotated = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-1-rotated", "refreshToken": "rt-1",
        }})
        _write_live(h, identity=3, pair=rotated)
        assert oauth.credential_fingerprint(rotated) == oauth.credential_fingerprint(
            _pair(1)
        )

        sw._resync_rotated_backup("3", EMAILS[3], "", rotated)

        assert sw._read_account_credentials("3", EMAILS[3]) == _pair(3)

    def test_resync_still_adopts_a_rotation_nobody_else_owns(self, temp_home):
        # Positive control (the fast path this guard sits on): a lineage no
        # other slot's backup carries is a genuine rotation-before-collection
        # of the identity's own family — the backup IS resynced.
        h = _harness(temp_home, identity=3, pair_of=3, record=3)
        sw = h.switcher
        warnings = _capture(h)
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-3-next", "refreshToken": "rt-3-next",
        }})
        _write_live(h, identity=3, pair=fresh)

        sw._resync_rotated_backup("3", EMAILS[3], "", fresh)

        assert sw._read_account_credentials("3", EMAILS[3]) == fresh
        assert warnings.messages == []

    def test_fetch_active_usage_identity_only_leaves_backup_alone(self, temp_home):
        # Through the caller: the server accepts Account-1's pair (it is a
        # valid token), the fast path reaches the resync — and the resync
        # must not seed Account-3's backup with it.
        h = _harness(temp_home, identity=3, pair_of=1, record=1)
        sw = h.switcher
        usage_result = {
            "five_hour": {"pct": 10, "clock": "Jan 1 03:00", "countdown": "0m"},
            "seven_day": {"pct": 50, "clock": "Jan 2 03:00", "countdown": "0m"},
        }
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            return_value=oauth.UsageOutcome(usage_result),
        ):
            record = sw._fetch_active_usage("3", EMAILS[3], _pair(1), "")

        assert record.usage == usage_result
        assert sw._read_account_credentials("3", EMAILS[3]) == _pair(3)


# ---------------------------------------------------------------------------
# The adoption path (the record must not follow an identity-only write).
# ---------------------------------------------------------------------------


class TestAdoptIdentityOnly:
    def test_adopt_refuses_identity_only_write(self, temp_home):
        h = _harness(temp_home, identity=3, pair_of=1, record=2)
        warnings = _capture(h)

        adopted, prior = h.switcher.adopt_active_account("3")

        assert adopted is False
        assert prior == "2"
        assert h.active_number() == 2
        assert h.switcher.identity_only_write == {
            "identity": "3", "owner": "1", "recorded": "2",
        }
        assert any(
            _is_identity_only_warning(m, owner=1, identity=3)
            for m in warnings.messages
        ), warnings.messages

    def test_adopt_identity_only_state_clears_when_the_login_is_consistent(
        self, temp_home,
    ):
        h = _harness(temp_home, identity=3, pair_of=1, record=2)
        h.switcher.adopt_active_account("3")
        assert h.switcher.identity_only_write is not None

        # The real login lands (identity and pair agree): adopted, state gone.
        _write_live(h, identity=3, pair=_pair(3))
        adopted, _ = h.switcher.adopt_active_account("3")
        assert adopted is True
        assert h.switcher.identity_only_write is None

    def test_adopt_with_an_empty_live_store_still_follows_the_identity(
        self, temp_home,
    ):
        # No pair at all → nothing to attribute, nothing to poison: the
        # CON-1581 adoption keeps working on identity alone.
        h = _harness(temp_home, identity=3, pair_of=1, record=2)
        (h.temp_home / ".claude" / ".credentials.json").write_text("")
        adopted, prior = h.switcher.adopt_active_account("3")
        assert adopted is True
        assert prior == "2"
        assert h.switcher.identity_only_write is None


class TestEngineIdentityOnly:
    def test_identity_only_write_is_not_adopted_and_is_reported(self, temp_home):
        h = _harness(temp_home, identity=3, pair_of=1, record=1)
        warnings = _capture(h)

        outcome = h.tick_with_usage(HEALTHY)

        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1  # record kept at the pair's owner
        assert _adopts(h) == []
        (event,) = _identity_only_events(h)
        payload = event.to_json()
        assert payload["event"] == "identity-only-write"
        assert payload["identity"] == {"number": 3, "email": EMAILS[3]}
        assert payload["owner"] == {"number": 1, "email": EMAILS[1]}
        assert payload["recorded"] == {"number": 1, "email": EMAILS[1]}
        human = event.human()
        assert human.startswith("WARN")
        assert "Account-1" in human and "Account-3" in human
        assert h.switcher._read_account_credentials("3", EMAILS[3]) == _pair(3)
        assert sum(
            _is_identity_only_warning(m, owner=1, identity=3)
            for m in warnings.messages
        ) == 1

    def test_identity_only_write_is_reported_once_per_episode(self, temp_home):
        h = _harness(temp_home, identity=3, pair_of=1, record=1)
        warnings = _capture(h)

        h.tick_with_usage(HEALTHY)
        h.clock.advance(60)
        h.tick_with_usage(HEALTHY)
        assert len(_identity_only_events(h)) == 1
        assert sum(
            _is_identity_only_warning(m, owner=1, identity=3)
            for m in warnings.messages
        ) == 1

        # The sensor brings the login home (identity 1, pair 1): silence.
        _write_live(h, identity=1, pair=_pair(1))
        h.clock.advance(60)
        h.tick_with_usage(HEALTHY)
        assert len(_identity_only_events(h)) == 1
        assert h.switcher.identity_only_write is None

        # A second episode is a new report.
        _write_live(h, identity=3, pair=_pair(1))
        h.clock.advance(60)
        h.tick_with_usage(HEALTHY)
        assert len(_identity_only_events(h)) == 2
        assert h.active_number() == 1

    def test_identity_only_write_takes_the_forensic_snapshot(self, temp_home):
        # CON-2323 hunts the writer of exactly this shape; with the adoption
        # refused there is no adopt-real-login to hang its snapshot on, so
        # the snapshot follows the identity-only report (once per episode).
        from claude_swap.autoswitch import AdoptSnapshotEvent

        calls: list[dict] = []

        def fake_snapshot(**kwargs):
            calls.append(kwargs)
            return {"processes": {"claude": 1}, "errors": []}

        h = _harness(temp_home, identity=3, pair_of=1, record=1)
        h.engine = h._make_engine(adopt_snapshot=fake_snapshot)

        h.tick_with_usage(HEALTHY)
        h.clock.advance(60)
        h.tick_with_usage(HEALTHY)

        (report,) = _identity_only_events(h)
        (snap,) = [e for e in h.events if isinstance(e, AdoptSnapshotEvent)]
        assert h.events.index(snap) == h.events.index(report) + 1
        (call,) = calls
        assert call["session_dir"] == h.switcher._session_dir("3", EMAILS[3])
        payload = snap.to_json()
        assert payload["from"] == {"number": 1, "email": EMAILS[1]}
        assert payload["to"] == {"number": 3, "email": EMAILS[3]}
        assert payload["snapshot"] == {"processes": {"claude": 1}, "errors": []}

    def test_consistent_manual_login_is_still_adopted(self, temp_home):
        # Regression guard for CON-1581: a real /login (identity AND pair of
        # the same slot) is adopted as before, with no identity-only report.
        h = _harness(temp_home, identity=3, pair_of=3, record=1)
        h.tick_with_usage(HEALTHY)
        assert h.active_number() == 3
        assert len(_adopts(h)) == 1
        assert _identity_only_events(h) == []
