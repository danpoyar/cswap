"""The recorded active slot follows the real login (CON-1581).

``activeAccountNumber`` is written only by switch/add; a manual ``claude
/login`` onto another managed account leaves the record naming the OLD
slot, and every record consumer — the ``cswap list --json`` field, the
``refresh --all`` active-slot exclusion, rotation anchors, the
fresh-machine activation path — then acts on the wrong slot. The engine
now reconciles the record with the live identity (``~/.claude.json``'s
``oauthAccount``, the same identity ``claude auth status`` prints) on
every real tick and says so with one ``adopt-real-login`` event carrying
both numbers.
"""

from __future__ import annotations

from pathlib import Path

from claude_swap.autoswitch import (
    AdoptRealLoginEvent,
    AdoptSnapshotEvent,
    TickOutcome,
)
from tests.test_autoswitch import EngineHarness, _usage

EMAILS = {1: "a@example.com", 2: "b@example.com", 3: "c@example.com"}


def _harness(
    temp_home: Path, *, live: int, record: int, **kwargs
) -> EngineHarness:
    h = EngineHarness(temp_home, **kwargs)
    h.seed(1, EMAILS[1])
    h.seed(2, EMAILS[2])
    h.seed(3, EMAILS[3])
    h.make_live(EMAILS[live], live)
    data = h.switcher._get_sequence_data()
    assert data is not None
    data["activeAccountNumber"] = record
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _adopts(h: EngineHarness) -> list[AdoptRealLoginEvent]:
    return [e for e in h.events if isinstance(e, AdoptRealLoginEvent)]


HEALTHY = {"1": _usage(10), "2": _usage(10), "3": _usage(10)}


class TestAdoptRealLogin:
    def test_manual_login_is_adopted_into_the_record(self, temp_home):
        h = _harness(temp_home, live=3, record=1)
        outcome = h.tick_with_usage(HEALTHY)
        assert outcome is TickOutcome.NO_ACTION  # healthy, below threshold
        assert h.active_number() == 3
        (adopt,) = _adopts(h)
        assert adopt.prior == {"number": 1, "email": EMAILS[1]}
        assert adopt.to_ref == {"number": 3, "email": EMAILS[3]}
        payload = adopt.to_json()
        assert payload["event"] == "adopt-real-login"
        assert payload["from"] == {"number": 1, "email": EMAILS[1]}
        assert payload["to"] == {"number": 3, "email": EMAILS[3]}

    def test_adoption_happens_once_not_every_tick(self, temp_home):
        h = _harness(temp_home, live=3, record=1)
        h.tick_with_usage(HEALTHY)
        h.clock.advance(60)
        h.tick_with_usage(HEALTHY)
        assert len(_adopts(h)) == 1

    def test_matching_record_stays_silent(self, temp_home):
        h = _harness(temp_home, live=1, record=1)
        h.tick_with_usage(HEALTHY)
        assert _adopts(h) == []
        assert h.active_number() == 1

    def test_dry_run_does_not_write(self, temp_home):
        # Same law as the quarantine release: a dry-run tick must not
        # mutate anything.
        h = _harness(temp_home, live=3, record=1)
        h.engine = h._make_engine(dry_run=True)
        h.tick_with_usage(HEALTHY)
        assert h.active_number() == 1
        assert _adopts(h) == []


class TestAdoptGuards:
    def test_refused_when_the_live_identity_moved_under_the_lock(self, temp_home):
        # TOCTOU: a switch (or another /login) landed between the engine's
        # read and the lock — the record must not adopt a stale slot.
        h = _harness(temp_home, live=1, record=2)
        adopted, prior = h.switcher.adopt_active_account("3")
        assert adopted is False
        assert prior == "2"
        assert h.active_number() == 2

    def test_adopts_the_slot_the_live_identity_names(self, temp_home):
        h = _harness(temp_home, live=3, record=2)
        adopted, prior = h.switcher.adopt_active_account("3")
        assert adopted is True
        assert prior == "2"
        assert h.active_number() == 3


class TestAdoptSnapshot:
    """CON-2323: the adoption moment is captured, and never breaks the tick."""

    def test_snapshot_event_follows_the_adopt_event(self, temp_home):
        calls: list[dict] = []

        def fake_snapshot(**kwargs):
            calls.append(kwargs)
            return {"processes": {"claude": 2}, "errors": []}

        h = _harness(temp_home, live=3, record=1)
        h.engine = h._make_engine(adopt_snapshot=fake_snapshot)
        outcome = h.tick_with_usage(HEALTHY)
        assert outcome is TickOutcome.NO_ACTION
        (adopt,) = _adopts(h)
        snaps = [e for e in h.events if isinstance(e, AdoptSnapshotEvent)]
        assert len(snaps) == 1
        assert h.events.index(snaps[0]) == h.events.index(adopt) + 1
        (call,) = calls
        assert call["config_path"] == h.switcher._get_claude_config_path()
        assert call["session_dir"] == h.switcher._session_dir("3", EMAILS[3])
        payload = snaps[0].to_json()
        assert payload["event"] == "adopt-real-login-snapshot"
        assert payload["from"] == {"number": 1, "email": EMAILS[1]}
        assert payload["to"] == {"number": 3, "email": EMAILS[3]}
        assert payload["snapshot"] == {"processes": {"claude": 2}, "errors": []}
        assert "error" not in payload
        assert "2 claude" in snaps[0].human()

    def test_collector_failure_never_breaks_the_tick(self, temp_home):
        def broken(**kwargs):
            raise RuntimeError("boom")

        h = _harness(temp_home, live=3, record=1)
        h.engine = h._make_engine(adopt_snapshot=broken)
        outcome = h.tick_with_usage(HEALTHY)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 3
        assert len(_adopts(h)) == 1
        (snap,) = [e for e in h.events if isinstance(e, AdoptSnapshotEvent)]
        payload = snap.to_json()
        assert payload["snapshot"] is None
        assert "boom" in payload["error"]
        assert "boom" in snap.human()

    def test_no_snapshot_without_adoption(self, temp_home):
        h = _harness(temp_home, live=1, record=1)
        h.engine = h._make_engine(adopt_snapshot=lambda **kw: {"never": True})
        h.tick_with_usage(HEALTHY)
        assert [e for e in h.events if isinstance(e, AdoptSnapshotEvent)] == []
