"""Tests for the auto-switch engine (autoswitch.py)."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth, poll_policy
from claude_swap.autoswitch import (
    DRAIN2_RESUME_MESSAGE,
    DRAIN2_SELF_RESCUE_S,
    DRAIN2_STOP_MESSAGE,
    DRAIN_STALE_GAP_S,
    EPISODE_LATCH_NAME,
    EPISODE_LATCH_POINTER_NAME,
    IDLE_HOLD_MAX_S,
    NO_RESET_FALLBACK_S,
    QUIET_WINDOW_S,
    SETTINGS_WATCH_S,
    AllExhaustedEvent,
    AutoSwitchEngine,
    ConfigWarningEvent,
    Drain2ResumeEvent,
    Drain2SignalEvent,
    Drain2TimeoutEvent,
    Drain2UnavailableEvent,
    Drain2VerifyEvent,
    DrainTimeoutEvent,
    EarlySwapEvent,
    EpisodeNoticeEvent,
    ErrorEvent,
    LastAccountAlertEvent,
    NoSwitchEvent,
    PollEvent,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
    UnquarantineEvent,
    pct_label,
)
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.park import ParkSession, WaveResult
from claude_swap.usage_store import FetchRecord, UsageEntry
from claude_swap.models import Platform
from claude_swap.settings import (
    SETTING_SPECS,
    AutoSwitchSettings,
    load_settings,
    set_setting,
    settings_path,
    unset_setting,
)
from claude_swap.switcher import ClaudeAccountSwitcher


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _entry_for(value: dict | str | None, now: float) -> UsageEntry:
    """Synthesize the store entry a live fetch would have produced."""
    if isinstance(value, dict):
        return UsageEntry(last_good=value, fetched_at=now, age_s=0.0)
    if isinstance(value, str):
        return UsageEntry(sentinel=value)
    return UsageEntry()


class EngineHarness:
    """Seeded switcher + engine + captured events, on the Linux file backend."""

    def __init__(self, temp_home: Path, **settings_kwargs):
        self.temp_home = temp_home
        self.switcher = ClaudeAccountSwitcher()
        self.switcher.platform = Platform.LINUX
        self.switcher._setup_directories()
        self.switcher._init_sequence_file()
        self.settings = AutoSwitchSettings(**settings_kwargs)
        self.events: list = []
        self.clock = FakeClock()
        # Keep the usage store on the same fake clock as the engine so
        # freshness/claims/poll scheduling are deterministic in tests.
        self.switcher._usage_store.clock = self.clock
        self.engine = self._make_engine()

    def _make_engine(self, **kwargs) -> AutoSwitchEngine:
        return AutoSwitchEngine(
            self.switcher,
            self.settings,
            self.events.append,
            clock=self.clock,
            **kwargs,
        )

    def seed(self, num: int, email: str, *, expires_at: int | None = None) -> None:
        oauth_blob: dict = {
            "accessToken": f"sk-{num}",
            "refreshToken": f"rt-{num}",
        }
        if expires_at is not None:
            oauth_blob["expiresAt"] = expires_at
        self.switcher._write_account_credentials(
            str(num), email, json.dumps({"claudeAiOauth": oauth_blob})
        )
        self.switcher._write_account_config(
            str(num),
            email,
            json.dumps({
                "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
            }),
        )
        data = self.switcher._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        self.switcher._write_json(self.switcher.sequence_file, data)

    def make_live(self, email: str, num: int) -> None:
        (self.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "sk-live", "refreshToken": "rt-live"},
        }))
        (self.temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{num}"},
        }))

    def tick_with_usage(self, usage: dict) -> TickOutcome:
        entries = {
            num: _entry_for(value, self.clock.now) for num, value in usage.items()
        }
        return self.tick_with_entries(entries)

    def tick_with_entries(self, entries: dict[str, UsageEntry]) -> TickOutcome:
        with patch.object(
            self.switcher, "usage_entries_by_account", return_value=entries
        ):
            return self.engine.tick()

    def active_number(self) -> int | None:
        return self.switcher._get_sequence_data()["activeAccountNumber"]

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def state(self) -> dict:
        path = self.switcher.backup_dir / "autoswitch_state.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())


@pytest.fixture
def harness(temp_home: Path) -> EngineHarness:
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestDecisionTable:
    def test_below_threshold_is_no_action(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_over_threshold_switches_to_max_headroom(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(40), "3": _usage(20),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.to_ref == {"number": 3, "email": "c@example.com"}
        assert harness.state()["lastSwitchTo"] == "3"

    def test_state_records_trigger_and_gate(self, harness):
        # The state file must carry WHY the daemon switched, so the Quota
        # panel can explain the last switch to a human. Additive fields:
        # schemaVersion stays put, consumers ignore unknown keys.
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(40), "3": _usage(20),
        })
        assert outcome is TickOutcome.SWITCHED
        state = harness.state()
        assert state["lastSwitchTrigger"] == "proactive"
        # A proactive switch only proceeds through the quiet gate.
        assert state["lastSwitchGate"] == "quiet"

    def test_no_active_account(self, temp_home):
        h = EngineHarness(temp_home)
        assert h.engine.tick() is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "no-active-account"
        ]

    def test_hysteresis_margin_blocks_marginal_candidates(self, harness):
        # threshold 90, hysteresis 10 → a candidate must beat the active
        # account's utilization by >= 10 points; 95→86 is only 9 better.
        # Failing the margin is NOT exhaustion: no all-exhausted event, no
        # reset-sleep — the next tick must stay at normal cadence so the
        # at-limit escape isn't missed when the active account tops out.
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(86), "3": _usage(88),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]
        assert harness.engine._sleep_until_ts is None
        delay = harness.engine._next_delay(outcome)
        assert delay <= 1.1 * harness.settings.interval_seconds

    def test_issue_115_strictly_better_candidate_switches(self, harness):
        # Regression for #115: active bound by 5h (99%), candidate bound by
        # 7d (89%). The old absolute bar (<= 80% used) vetoed the candidate;
        # the relative gate takes it: 89 < 90 and 99 - 89 >= 10.
        outcome = harness.tick_with_usage({
            "1": {"five_hour": {"pct": 99.0}, "seven_day": {"pct": 24.0}},
            "2": {"five_hour": {"pct": 3.0}, "seven_day": {"pct": 89.0}},
            "3": {"five_hour": {"pct": 95.0}, "seven_day": {"pct": 10.0}},
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert harness.active_number() == 2

    def test_proactive_never_lands_at_or_over_threshold(self, temp_home):
        # threshold 80, hysteresis 5: the candidate at 85% is five points
        # better than the active 90%, but it already sits over the threshold
        # and would re-trigger on the very next tick — blocked.
        h = EngineHarness(temp_home, threshold=80.0, hysteresis_pct=5.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({"1": _usage(90), "2": _usage(85)})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]

    def test_stable_landing_does_not_switch_back(self, temp_home):
        # Cooldown disabled so only the gate itself prevents flapping: after
        # 99→89 the roles reverse, and the old account (99%) can never beat
        # the new active (89%) — the move is one-way.
        h = EngineHarness(temp_home, cooldown_seconds=0.0)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        usage = {
            "1": {"five_hour": {"pct": 99.0}, "seven_day": {"pct": 24.0}},
            "2": {"five_hour": {"pct": 3.0}, "seven_day": {"pct": 89.0}},
        }
        assert h.tick_with_usage(usage) is TickOutcome.SWITCHED
        assert h.active_number() == 2
        h.clock.advance(60)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_mixed_unknown_and_exhausted_is_not_all_exhausted(self, harness):
        # One candidate at its limit, the other unreadable this tick: usage
        # could recover any moment, so no long reset-sleep.
        outcome = harness.tick_with_usage({
            "1": _usage(95),
            "2": _usage(100, "2026-07-03T12:00:00Z"),
            "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]
        assert harness.engine._sleep_until_ts is None
        delay = harness.engine._next_delay(outcome)
        assert delay <= 1.1 * harness.settings.interval_seconds

    def test_stale_beyond_trust_blocks_all_exhausted(self, harness):
        # One candidate exhausted on trusted-stale data, the other's data aged
        # past every trust window (no failures, no plan — just overdue): the
        # unknown candidate could be viable, so no long reset-sleep.
        now = harness.clock.now
        reset = "2026-07-05T12:00:00Z"
        outcome = harness.tick_with_entries({
            "1": UsageEntry(last_good=_usage(95), fetched_at=now, age_s=0.0),
            "2": UsageEntry(
                last_good=_usage(100, reset), fetched_at=now - 400, age_s=400.0,
                consecutive_failures=1, trust_extended=True,
            ),
            "3": UsageEntry(last_good=_usage(10), fetched_at=now - 400, age_s=400.0),
        })
        assert outcome is TickOutcome.BLOCKED
        assert not any(isinstance(e, AllExhaustedEvent) for e in harness.events)
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-qualifying-candidate"]

    def test_trusted_stale_exhausted_set_still_fires_all_exhausted(self, harness):
        # Every candidate at its limit, known only through trusted-stale data
        # (in failure state) — that is still "known and exhausted".
        now = harness.clock.now
        reset = "2026-07-05T12:00:00Z"
        stale_exhausted = UsageEntry(
            last_good=_usage(100, reset), fetched_at=now - 400, age_s=400.0,
            consecutive_failures=1, trust_extended=True,
        )
        outcome = harness.tick_with_entries({
            "1": UsageEntry(last_good=_usage(95), fetched_at=now, age_s=0.0),
            "2": stale_exhausted,
            "3": stale_exhausted,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(
            e for e in harness.events if isinstance(e, AllExhaustedEvent)
        )
        assert exhausted.earliest_reset_at == reset

    # test_cooldown_suppresses_proactive was retired after the 2026-07-31
    # incident: cooldown no longer holds proactive (see
    # test_proactive_escapes_cooldown below). Its real intent — cooldown
    # debounces VOLUNTARY switches — lives on through the consume-first
    # trigger in TestConsumeFirstStrategy::test_respects_cooldown.

    def test_at_limit_bypasses_cooldown(self, harness):
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock() - 10)
        )
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(10), "3": _usage(50),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert harness.active_number() == 2

    def test_proactive_escapes_cooldown(self, harness):
        # Incident 2026-07-31 (~11:30 UTC, cswap-auto.log): the active slot
        # sat at 98% with threshold 95 for eight minutes emitting "no-switch
        # cooldown" until it hit 100% and took the forced at-limit exit.
        # Over the threshold a switch is no longer a voluntary optimization —
        # cooldown must only debounce the voluntary consume-first rotation.
        harness.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=harness.clock() - 10)
        )
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"

    # test_cooldown_expires moved to
    # TestConsumeFirstStrategy::test_cooldown_expires: since proactive
    # escapes cooldown (2026-07-31 incident), expiry is only observable on
    # the consume-first trigger — the proactive variant passed vacuously.

    def test_unknown_active_usage_waits_then_fails_over(self, harness):
        usage = {"1": None, "2": _usage(10), "3": _usage(50)}
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.active_number() == 2

    def test_known_active_usage_resets_unhealthy_counter(self, harness):
        unknown = {"1": None, "2": _usage(10), "3": _usage(10)}
        healthy = {"1": _usage(50), "2": _usage(10), "3": _usage(10)}
        harness.tick_with_usage(unknown)
        harness.tick_with_usage(unknown)
        harness.tick_with_usage(healthy)  # resets the counter
        assert harness.tick_with_usage(unknown) is TickOutcome.NO_ACTION
        assert harness.active_number() == 1

    def test_all_candidates_unknown_is_no_comparison(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": None, "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        assert [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)] == [
            "no-comparison"
        ]

    def test_tie_resolves_to_earliest_slot(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(30), "3": _usage(30),
        })
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2

    def test_candidate_not_better_than_active_is_skipped(self, harness):
        # Active 91% used (9 headroom); candidates worse or equal → exhausted.
        outcome = harness.tick_with_usage({
            "1": _usage(91), "2": _usage(95), "3": _usage(99),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_at_limit_escapes_hysteresis_bar(self, harness):
        # Active hard at 100%; the only room anywhere is a candidate at 85%,
        # which the proactive hysteresis bar (<=80%) would reject. At-limit is
        # an escape: any account with real headroom beats a blocked one.
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(85), "3": _usage(97),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert harness.active_number() == 2

    def test_at_limit_never_targets_another_at_limit_account(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(100), "3": _usage(100),
        })
        assert outcome is TickOutcome.BLOCKED
        assert harness.active_number() == 1

    def test_failover_ignores_hysteresis_bar(self, harness):
        # Active usage unreadable (auth likely dead); the only candidate with
        # room sits above the hysteresis bar — failover takes it anyway.
        usage = {"1": None, "2": _usage(85), "3": _usage(100)}
        harness.tick_with_usage(usage)
        harness.tick_with_usage(usage)
        outcome = harness.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.active_number() == 2

    def test_unmanaged_live_login_is_never_touched(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        # The user logged in with an account cswap doesn't manage.
        h.make_live("stranger@example.com", 9)
        live_before = (temp_home / ".claude" / ".credentials.json").read_text()
        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["unmanaged-active-account"]
        assert (temp_home / ".claude" / ".credentials.json").read_text() == live_before

    def test_all_exhausted_carries_earliest_reset(self, harness):
        outcome = harness.tick_with_usage({
            "1": _usage(100, "2026-07-03T12:00:00Z"),
            "2": _usage(100, "2026-07-03T10:30:00Z"),
            "3": _usage(100, "2026-07-03T11:00:00Z"),
        })
        assert outcome is TickOutcome.BLOCKED
        event = next(e for e in harness.events if isinstance(e, AllExhaustedEvent))
        assert event.earliest_reset_at == "2026-07-03T10:30:00Z"
        assert harness.engine._sleep_until_ts is not None

    @pytest.mark.parametrize("offset", [-60.0, 0.0])
    def test_all_exhausted_ignores_non_future_reset(self, harness, offset):
        from datetime import datetime, timezone

        reset = (
            datetime.fromtimestamp(harness.clock.now + offset, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        outcome = harness.tick_with_usage({
            "1": _usage(100, reset),
            "2": _usage(100, reset),
            "3": _usage(100, reset),
        })
        assert outcome is TickOutcome.BLOCKED
        event = next(e for e in harness.events if isinstance(e, AllExhaustedEvent))
        assert event.earliest_reset_at is None
        assert harness.engine._sleep_until_ts is None
        assert harness.engine._next_delay(outcome) == NO_RESET_FALLBACK_S


# Real transcript-record shapes, as Claude Code writes them (first lines of a
# live ~/.claude/projects/<munged-cwd>/<session-uuid>.jsonl).
_TRANSCRIPT_LINES = (
    '{"type":"last-prompt","leafUuid":"0fdf8735-e4dd-4b5b-af96-090f5557e015",'
    '"sessionId":"755b81b7-819f-4039-a59d-965564f875d2"}\n'
    '{"type":"mode","mode":"normal",'
    '"sessionId":"755b81b7-819f-4039-a59d-965564f875d2"}\n'
)

# Both live layouts: a main session transcript, and a workflow subagent's
# transcript nested three directories deeper.
_MAIN_SESSION_REL = "-Users-philosopher/755b81b7-819f-4039-a59d-965564f875d2.jsonl"
_SUBAGENT_REL = (
    "-Users-philosopher/0c51f7df-8906-4d82-bc46-a85d3b7154bd/subagents/"
    "workflows/wf_61fb8766-23a/agent-ad4d85f249c88950c.jsonl"
)


def _write_transcript(
    harness: EngineHarness, age_s: float, rel: str = _MAIN_SESSION_REL
) -> Path:
    """Write a real-format session transcript whose mtime is ``age_s`` before
    the harness clock's now."""
    path = harness.temp_home / ".claude" / "projects" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TRANSCRIPT_LINES)
    ts = harness.clock.now - age_s
    os.utime(path, (ts, ts))
    return path


class TestQuietGate:
    """Voluntary switches wait for session-traffic silence.

    A proactive swap invalidates the prompt caches of every live session on
    the account being left (orgs don't share caches), so it is only allowed
    once the newest ``~/.claude/projects/**/*.jsonl`` mtime is at least
    QUIET_WINDOW_S old. At-limit/failover are escapes and ignore the gate.
    """

    _PROACTIVE = {"1": _usage(96), "2": _usage(40), "3": _usage(20)}

    def test_fresh_transcript_blocks_proactive(self, harness):
        _write_transcript(harness, age_s=10.0)
        outcome = harness.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1
        assert [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)] == [
            "sessions-active"
        ]
        assert "lastSwitchAt" not in harness.state()

    def test_stale_transcript_allows_proactive(self, harness):
        _write_transcript(harness, age_s=360.0)  # 6 min > 5 min window
        outcome = harness.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.gate == "quiet"
        assert switch.to_json()["gate"] == "quiet"

    def test_fresh_subagent_transcript_blocks_proactive(self, harness):
        # Subagent transcripts live 3 levels deeper; the scan must recurse.
        _write_transcript(harness, age_s=10.0, rel=_SUBAGENT_REL)
        outcome = harness.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)] == [
            "sessions-active"
        ]

    def test_newest_transcript_wins(self, harness):
        # An old main session plus a busy subagent: the newest mtime gates.
        _write_transcript(harness, age_s=3600.0)
        _write_transcript(harness, age_s=10.0, rel=_SUBAGENT_REL)
        outcome = harness.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION

    def test_no_transcripts_is_quiet(self, harness):
        # Nothing under projects/ → nothing to burn → proactive allowed.
        outcome = harness.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.gate == "quiet"

    def test_at_limit_ignores_quiet_gate(self, harness):
        _write_transcript(harness, age_s=10.0)
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(40), "3": _usage(20),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert switch.gate == "forced"
        assert switch.to_json()["gate"] == "forced"

    def test_at_limit_during_quiet_is_labeled_quiet(self, harness):
        # The gate field records the traffic state at swap time, so forced
        # triggers during real silence are measurably harmless.
        _write_transcript(harness, age_s=360.0)
        outcome = harness.tick_with_usage({
            "1": _usage(100), "2": _usage(40), "3": _usage(20),
        })
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert switch.gate == "quiet"

    def test_failover_ignores_quiet_gate(self, harness):
        _write_transcript(harness, age_s=10.0)
        usage = {"1": None, "2": _usage(10), "3": _usage(50)}
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(usage) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert switch.gate == "forced"

    def _loaded_harness(self, temp_home: Path, **kwargs) -> EngineHarness:
        h = EngineHarness(temp_home, switch_under_load=True, **kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_switch_under_load_lets_proactive_through(self, temp_home):
        """Unattended fleets (mining waves, overnight agents) never go quiet:
        the gate would hold the proactive swap until the account hit the wall
        and only an at-limit escape got out — after in-flight agents already
        died on the limit. With switchUnderLoad the at-threshold swap lands
        under traffic, paying prompt-cache misses instead of failed agents."""
        h = self._loaded_harness(temp_home)
        _write_transcript(h, age_s=10.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.gate == "forced"
        assert switch.to_json()["gate"] == "forced"

    def test_switch_under_load_still_holds_consume_first(self, temp_home):
        """The escape hatch is only for "time to leave". The below-threshold
        consume-first rotation is pure optimization and keeps waiting for
        silence, so it never burns a live session's cache for nothing."""
        h = self._loaded_harness(temp_home, strategy="consume-first")
        _write_transcript(h, age_s=10.0)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert "sessions-active" in [
            e.reason for e in h.events if isinstance(e, NoSwitchEvent)
        ]

    def test_switch_under_load_defaults_off(self):
        assert AutoSwitchSettings().switch_under_load is False
        assert SETTING_SPECS["autoswitch.switchUnderLoad"].field == "switch_under_load"
        assert SETTING_SPECS["autoswitch.switchUnderLoad"].kind == "bool"

    def test_perform_rechecks_quiet_under_lock(self, harness):
        """Traffic appearing between the tick-top gate and the actual switch
        (token freshening can take seconds) must still block the swap."""
        real_freshen = harness.engine._freshen_target

        def freshen_then_traffic(number, email):
            status = real_freshen(number, email)
            _write_transcript(harness, age_s=10.0)
            return status

        with patch.object(
            harness.engine, "_freshen_target", side_effect=freshen_then_traffic
        ):
            outcome = harness.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        assert harness.active_number() == 1
        assert "sessions-active" in [
            e.reason for e in harness.events if isinstance(e, NoSwitchEvent)
        ]


class TestDrainGate:
    """Forced switches drain: bounded wait for session silence (CON-419).

    A forced swap (failover, and proactive under switchUnderLoad) under live
    traffic burns the prompt cache of every running session on the account
    being left. With ``autoswitch.drainTimeoutSeconds`` set, the engine holds
    the forced swap — re-checking every tick — until transcripts have been
    silent for QUIET_WINDOW_S, and at the ceiling swaps anyway with a
    warning: an account pinned at its limit breaks live agents harder than a
    cache miss does. 0 (the default) keeps forced switches immediate.

    At-limit is the exception (CON-486): it means the binding window is at
    100%, so the wait would protect a cache that is already dying — the swap
    lands immediately. The wait-machinery tests below therefore drive the
    drain via proactive-under-switchUnderLoad (and failover), and the
    at-limit tests assert the skip.
    """

    _AT_LIMIT = {"1": _usage(100), "2": _usage(40), "3": _usage(20)}
    _PROACTIVE = {"1": _usage(96), "2": _usage(40), "3": _usage(20)}

    def _drain_harness(self, temp_home: Path, **kwargs) -> EngineHarness:
        h = EngineHarness(temp_home, drain_timeout_seconds=600.0, **kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def _reasons(self, h: EngineHarness) -> list[str]:
        return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def test_default_off_swaps_immediately(self, harness):
        # Upstream behavior is unchanged until the ceiling is configured.
        assert AutoSwitchSettings().drain_timeout_seconds == 0.0
        _write_transcript(harness, age_s=10.0)
        outcome = harness.tick_with_usage(self._AT_LIMIT)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.gate == "forced"
        assert "drain" not in switch.to_json()
        assert "drain-wait" not in self._reasons(harness)

    def test_at_limit_dead_window_skips_drain(self, temp_home):
        # CON-486: at-limit means the binding window is at 100% — every call
        # on the account being left is already failing, so transcript silence
        # measures how long the dying takes, not a cache worth protecting
        # (live episode 2026-08-14: 417s of drain-wait on a dead account).
        h = self._drain_harness(temp_home)
        _write_transcript(h, age_s=10.0)  # park is writing right now
        outcome = h.tick_with_usage(self._AT_LIMIT)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert switch.gate == "forced"
        assert "drain" not in switch.to_json()
        assert "drain-wait" not in self._reasons(h)
        assert "drain" not in h.state()

    def test_below_100_forced_switch_still_drains(self, temp_home):
        # The other half of the CON-486 contract: a forced switch off an
        # account whose window is NOT dead (95%) keeps the bounded wait.
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        usage = {"1": _usage(95), "2": _usage(40), "3": _usage(20)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert self._reasons(h) == ["drain-wait"]
        assert h.state()["drain"]["startedAt"] == h.clock.now

    def test_busy_forced_switch_waits(self, temp_home):
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert self._reasons(h) == ["drain-wait"]
        assert h.state()["drain"]["startedAt"] == h.clock.now
        assert "lastSwitchAt" not in h.state()

    def test_wait_progresses_without_restarting(self, temp_home):
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        started = h.state()["drain"]["startedAt"]
        h.clock.advance(60.0)
        _write_transcript(h, age_s=10.0)  # traffic continues
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["drain-wait", "drain-wait"]
        assert h.state()["drain"]["startedAt"] == started
        assert h.state()["drain"]["updatedAt"] == h.clock.now

    def test_quiet_after_wait_switches_with_drain_go(self, temp_home):
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        h.clock.advance(360.0)  # transcript is now 370s old -> quiet
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.gate == "quiet"
        assert switch.to_json()["drain"] == {"outcome": "go", "waitedSeconds": 360}
        assert "drained 360s" in switch.human()
        assert "drain" not in h.state()
        assert not [e for e in h.events if isinstance(e, DrainTimeoutEvent)]

    def test_ceiling_swaps_with_warn(self, temp_home):
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        h.clock.advance(610.0)
        _write_transcript(h, age_s=10.0)  # still busy at the ceiling
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        warn = next(e for e in h.events if isinstance(e, DrainTimeoutEvent))
        assert warn.human().startswith("WARN")
        payload = warn.to_json()
        assert payload["event"] == "drain-timeout"
        assert payload["trigger"] == "proactive"
        assert payload["waitedSeconds"] == 610
        assert payload["maxWaitSeconds"] == 600
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.gate == "forced"
        assert switch.to_json()["drain"] == {
            "outcome": "timeout",
            "waitedSeconds": 610,
        }
        # The WARN precedes the switch in the log.
        kinds = h.kinds()
        assert kinds.index("drain-timeout") < kinds.index("switch")
        assert "drain" not in h.state()  # a landed switch closes the episode

    def test_immediate_quiet_has_no_drain_field(self, temp_home):
        h = self._drain_harness(temp_home)
        _write_transcript(h, age_s=360.0)
        outcome = h.tick_with_usage(self._AT_LIMIT)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.gate == "quiet"
        assert "drain" not in switch.to_json()
        assert "drain-wait" not in self._reasons(h)
        assert "drain" not in h.state()

    def test_failover_drains_too(self, temp_home):
        h = self._drain_harness(temp_home)
        _write_transcript(h, age_s=10.0)
        usage = {"1": None, "2": _usage(10), "3": _usage(50)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION  # 1/3 unhealthy
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION  # 2/3 unhealthy
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION  # failover drains
        assert "drain-wait" in self._reasons(h)
        h.clock.advance(360.0)
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert switch.to_json()["drain"]["outcome"] == "go"

    def test_proactive_under_load_drains(self, temp_home):
        # switchUnderLoad released proactive from the unbounded gate; with a
        # drain ceiling it waits for a pause first instead of landing at the
        # first busy tick.
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["drain-wait"]
        h.clock.advance(360.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.gate == "quiet"
        assert switch.to_json()["drain"]["outcome"] == "go"

    def test_voluntary_gate_stays_unbounded(self, temp_home):
        # The drain ceiling is only for forced switches: the voluntary quiet
        # gate keeps holding proactive (without switchUnderLoad) forever.
        h = self._drain_harness(temp_home)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        h.clock.advance(700.0)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["sessions-active", "sessions-active"]
        assert "drain" not in h.state()

    def test_stale_drain_record_restarts_the_episode(self, temp_home):
        # A leftover record from a previous episode (engine slept, condition
        # went away and came back) must not count as already-waited time.
        h = self._drain_harness(temp_home, switch_under_load=True)
        state_file = h.switcher.backup_dir / "autoswitch_state.json"
        state_file.write_text(json.dumps({
            "schemaVersion": 1,
            "drain": {
                "startedAt": h.clock.now - 5000.0,
                "updatedAt": h.clock.now - DRAIN_STALE_GAP_S - 1.0,
                "trigger": "proactive",
            },
        }))
        _write_transcript(h, age_s=10.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["drain-wait"]
        assert h.state()["drain"]["startedAt"] == h.clock.now

    def test_timeout_warns_once_across_blocked_attempts(self, temp_home):
        # Ceiling reached but the swap keeps failing PAST the gate (every
        # ranked target refuses to freshen): the engine keeps trying every
        # tick without re-waiting a full ceiling and without repeating the
        # WARN. (Since CON-572 a tick with no candidate at all never
        # starts or keeps a drain — the episode's wait belongs to a switch
        # that can actually happen — so the blocked attempts here are
        # post-gate failures.)
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        h.clock.advance(610.0)
        _write_transcript(h, age_s=10.0)
        with patch.object(
            h.engine, "_freshen_target", return_value="skip-live-session"
        ):
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.BLOCKED
            h.clock.advance(60.0)
            _write_transcript(h, age_s=10.0)
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.BLOCKED
        warns = [e for e in h.events if isinstance(e, DrainTimeoutEvent)]
        assert len(warns) == 1
        assert self._reasons(h) == [  # no re-wait after the timeout
            "drain-wait",
            "no-viable-target",
            "no-viable-target",
        ]
        assert h.state()["drain"]["timeoutWarned"] is True

    def test_failed_attempt_keeps_the_episode(self, temp_home):
        # Silence arrived but the swap failed past the gate (freshen
        # hiccup): the episode must survive, so the eventual switch still
        # carries its drain label instead of restarting from zero.
        h = self._drain_harness(temp_home, switch_under_load=True)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        h.clock.advance(360.0)  # quiet now
        with patch.object(
            h.engine, "_freshen_target", return_value="transient"
        ):
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.ERROR
        assert "drain" in h.state()  # episode survived the failed attempt
        h.clock.advance(30.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain"] == {
            "outcome": "go",
            "waitedSeconds": 390,
        }
        assert "drain" not in h.state()

    def test_dry_run_drain_writes_no_state(self, temp_home):
        h = self._drain_harness(temp_home, switch_under_load=True)
        h.engine = h._make_engine(dry_run=True)
        _write_transcript(h, age_s=10.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["drain-wait"]
        assert "drain" not in h.state()  # dry-run keeps the episode in memory
        h.clock.advance(610.0)
        _write_transcript(h, age_s=10.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.dry_run is True
        assert switch.to_json()["drain"]["outcome"] == "timeout"
        assert h.state() == {}  # nothing was ever written
        assert h.active_number() == 1

    def test_drain_setting_spec(self):
        spec = SETTING_SPECS["autoswitch.drainTimeoutSeconds"]
        assert spec.field == "drain_timeout_seconds"
        assert spec.kind == "float"
        assert spec.lo == 0.0
        assert spec.hi == 86400.0
        assert AutoSwitchSettings().drain_timeout_seconds == 0.0


class TestVoluntaryMinimumInterval:
    """cooldown_seconds is the minimum spacing between VOLUNTARY switches;
    the fleet runs it at 2 hours (settings.json), so prove the mechanism
    holds at that value. Since the 2026-07-31 incident (98% held for eight
    minutes by "no-switch cooldown" until the wall) the only voluntary
    trigger is consume-first below the threshold — proactive means the
    threshold is crossed and escapes, like at-limit always did. The blocked
    case therefore lives on the consume-first trigger."""

    _TWO_HOURS = 7200.0

    def _harness(self, temp_home: Path, **kwargs) -> EngineHarness:
        h = EngineHarness(temp_home, cooldown_seconds=self._TWO_HOURS, **kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_second_voluntary_within_two_hours_is_blocked(self, temp_home):
        h = self._harness(temp_home, strategy="consume-first")
        assert h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 2

        h.clock.advance(3600.0)  # 1h later — inside the 2h floor
        h.events.clear()
        outcome = h.tick_with_usage({
            "2": _usage7(20, 20, _R_LATER),
            "1": _usage7(10, 10, _R_LATEST),
            "3": _usage7(10, 10, _R_SOON),   # sooner target appears
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "cooldown"
        ]

        h.clock.advance(3601.0)  # 2h+1s after the first switch
        h.events.clear()
        assert h.tick_with_usage({
            "2": _usage7(20, 20, _R_LATER),
            "1": _usage7(10, 10, _R_LATEST),
            "3": _usage7(10, 10, _R_SOON),
        }) is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_proactive_bypasses_two_hour_interval(self, temp_home):
        # The 2026-07-31 incident scenario at fleet settings: a second slot
        # crosses the threshold inside the 2h window and must still leave.
        h = self._harness(temp_home)
        assert h.tick_with_usage(
            {"1": _usage(96), "2": _usage(40), "3": _usage(20)}
        ) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        h.clock.advance(3600.0)  # 1h later — inside the 2h floor
        h.events.clear()
        outcome = h.tick_with_usage(
            {"3": _usage(96), "1": _usage(50), "2": _usage(40)}
        )
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"

    def test_at_limit_bypasses_two_hour_interval(self, temp_home):
        h = self._harness(temp_home)
        assert h.tick_with_usage(
            {"1": _usage(96), "2": _usage(40), "3": _usage(20)}
        ) is TickOutcome.SWITCHED
        h.clock.advance(60.0)
        h.events.clear()
        outcome = h.tick_with_usage(
            {"3": _usage(100), "1": _usage(50), "2": _usage(40)}
        )
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"


class TestIdleHold:
    """Active token expired while Claude Code owns it → hold, don't fail over."""

    _HELD = {"1": USAGE_TOKEN_EXPIRED, "2": _usage(10), "3": _usage(20)}

    def test_token_expired_holds_instead_of_failover(self, harness):
        for _ in range(6):  # far past unhealthy_ticks (3)
            assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
            harness.clock.advance(60)
        assert harness.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in harness.events)
        reasons = {e.reason for e in harness.events if isinstance(e, NoSwitchEvent)}
        assert reasons == {"active-idle"}
        assert harness.engine._unhealthy_ticks == 0

    def test_idle_hold_slows_cadence(self, harness):
        outcome = harness.tick_with_usage(self._HELD)
        assert outcome is TickOutcome.NO_ACTION
        assert harness.engine._next_delay(outcome) >= NO_RESET_FALLBACK_S

    def test_idle_hold_cap_escalates_to_failover(self, harness):
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        harness.clock.advance(IDLE_HOLD_MAX_S + 1)
        # Past the cap the sentinel counts as unhealthy again → failover after
        # unhealthy_ticks (3) consecutive ticks.
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.tick_with_usage(self._HELD) is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"

    def test_recovery_resets_the_hold_clock(self, harness):
        healthy = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        harness.tick_with_usage(self._HELD)
        harness.clock.advance(IDLE_HOLD_MAX_S - 60)
        harness.tick_with_usage(healthy)  # user came back; token refreshed
        harness.clock.advance(120)
        # New expiry long after: the hold clock restarted, so still held.
        assert harness.tick_with_usage(self._HELD) is TickOutcome.NO_ACTION
        assert harness.engine._unhealthy_ticks == 0
        assert harness.active_number() == 1

    def test_plain_fetch_failure_still_counts_unhealthy(self, harness):
        # A None (network failure / dead creds) is NOT the idle sentinel:
        # unhealthy counting and the hold clock reset both apply.
        harness.tick_with_usage(self._HELD)
        unknown = {"1": None, "2": _usage(10), "3": _usage(20)}
        assert harness.tick_with_usage(unknown) is TickOutcome.NO_ACTION
        assert harness.engine._unhealthy_ticks == 1
        assert harness.engine._idle_hold_since is None


class TestAdaptiveScheduler:
    """End-to-end through the real store: O(1) baseline, escalations,
    skip-to-reset, movement-based cadence."""

    def _harness(self, temp_home, monkeypatch, accounts=3, **settings_kwargs):
        monkeypatch.setattr("claude_swap.switcher._FETCH_STAGGER_S", 0)
        h = EngineHarness(temp_home, **settings_kwargs)
        emails = ["a@example.com", "b@example.com", "c@example.com"]
        for num in range(1, accounts + 1):
            h.seed(num, emails[num - 1])
        h.make_live("a@example.com", 1)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        return h

    @staticmethod
    def _counting_fetch(counts, usage_by_num, errors_by_num=None):
        def fake(num, email, creds, is_active=False, persist_credentials=None):
            counts[num] = counts.get(num, 0) + 1
            error = (errors_by_num or {}).get(num)
            if error:
                return oauth.UsageOutcome(None, error=error)
            value = usage_by_num.get(num)
            return oauth.UsageOutcome(dict(value) if value else None)
        return fake

    def _tick(self, h, counts, usage_by_num, errors_by_num=None):
        with patch(
            "claude_swap.oauth.try_fetch_usage_for_account",
            side_effect=self._counting_fetch(counts, usage_by_num, errors_by_num),
        ):
            return h.engine.tick()

    def test_baseline_fetches_active_plus_one_candidate(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch)
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        # t0: active (never fetched) + the stalest candidate.
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1}
        # t60: active planned MIN_INTERVAL_S out; the never-fetched candidate
        # is the due one.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1, "3": 1}
        # t120: nobody due — everyone served from the store.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 1, "2": 1, "3": 1}
        # t180: the active account's plan comes due.
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts == {"1": 2, "2": 1, "3": 1}

    def test_near_threshold_escalates_to_full_refresh(self, temp_home, monkeypatch):
        # threshold 90, margin 15 → active at 80% is within the escalation band.
        h = self._harness(temp_home, monkeypatch)
        counts: dict[str, int] = {}
        outcome = self._tick(
            h, counts, {"1": _usage(80), "2": _usage(10), "3": _usage(20)}
        )
        assert outcome is TickOutcome.NO_ACTION  # still below the threshold
        assert counts == {"1": 1, "2": 1, "3": 1}  # but everyone got refreshed

    def test_active_unknown_escalates_before_failover(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, unhealthy_ticks=1)
        counts: dict[str, int] = {}
        outcome = self._tick(
            h, counts,
            {"2": _usage(10), "3": _usage(50)},
            errors_by_num={"1": "timeout"},
        )
        # Candidate data was refreshed in the same tick the failover ran on.
        assert counts == {"1": 1, "2": 1, "3": 1}
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_cadence_floor_and_decay(self, temp_home, monkeypatch):
        # The active account polls at MIN_INTERVAL_S first; unmoved usage
        # decays the interval ×1.5 toward ACTIVE_MAX_INTERVAL_S.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(10), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)  # never-fetched → fetched
        assert counts["1"] == 1
        for _ in range(2):  # ages 60s and 120s — inside the 180s floor
            h.clock.advance(60)
            self._tick(h, counts, usage)
        assert counts["1"] == 1
        h.clock.advance(60)  # age 180s → due again
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        # Unmoved → interval decayed to 270s: not due at +240, due at +300.
        h.clock.advance(240)
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts["1"] == 3

    def test_urgent_cadence_when_burning_near_the_band(self, temp_home, monkeypatch):
        # Active moving inside the escalation band → 60s urgent cadence, so
        # a threshold crossing is seen within a minute of the previous poll.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(70), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(80)  # burning: +10 pts, now inside the band
        h.clock.advance(180)
        self._tick(h, counts, usage)  # movement + in band → urgent plan
        assert counts["1"] == 2
        usage["1"] = _usage(84)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # urgent plan due after only 60s
        assert counts["1"] == 3

    def test_in_band_without_movement_keeps_the_floor(self, temp_home, monkeypatch):
        # In the escalation band but not burning: no urgency — the normal
        # 180s floor applies (escalation keeps candidates fresh; it must not
        # re-fetch a fresh, unmoving active every tick).
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(80), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        for _ in range(2):
            h.clock.advance(60)
            self._tick(h, counts, usage)
        assert counts["1"] == 1  # not due inside the floor
        h.clock.advance(60)
        self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_urgent_band_follows_the_threshold(self, temp_home, monkeypatch):
        # The urgent band is distance-to-threshold, not absolute pct: with
        # threshold 50 (band edge 35), movement at 40% engages the urgent
        # cadence that the default threshold would ignore.
        h = self._harness(temp_home, monkeypatch, accounts=2, threshold=50)
        usage = {"1": _usage(30), "2": _usage(10)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(40)
        h.clock.advance(180)
        self._tick(h, counts, usage)  # movement inside the 35..50 band
        assert counts["1"] == 2
        usage["1"] = _usage(44)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # urgent plan due after only 60s
        assert counts["1"] == 3

    def test_stale_candidate_plan_never_gates_the_active(
        self, temp_home, monkeypatch
    ):
        # Role change outside a cswap switch (e.g. manual login): the active
        # slot can carry a plan written while it was an idle candidate, up to
        # 600s out. The ACTIVE_MAX_INTERVAL_S age cap overrides it.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(50), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.switcher._usage_store.set_poll_plan(
            {"1": (h.clock.now + 600.0, 600.0)}, {"1": ("a@example.com", "")}
        )
        h.clock.advance(240)  # inside the bogus plan, under the age cap
        self._tick(h, counts, usage)
        assert counts["1"] == 1
        h.clock.advance(120)  # age 360 ≥ ACTIVE_MAX_INTERVAL_S
        self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_exhausted_active_is_rechecked_before_its_reset(
        self, temp_home, monkeypatch
    ):
        from datetime import datetime, timezone

        h = self._harness(temp_home, monkeypatch, accounts=1)
        reset_ts = h.clock.now + 7200.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(100, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        assert counts["1"] == 1
        for _ in range(3):
            h.clock.advance(400)
            self._tick(h, counts, usage)
        assert counts["1"] == 2

    def test_engine_repairs_legacy_reset_parked_active_plan(
        self, temp_home, monkeypatch
    ):
        from datetime import datetime, timezone

        h = self._harness(temp_home, monkeypatch, accounts=1)
        reset_ts = h.clock.now + 86_400.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(100, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.switcher._usage_store.set_poll_plan(
            {"1": (reset_ts, 300.0)}, {"1": ("a@example.com", "")}
        )

        h.clock.advance(400)
        self._tick(h, counts, usage)
        assert counts["1"] == 2
        entry = h.switcher._usage_store.entries(
            {"1": ("a@example.com", "")}
        )["1"]
        assert entry.next_poll_at is not None
        assert entry.next_poll_at < reset_ts

    def test_band_jump_is_seen_at_most_one_poll_late(
        self, temp_home, monkeypatch
    ):
        # Active at 40% jumps into the band between polls: the jump is picked
        # up on the next planned poll, escalates the same tick, and the
        # movement flips the active onto the urgent cadence.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(40), "2": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        usage["1"] = _usage(80)
        h.clock.advance(60)
        self._tick(h, counts, usage)  # plan-skipped: still believed at 40%
        assert counts["1"] == 1
        h.clock.advance(120)
        self._tick(h, counts, usage)  # planned poll sees 80% → escalate-all
        assert counts["1"] == 2
        assert counts["2"] == 1  # at the TTL edge: still served, not refetched
        h.clock.advance(60)
        self._tick(h, counts, usage)  # movement in band → urgent cadence
        assert counts["1"] == 3
        assert counts["2"] == 2  # now stale → the escalation refreshes it

    def test_active_in_backoff_keeps_trusted_headroom(self, temp_home, monkeypatch):
        # The active account's fetches are being refused (429 with a long
        # Retry-After). Its last-good data ages past STALE_OK_S, but the
        # staleness is deliberate: headroom stays known, so no unhealthy
        # ticks and no escalate-all burst while the server is rate limiting.
        h = self._harness(temp_home, monkeypatch)
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        h.clock.advance(60)
        self._tick(h, counts, usage)
        h.switcher._usage_store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"1": ("a@example.com", "")},
        )
        h.clock.advance(400)  # active data now well past STALE_OK_S, in backoff
        counts.clear()
        outcome = self._tick(h, counts, usage)
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._unhealthy_ticks == 0
        assert "1" not in counts  # backoff respected
        assert sum(counts.values()) == 1  # baseline slot only, no escalate-all

    def test_all_exhausted_escalation_preserves_wider_plan(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch)
        usage = {num: _usage(100) for num in ("1", "2", "3")}
        counts: dict[str, int] = {}
        assert self._tick(h, counts, usage) is TickOutcome.BLOCKED
        assert counts == {"1": 1, "2": 1, "3": 1}

        # Simulate the wider plan learned after repeated 429s. The next
        # all-exhausted wake may refresh other stale rows, but escalation must
        # not defeat this token's congestion-control interval.
        h.switcher._usage_store.set_poll_plan(
            {"2": (h.clock.now + 1800.0, 1800.0)},
            {"2": ("b@example.com", "")},
        )
        h.clock.advance(NO_RESET_FALLBACK_S)
        assert self._tick(h, counts, usage) is TickOutcome.BLOCKED
        assert counts["2"] == 1

    def test_exhausted_candidate_keeps_a_bounded_poll_plan(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch)
        reset_iso = "2026-07-05T12:00:00Z"
        usage = {"1": _usage(50), "2": _usage(100, reset_iso), "3": _usage(20)}
        counts: dict[str, int] = {}
        for _ in range(3):
            self._tick(h, counts, usage)
            h.clock.advance(60)
        assert counts["2"] == 1
        entry = h.switcher._usage_store.entries(
            {"2": ("b@example.com", "")}
        )["2"]
        assert entry.poll_interval_s == poll_policy.EXHAUSTED_INTERVAL_S
        assert entry.next_poll_at is not None
        assert entry.next_poll_at <= (
            entry.fetched_at
            + poll_policy.EXHAUSTED_INTERVAL_S * (1 + poll_policy.JITTER_FRAC)
        )

    def test_poll_never_scheduled_past_a_window_reset(self, temp_home, monkeypatch):
        from datetime import datetime, timezone

        from claude_swap.autoswitch import RESET_SLACK_S

        # The candidate's default interval is 300s, but its 5h window resets
        # in 90s — its stored 40% is obsolete at the rollover, so the next
        # poll must be clamped to reset + slack rather than waiting it out.
        h = self._harness(temp_home, monkeypatch, accounts=2)
        reset_ts = h.clock.now + 90.0
        reset_iso = (
            datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        usage = {"1": _usage(50), "2": _usage(40, reset_iso)}
        counts: dict[str, int] = {}
        self._tick(h, counts, usage)
        entry = h.switcher._usage_store.entries(
            {"2": ("b@example.com", "")}
        )["2"]
        assert entry.next_poll_at == pytest.approx(reset_ts + RESET_SLACK_S)
        # Learned cadence untouched by the clamp.
        assert entry.poll_interval_s == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_movement_adapts_poll_interval(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, accounts=2)
        usage = {"1": _usage(50), "2": _usage(10)}
        counts: dict[str, int] = {}

        def interval() -> float | None:
            return h.switcher._usage_store.entries(
                {"2": ("b@example.com", "")}
            )["2"].poll_interval_s

        self._tick(h, counts, usage)          # first data point → base interval
        assert interval() == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S  # 300s
        h.clock.advance(180)
        self._tick(h, counts, usage)          # not due yet (300s interval)
        assert counts["2"] == 1
        h.clock.advance(120)
        self._tick(h, counts, usage)          # unmoved → backs off ×1.5
        assert counts["2"] == 2
        assert interval() == 450.0
        h.clock.advance(450)
        usage["2"] = _usage(20)               # moved 10 pts on another machine
        self._tick(h, counts, usage)
        assert counts["2"] == 3
        assert interval() == 225.0            # halved: polled closer while moving

    def test_idle_hold_skips_candidate_polling(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch)
        # Active token locally expired. The first tick now ATTEMPTS the
        # locked refresh (the fix's whole point); when it fails transiently
        # (network down), the row enters a failure backoff and subsequent
        # ticks surface the expired sentinel statically → idle-hold, with no
        # candidate slot spent.
        (h.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "expiresAt": 1000,
            },
        }))
        # The slot backup must be expired too — a non-expired backup would be
        # restored without any POST (no failure, no backoff, no hold).
        h.seed(1, "a@example.com", expires_at=1000)
        usage = {"2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        with patch(
            "claude_swap.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "network"),
        ):
            assert self._tick(h, counts, usage) is TickOutcome.NO_ACTION
            h.clock.advance(10)  # still inside the 30s failure backoff
            counts.clear()
            # Backoff established → the next tick polls nothing at all: the
            # active row is gated, the sentinel surfaces statically, and no
            # candidate slot is spent.
            assert self._tick(h, counts, usage) is TickOutcome.NO_ACTION
        assert counts == {}
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons[-1] == "active-idle"

    def test_poll_event_carries_fetch_errors(self, temp_home, monkeypatch):
        h = self._harness(temp_home, monkeypatch, accounts=2, unhealthy_ticks=3)
        counts: dict[str, int] = {}
        self._tick(
            h, counts, {"2": _usage(10)}, errors_by_num={"1": "http-429"}
        )
        poll = next(e for e in h.events if isinstance(e, PollEvent))
        assert poll.fetch_errors.get("1") == "http-429"
        assert "http-429" in poll.human()
        assert poll.to_json()["fetchErrors"] == {"1": "http-429"}

    def test_quarantined_candidate_never_consumes_the_poll_slot(
        self, temp_home, monkeypatch
    ):
        h = self._harness(temp_home, monkeypatch)
        h.engine._quarantine("2", "b@example.com", "invalid_grant")
        usage = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        counts: dict[str, int] = {}
        for _ in range(3):
            self._tick(h, counts, usage)
            h.clock.advance(60)
        # The alternate slot always went to account 3; 2 is dead weight.
        assert "2" not in counts
        assert counts["3"] >= 1

    def test_expired_active_enters_idle_hold_even_during_backoff(
        self, temp_home, monkeypatch
    ):
        """Finding-2 regression: the owned+expired sentinel must not be hidden
        by the active row's failure backoff (e.g. a Retry-After window), or
        the engine would count unhealthy ticks toward a spurious failover."""
        from claude_swap.usage_store import FetchRecord

        h = self._harness(temp_home, monkeypatch)
        # Active token locally expired while an owner is present.
        (h.temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live", "refreshToken": "rt-live",
                "expiresAt": 1000,
            },
        }))
        # Active row sits in a long failure backoff → the fetch path (and its
        # own expired short-circuit) is unreachable this tick.
        h.switcher._usage_store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"1": ("a@example.com", "")},
        )
        counts: dict[str, int] = {}
        outcome = self._tick(h, counts, {"2": _usage(10), "3": _usage(20)})
        assert outcome is TickOutcome.NO_ACTION
        assert h.engine._unhealthy_ticks == 0
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["active-idle"]

    def test_consume_first_hold_never_escalates_below_threshold(
        self, temp_home, monkeypatch
    ):
        """Flat-traffic guard: a below-threshold consume-first tick that ends
        in a hold (no switch would fire) keeps the O(1) baseline — the
        phase-2 escalation is reserved for ticks that would actually switch.
        The fetch-set spy also catches an accidental all-candidates request
        that reserve() would have served from the store without HTTP."""
        h = self._harness(temp_home, monkeypatch, strategy="consume-first")
        # Active resets soonest -> every tick holds already-consuming-soonest.
        # five_hour 50 mirrors the baseline-cadence test's active plan.
        usage = {
            "1": _usage7(50, 20, _R_SOON),
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        }
        counts: dict[str, int] = {}
        fetch_sets: list[set] = []
        real_collect = h.switcher.usage_entries_by_account

        def spying_collect(*args, **kwargs):
            fetch_sets.append(set(kwargs.get("fetch") or ()))
            return real_collect(*args, **kwargs)

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=spying_collect
        ):
            for _ in range(4):  # t0, t60, t120, t180
                outcome = self._tick(h, counts, usage)
                assert outcome is TickOutcome.NO_ACTION
                h.clock.advance(60)
        # (a) HTTP volume identical to the baseline cadence under `best`.
        assert counts == {"1": 2, "2": 1, "3": 1}
        # (b) no collection ever requested the all-candidates escalation set.
        assert {"1", "2", "3"} not in fetch_sets

    def test_consume_first_stale_target_holds_then_switches(
        self, temp_home, monkeypatch
    ):
        """Stale-after-escalation: when the phase-2 refetch cannot freshen the
        chosen target (Retry-After backoff), the freshness gate holds with
        stale-usage instead of switching on old data; once the backoff lapses
        a later tick freshens the target and the switch lands."""
        h = self._harness(temp_home, monkeypatch, strategy="consume-first")
        counts: dict[str, int] = {}
        # Populate the store while the active account resets soonest (holds).
        view_a = {
            "1": _usage7(50, 20, _R_SOON),
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        }
        self._tick(h, counts, view_a)          # t0: fetches 1, 2
        h.clock.advance(60)
        self._tick(h, counts, view_a)          # t60: fetches 3
        assert counts == {"1": 1, "2": 1, "3": 1}
        # #2 enters a Retry-After backoff; its stored entry ages past the
        # serve TTL (180s) while staying inside decision trust (300s).
        h.switcher._usage_store.record(
            {"2": FetchRecord(error="http-429", retry_after_s=600.0)},
            {"2": ("b@example.com", "")},
        )
        h.clock.advance(181)                   # t241
        h.events.clear()
        # The active refetch now reports the LATEST reset, so stored #2
        # (age 241: decision-trusted, no longer fresh) is the provisional
        # pick — but phase 2 cannot freshen it through the backoff.
        view_b = {
            "1": _usage7(50, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome = self._tick(h, counts, view_b)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert "stale-usage" in reasons
        assert counts["2"] == 1  # the backoff kept every refetch off #2
        # Backoff lapses -> a later tick freshens #2 and the switch lands.
        h.events.clear()
        h.clock.advance(700)
        outcome = self._tick(h, counts, view_b)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "consume-first"


class TestApiKeyAccounts:
    def _mark_api_key(self, harness, num: int) -> None:
        data = harness.switcher._get_sequence_data()
        data["accounts"][str(num)]["kind"] = "api_key"
        harness.switcher._write_json(harness.switcher.sequence_file, data)

    def test_api_key_candidate_excluded_by_default(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        outcome = h.tick_with_usage({"1": _usage(95), "2": "api key"})
        assert outcome is TickOutcome.BLOCKED
        assert h.active_number() == 1

    def test_api_key_is_last_resort_when_included(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        # A qualifying OAuth candidate wins over the API key...
        outcome = h.tick_with_usage({
            "1": _usage(95), "2": "api key", "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_api_key_used_when_oauth_exhausted(self, temp_home):
        h = EngineHarness(temp_home, include_api_key_accounts=True)
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        self._mark_api_key(h, 2)
        outcome = h.tick_with_usage({
            "1": _usage(100), "2": "api key", "3": _usage(100),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_active_api_key_idles_engine(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "key@token.local")
        h.seed(2, "b@example.com")
        h.make_live("key@token.local", 1)
        self._mark_api_key(h, 1)
        outcome = h.tick_with_usage({"1": "api key", "2": _usage(10)})
        assert outcome is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "active-api-key"
        ]


class TestFreshening:
    def test_near_expiry_target_is_refreshed_and_persisted(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 60_000)
        h.make_live("a@example.com", 1)

        rotated = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-2-new",
                "refreshToken": "rt-2-new",
                "expiresAt": int(h.clock() * 1000) + 3_600_000,
            }
        })
        live_creds_path = temp_home / ".claude" / ".credentials.json"
        live_before = live_creds_path.read_text()
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(rotated, None),
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED
        mock_refresh.assert_called_once()
        # Freshening itself never touched the active store (the switch did,
        # afterwards, via _perform_switch): the rotated token must have gone
        # through the backup, and now be live.
        assert "sk-2-new" in live_creds_path.read_text()
        assert live_creds_path.read_text() != live_before

    def test_fresh_target_is_not_refreshed(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.SWITCHED
        mock_refresh.assert_not_called()

    def test_invalid_grant_quarantines_and_tries_next(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "invalid_grant"),
        ):
            outcome = h.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(20),
            })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3  # next candidate after 2 was quarantined
        q = next(e for e in h.events if isinstance(e, QuarantineEvent))
        assert (q.number, q.reason) == ("2", "invalid_grant")
        assert "2" in h.state()["quarantine"]

    def test_transient_failure_skips_without_quarantine(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)
        h.make_live("a@example.com", 1)
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(None, "transient"),
        ):
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.ERROR
        assert h.active_number() == 1
        assert not h.state().get("quarantine")
        assert any(isinstance(e, ErrorEvent) for e in h.events)

    def test_live_session_target_is_skipped_even_with_fresh_token(self, temp_home):
        # Auto never activates an account that has a live `cswap run` session:
        # dual refresh-token ownership with nobody reading the warning.
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=int(h.clock() * 1000) + 3_600_000)
        h.make_live("a@example.com", 1)
        with patch.object(
            h.switcher, "live_session_pids_for", return_value=[4242]
        ), patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.BLOCKED
        mock_refresh.assert_not_called()
        assert h.active_number() == 1

    def test_live_session_near_expiry_is_skipped(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.make_live("a@example.com", 1)
        with patch.object(
            h.switcher, "live_session_pids_for", return_value=[4242]
        ), patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})
        assert outcome is TickOutcome.BLOCKED
        mock_refresh.assert_not_called()
        assert h.active_number() == 1


class TestQuarantineLifecycle:
    def test_quarantine_persists_across_engine_instances(self, harness):
        harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        harness.events.clear()
        fresh_engine = harness._make_engine()
        usage = {"1": _usage(95), "2": _usage(0), "3": _usage(50)}
        with patch.object(
            harness.switcher,
            "usage_entries_by_account",
            return_value={
                num: _entry_for(value, harness.clock.now)
                for num, value in usage.items()
            },
        ):
            outcome = fresh_engine.tick()
        # 2 has the most headroom but is quarantined → 3 wins.
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_replaced_credentials_lift_quarantine(self, harness):
        harness.engine._quarantine("2", "b@example.com", "invalid_grant")
        # User re-logged in and re-captured the slot: new refresh token.
        harness.switcher._write_account_credentials(
            "2",
            "b@example.com",
            json.dumps({
                "claudeAiOauth": {"accessToken": "sk-2b", "refreshToken": "rt-2b"},
            }),
        )
        harness.events.clear()
        outcome = harness.tick_with_usage({
            "1": _usage(95), "2": _usage(0), "3": _usage(50),
        })
        assert any(isinstance(e, UnquarantineEvent) for e in harness.events)
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 2
        assert "2" not in (harness.state().get("quarantine") or {})

    def test_state_lock_preserves_concurrent_writes(self, harness):
        # Simulate another engine writing between our read and our write: the
        # RMW under the state lock must preserve its quarantine entry.
        harness.engine._mutate_state(
            lambda s: s.setdefault("quarantine", {}).update(
                {"3": {"email": "c@example.com", "reason": "invalid_grant",
                       "at": "x", "refreshTokenFingerprint": None}}
            )
        )
        harness.engine._mutate_state(lambda s: s.update(lastSwitchAt=123.0))
        state = harness.state()
        assert state["lastSwitchAt"] == 123.0
        assert "3" in state["quarantine"]


class TestDryRunAndNoOp:
    def test_dry_run_mutates_nothing(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.engine = h._make_engine(dry_run=True)
        live_before = (temp_home / ".claude" / ".credentials.json").read_text()

        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.dry_run is True
        assert h.active_number() == 1  # unchanged
        assert (temp_home / ".claude" / ".credentials.json").read_text() == live_before
        assert h.state() == {}  # no lastSwitchAt recorded

    def test_dry_run_never_freshens_or_quarantines(self, temp_home):
        # A near-expiry target would normally be refreshed (a real token
        # rotation) and a dead one quarantined (a state write). Dry-run must
        # stop at the decision: no network, no writes of any kind.
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com", expires_at=1)  # long expired
        h.make_live("a@example.com", 1)
        h.engine = h._make_engine(dry_run=True)
        backup_before = h.switcher.read_account_credentials("2", "b@example.com")

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials"
        ) as mock_refresh:
            outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert outcome is TickOutcome.SWITCHED  # reported the would-switch
        mock_refresh.assert_not_called()
        assert h.switcher.read_account_credentials("2", "b@example.com") == backup_before
        assert h.state() == {}  # no quarantine, no lastSwitchAt

    def test_dry_run_does_not_release_quarantines(self, temp_home):
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.engine._quarantine("2", "b@example.com", "invalid_grant")
        # Replace the credential — a real tick would lift the quarantine.
        h.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {"accessToken": "n", "refreshToken": "n"}}),
        )
        h.events.clear()
        h.engine = h._make_engine(dry_run=True)
        state_before = h.state()

        outcome = h.tick_with_usage({"1": _usage(95), "2": _usage(10)})

        assert not any(isinstance(e, UnquarantineEvent) for e in h.events)
        assert h.state() == state_before  # state file untouched
        # And the still-recorded quarantine keeps 2 out of the dry-run plan.
        assert outcome is TickOutcome.BLOCKED

    def test_already_active_result_is_noop(self, harness):
        with patch.object(
            harness.switcher,
            "switch_to",
            return_value={"switched": False, "reason": "already-active"},
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(50),
            })
        assert outcome is TickOutcome.NO_ACTION
        assert "lastSwitchAt" not in harness.state()


class TestEventsShape:
    def test_every_event_has_envelope(self, harness):
        harness.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        assert harness.events
        for event in harness.events:
            payload = event.to_json()
            assert payload["schemaVersion"] == 1
            assert payload["event"] == event.kind
            assert payload["ts"].endswith("Z")

    def test_switch_event_refs_match_account_ref_shape(self, harness):
        harness.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(50)})
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        payload = switch.to_json()
        assert payload["from"] == {"number": 1, "email": "a@example.com"}
        assert payload["to"] == {"number": 2, "email": "b@example.com"}

    def test_poll_event_human_line(self, harness):
        harness.tick_with_usage({"1": _usage(42), "2": _usage(10), "3": None})
        poll = next(e for e in harness.events if isinstance(e, PollEvent))
        line = poll.human()
        assert "Account-1" in line and "42% used" in line
        # Others show per-window pcts, not just the ambiguous binding pct.
        assert "#2: 5h 10% · 7d 0%" in line
        assert "#3: ?" in line

    def test_poll_event_windows_match_the_decision_set(self, temp_home):
        # Scoped windows appear only when configured: rendering an ignored
        # Fable 100% next to a switch onto that account would read as a bug.
        usage = {
            "1": _usage(42),
            "2": {
                "five_hour": {"pct": 3.0},
                "seven_day": {"pct": 89.0},
                "scoped": [{"name": "Fable", "pct": 21.0}],
            },
        }

        def build(**kw):
            h = EngineHarness(temp_home, **kw)
            h.seed(1, "a@example.com")
            h.seed(2, "b@example.com")
            h.make_live("a@example.com", 1)
            return h

        plain = build()
        plain.tick_with_usage(usage)
        poll = next(e for e in plain.events if isinstance(e, PollEvent))
        assert "#2: 5h 3% · 7d 89%" in poll.human()
        assert "Fable" not in poll.human()
        assert poll.to_json()["windowsPct"]["2"] == {"5h": 3.0, "7d": 89.0}

        modeled = build(model="Fable")
        modeled.tick_with_usage(usage)
        poll = next(e for e in modeled.events if isinstance(e, PollEvent))
        assert "#2: 5h 3% · 7d 89% · Fable 21%" in poll.human()
        assert poll.to_json()["windowsPct"]["2"] == {
            "5h": 3.0, "7d": 89.0, "Fable": 21.0,
        }


class TestRunLoop:
    def test_loop_ticks_until_stopped(self, harness):
        ticks = []

        def fake_tick():
            ticks.append(1)
            if len(ticks) >= 2:
                harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(harness.engine, "tick", side_effect=fake_tick), \
             patch.object(harness.engine._wake, "wait", return_value=None):
            assert harness.engine.run_loop() == 0
        assert len(ticks) == 2

    def test_loop_survives_raising_tick(self, harness):
        calls = []

        def raising_inner():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(
            harness.engine, "_tick_inner", side_effect=raising_inner
        ), patch.object(harness.engine._wake, "wait", return_value=None):
            harness.engine.run_loop()
        assert len(calls) == 2
        assert any(isinstance(e, ErrorEvent) for e in harness.events)

    def test_stop_before_start_is_not_lost(self, harness):
        # A stop() issued before the worker thread enters run_loop must not
        # be cleared away: the loop exits without a single tick.
        harness.engine.stop()
        with patch.object(harness.engine, "tick") as tick:
            assert harness.engine.run_loop() == 0
        tick.assert_not_called()

    def test_wake_during_tick_cuts_the_following_sleep_short(self, harness):
        # No wait patching on purpose: if the clear-at-top ordering were
        # wrong (wake cleared after the wait), the wake fired during tick 1
        # would be lost and the loop would block on the real 60s sleep —
        # caught by the join timeout instead of hanging the suite.
        ticks: list[int] = []

        def fake_tick():
            ticks.append(1)
            if len(ticks) == 1:
                harness.engine.wake()  # e.g. apply_threshold landed mid-tick
            else:
                harness.engine.stop()
            return TickOutcome.NO_ACTION

        with patch.object(harness.engine, "tick", side_effect=fake_tick):
            worker = threading.Thread(target=harness.engine.run_loop)
            worker.start()
            worker.join(timeout=10)
            finished = not worker.is_alive()
            harness.engine.stop()  # unblock a failing loop before asserting
            worker.join(timeout=5)
        assert finished
        assert len(ticks) == 2

    def test_blocked_with_reset_rechecks_at_exhausted_cadence(self, harness):
        harness.engine._sleep_until_ts = harness.clock() + 1800
        delay = harness.engine._next_delay(TickOutcome.BLOCKED)
        assert delay == poll_policy.EXHAUSTED_INTERVAL_S

    def test_blocked_exhausted_without_reset_uses_fallback(self, harness):
        harness.engine._sleep_until_ts = None
        harness.engine._blocked_wait_long = True
        assert harness.engine._next_delay(TickOutcome.BLOCKED) == 300.0

    def test_blocked_on_resolvable_condition_keeps_normal_cadence(self, harness):
        harness.engine._sleep_until_ts = None
        harness.engine._blocked_wait_long = False
        delay = harness.engine._next_delay(TickOutcome.BLOCKED)
        assert 0.9 * 60 <= delay <= 1.1 * 60

    def test_normal_delay_is_jittered_interval(self, harness):
        delay = harness.engine._next_delay(TickOutcome.NO_ACTION)
        assert 0.9 * 60 <= delay <= 1.1 * 60

    def test_sleep_cap(self, harness):
        harness.engine._sleep_until_ts = harness.clock() + 50 * 3600
        assert (
            harness.engine._next_delay(TickOutcome.BLOCKED)
            == poll_policy.EXHAUSTED_INTERVAL_S
        )


class TestSessionThreshold:
    """apply_threshold(): the TUI's session-only, mid-run override."""

    def test_apply_threshold_retargets_trigger_and_poll_pin(self, harness):
        harness.engine.apply_threshold(72.0)
        assert harness.engine.settings.threshold == 72.0
        # Poll-cadence planning follows the new value immediately.
        assert harness.switcher._poll_inputs_override == (72.0, ())
        # And the very next tick decides with it: 80% ≥ 72 switches, where
        # the constructed 90 would not have.
        outcome = harness.tick_with_usage({
            "1": _usage(80), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED

    def test_clear_poll_policy_inputs_unpins(self, harness):
        harness.engine.apply_threshold(72.0)
        harness.switcher.clear_poll_policy_inputs()
        assert harness.switcher._poll_inputs_override is None

    def _collect_fetch_sets(self, harness, threshold: float) -> list:
        entries = {
            n: _entry_for(_usage(80.0 if n == "1" else 10.0), harness.clock.now)
            for n in ("1", "2", "3")
        }
        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ) as collect:
            harness.engine._collect_scheduled_usage("1", threshold=threshold)
        return [c.kwargs.get("fetch") for c in collect.call_args_list]

    def test_collect_escalates_on_the_tick_snapshot_threshold(self, harness):
        # Escalation must key on the threshold captured by the tick, not a
        # re-read of self.settings (engine settings stay at 90 throughout).
        # Active at 80%: within ESCALATION_MARGIN_PCT of 90 → full refresh...
        assert {"1", "2", "3"} in self._collect_fetch_sets(harness, 90.0)
        # ...but not of 99.9 → baseline fetching only.
        assert {"1", "2", "3"} not in self._collect_fetch_sets(harness, 99.9)


def _scoped_usage(five_h: float, fable: float | None) -> dict:
    """5h/7d plus (optionally) a per-model weekly Fable window."""
    usage: dict = {"five_hour": {"pct": five_h}, "seven_day": {"pct": 10.0}}
    if fable is not None:
        usage["scoped"] = [{"name": "Fable", "pct": fable}]
    return usage


class TestSettingsReload:
    """A settings.json edit reaches a *running* engine, no restart needed.

    The model axes used to be frozen at construction, so toggling
    ``autoswitch.model`` only took effect after the daemon was restarted —
    which makes a settings-file-backed checkbox broken by construction.
    """

    def _seeded(self, temp_home: Path, **settings_kwargs) -> EngineHarness:
        h = EngineHarness(temp_home, **settings_kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def _two_ticks(self, harness, entries: dict, edit) -> list[PollEvent]:
        """Run the loop for exactly two ticks, applying ``edit`` between them."""
        ticks: list[int] = []
        real_tick = harness.engine.tick

        def tick_then_edit():
            ticks.append(1)
            outcome = real_tick()
            if len(ticks) == 1:
                edit()
            else:
                harness.engine.stop()
            return outcome

        with patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries
        ), patch.object(
            harness.engine, "tick", side_effect=tick_then_edit
        ), patch.object(harness.engine._wake, "wait", return_value=None):
            harness.engine.run_loop()
        return [e for e in harness.events if isinstance(e, PollEvent)]

    def test_model_key_appearing_rebinds_the_windows_mid_loop(self, harness):
        # The fleet shape the toggle exists for: fine on 5h/7d, spent on the
        # per-model weekly window.
        entries = {
            n: _entry_for(_scoped_usage(20.0, 96.0), harness.clock.now)
            for n in ("1", "2", "3")
        }
        polls = self._two_ticks(
            harness,
            entries,
            lambda: set_setting(
                harness.switcher.backup_dir, "autoswitch.model", "Fable"
            ),
        )
        assert len(polls) == 2
        # Tick 1 — account-wide windows only.
        assert list(polls[0].windows["1"]) == ["5h", "7d"]
        assert polls[0].headroom["1"] == 80.0
        # Tick 2 — same engine object, new binding window and headroom.
        assert list(polls[1].windows["1"]) == ["5h", "7d", "Fable"]
        assert polls[1].headroom["1"] == 4.0
        assert harness.engine._models == ("Fable",)
        # The collector's poll planning must key on the same axes, or plans
        # would be computed against a window the decision no longer uses.
        assert harness.switcher._poll_inputs_override == (90.0, ("Fable",))

    def test_model_key_disappearing_rebinds_the_windows_mid_loop(self, temp_home):
        h = self._seeded(temp_home, model="Fable")
        set_setting(h.switcher.backup_dir, "autoswitch.model", "Fable")
        h.engine = h._make_engine()  # constructed with the key already in the file
        entries = {
            n: _entry_for(_scoped_usage(20.0, 96.0), h.clock.now)
            for n in ("1", "2", "3")
        }
        polls = self._two_ticks(
            h,
            entries,
            lambda: unset_setting(h.switcher.backup_dir, "autoswitch.model"),
        )
        assert len(polls) == 2
        assert list(polls[0].windows["1"]) == ["5h", "7d", "Fable"]
        assert polls[0].headroom["1"] == 4.0
        assert list(polls[1].windows["1"]) == ["5h", "7d"]
        assert polls[1].headroom["1"] == 80.0
        assert h.engine._models == ()
        assert h.switcher._poll_inputs_override == (90.0, ())

    def test_reload_carries_the_unreported_window_rule(self, harness):
        # 2026-08-02: an account that reports no Fable window must read as
        # UNKNOWN once Fable is configured, never as its healthy 5h/7d
        # headroom. Turning the key on mid-loop must inherit that rule, or
        # the toggle would hand the escape a slot with unverified Fable
        # access — exactly the incident, re-entered through the new path.
        entries = {
            "1": _entry_for(_scoped_usage(20.0, 96.0), harness.clock.now),
            "2": _entry_for(_scoped_usage(8.0, None), harness.clock.now),
            "3": _entry_for(_scoped_usage(12.0, None), harness.clock.now),
        }
        polls = self._two_ticks(
            harness,
            entries,
            lambda: set_setting(
                harness.switcher.backup_dir, "autoswitch.model", "Fable"
            ),
        )
        # Before the edit #2 looks like a fine target on 5h/7d alone...
        assert polls[0].headroom["2"] == 90.0
        # ...after it, its unreported Fable window reads as unknown, and the
        # engine blocks instead of escaping onto an unverified account.
        assert polls[1].headroom["2"] is None
        assert polls[1].headroom["3"] is None
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold", "no-comparison"]
        assert harness.active_number() == 1

    def test_cli_flag_still_beats_the_file_after_a_reload(self, temp_home):
        # `cswap auto --threshold 75` must stay at 75 even when the file is
        # edited to 95 — an explicit flag outranks the file, before and after.
        h = self._seeded(temp_home, threshold=75.0)
        h.engine = h._make_engine(overrides={"threshold": 75.0})
        set_setting(h.switcher.backup_dir, "autoswitch.threshold", "95")
        set_setting(h.switcher.backup_dir, "autoswitch.model", "Fable")
        outcome = h.tick_with_usage({
            "1": _scoped_usage(80.0, 30.0),
            "2": _scoped_usage(10.0, 5.0),
            "3": _scoped_usage(10.0, 5.0),
        })
        # 80% ≥ the pinned 75 → switch; at the file's 95 nothing would move.
        assert outcome is TickOutcome.SWITCHED
        assert h.engine.settings.threshold == 75.0
        poll = next(e for e in h.events if isinstance(e, PollEvent))
        assert poll.threshold == 75.0
        # ...while a field the flag did NOT pin still follows the file.
        assert h.engine._models == ("Fable",)
        assert list(poll.windows["1"]) == ["5h", "7d", "Fable"]
        assert h.switcher._poll_inputs_override == (75.0, ("Fable",))

    def test_public_models_accessor_tracks_the_file(self, harness):
        # The one axis source a frontend may read: the auto screen ranks its
        # "Next best" list on it, so it has to be the same tuple the decision
        # binds on — after a reload as much as at construction.
        assert harness.engine.models == () == harness.engine._models
        set_setting(harness.switcher.backup_dir, "autoswitch.model", "Fable")
        harness.tick_with_usage({
            n: _scoped_usage(pct, 20.0)
            for n, pct in (("1", 50.0), ("2", 10.0), ("3", 10.0))
        })
        assert harness.engine.models == ("Fable",) == harness.engine._models

    def test_session_threshold_survives_a_reload(self, harness):
        # The TUI's session-only override is a pin too: a reload triggered by
        # an unrelated edit must not silently restore the file value.
        harness.engine.apply_threshold(72.0)
        set_setting(harness.switcher.backup_dir, "autoswitch.cooldownSeconds", "10")
        harness.tick_with_usage({"1": _usage(50), "2": _usage(10), "3": _usage(10)})
        assert harness.engine.settings.threshold == 72.0
        assert harness.engine.settings.cooldown_seconds == 10.0

    def test_model_name_typo_guard_rearms_on_a_new_name(self, temp_home):
        # The guard is one-shot per model set. A name changed mid-run has
        # never been checked, so the guard must fire again — otherwise a
        # typo'd toggle blocks every switch with no warning at all.
        h = self._seeded(temp_home, model="Fable")
        set_setting(h.switcher.backup_dir, "autoswitch.model", "Fable")
        h.engine = h._make_engine()
        entries = {
            n: _entry_for(_scoped_usage(20.0, 40.0), h.clock.now)
            for n in ("1", "2", "3")
        }
        self._two_ticks(
            h,
            entries,
            lambda: set_setting(
                h.switcher.backup_dir, "autoswitch.model", "Fabel"
            ),
        )
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1
        assert "Fabel" in warnings[0].message

    def test_new_axis_decides_on_stored_data_without_waiting_for_a_refetch(
        self, temp_home, monkeypatch
    ):
        # Poll plans in the usage store were computed against the OLD window
        # set, so the accounts are not due for minutes after the edit. That
        # must not delay the decision: lastGood carries the scoped windows
        # already, so headroom re-derives from stored data on the very next
        # tick and the switch happens with zero extra fetches.
        monkeypatch.setattr("claude_swap.switcher._FETCH_STAGGER_S", 0)
        h = self._seeded(temp_home)
        monkeypatch.setattr(h.switcher, "_live_session_pids", lambda *a: [])
        usage = {
            "1": _scoped_usage(20.0, 96.0),
            "2": _scoped_usage(20.0, 5.0),
            "3": _scoped_usage(20.0, 96.0),
        }
        counts: dict[str, int] = {}

        def fake_fetch(num, email, creds, is_active=False, persist_credentials=None):
            counts[num] = counts.get(num, 0) + 1
            return oauth.UsageOutcome(dict(usage[num]))

        def tick():
            with patch(
                "claude_swap.oauth.try_fetch_usage_for_account",
                side_effect=fake_fetch,
            ):
                return h.engine.tick()

        # Warm every slot into the store first. A baseline pass polls the
        # active slot plus ONE due candidate, so after a single tick the third
        # account still has no row — and its first-ever fetch on the next tick
        # would be miscounted as the reload forcing a refetch.
        for _ in range(5):
            assert tick() is TickOutcome.NO_ACTION
            if set(counts) == {"1", "2", "3"}:
                break
            h.clock.advance(1)
        fetched = dict(counts)
        assert set(fetched) == {"1", "2", "3"}  # real rows, with their plans

        h.clock.advance(60)  # well inside every plan written a moment ago
        set_setting(h.switcher.backup_dir, "autoswitch.model", "Fable")
        assert tick() is TickOutcome.SWITCHED
        assert counts == fetched  # not one extra request
        assert h.active_number() == 2

    def test_settings_edit_ends_a_long_sleep_early(self, harness):
        # A BLOCKED tick sleeps for minutes; honoring an edit only after that
        # is indistinguishable from "restart the daemon".
        calls: list[float] = []

        def fake_wait(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                set_setting(
                    harness.switcher.backup_dir, "autoswitch.model", "Fable"
                )
            return False

        with patch.object(harness.engine._wake, "wait", side_effect=fake_wait):
            harness.engine._wait_between_ticks(NO_RESET_FALLBACK_S)
        assert calls == [SETTINGS_WATCH_S]

    def test_untouched_settings_sleep_the_whole_delay(self, harness):
        calls: list[float] = []

        with patch.object(
            harness.engine._wake, "wait", side_effect=lambda t: calls.append(t)
        ):
            harness.engine._wait_between_ticks(NO_RESET_FALLBACK_S)
        assert len(calls) == math.ceil(NO_RESET_FALLBACK_S / SETTINGS_WATCH_S)
        assert sum(calls) == pytest.approx(NO_RESET_FALLBACK_S)


class TestSettingsReloadUnreadable:
    """A settings.json that does not answer must not rewrite a live policy.

    ``load_settings`` collapses missing/corrupt into defaults, which is the
    right semantics for a one-shot read and the wrong one for a re-read: a
    half-written or deleted file would otherwise revert model axes, strategy,
    threshold and cooldown on a running daemon, with nothing in the event
    stream to explain it.
    """

    POLICY = {
        "autoswitch.model": "Fable",
        "autoswitch.strategy": "consume-first",
        "autoswitch.threshold": "80",
        "autoswitch.cooldownSeconds": "600",
    }

    def _running(self, temp_home: Path) -> EngineHarness:
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        for dotted, value in self.POLICY.items():
            set_setting(h.switcher.backup_dir, dotted, value)
        h.settings = load_settings(h.switcher.backup_dir)
        h.engine = h._make_engine()
        self._assert_policy_intact(h)
        return h

    def _assert_policy_intact(self, h: EngineHarness) -> None:
        assert h.engine._models == ("Fable",)
        assert h.engine.settings.strategy == "consume-first"
        assert h.engine.settings.threshold == 80.0
        assert h.engine.settings.cooldown_seconds == 600.0
        assert h.switcher._poll_inputs_override == (80.0, ("Fable",))

    def _quiet_tick(self, h: EngineHarness) -> None:
        # Every account reports the configured Fable window, so the only
        # warning this class can ever see is the one under test.
        h.tick_with_usage({
            n: _scoped_usage(pct, 20.0)
            for n, pct in (("1", 50.0), ("2", 10.0), ("3", 10.0))
        })

    def _warnings(self, h: EngineHarness) -> list[ConfigWarningEvent]:
        return [e for e in h.events if isinstance(e, ConfigWarningEvent)]

    def test_corrupt_file_keeps_the_policy_and_says_so_once(self, temp_home):
        h = self._running(temp_home)
        path = settings_path(h.switcher.backup_dir)
        path.write_text('{"autoswitch": {"model": "Fable",}}', encoding="utf-8")
        self._quiet_tick(h)
        self._assert_policy_intact(h)
        warnings = self._warnings(h)
        assert len(warnings) == 1
        assert str(path) in warnings[0].message
        # ...and it stays one line, not one per tick for as long as it is broken.
        self._quiet_tick(h)
        self._assert_policy_intact(h)
        assert len(self._warnings(h)) == 1

    def test_deleted_file_keeps_the_policy_and_says_so_once(self, temp_home):
        h = self._running(temp_home)
        path = settings_path(h.switcher.backup_dir)
        path.unlink()
        self._quiet_tick(h)
        self._assert_policy_intact(h)
        assert len(self._warnings(h)) == 1

    def test_a_file_that_never_existed_warns_about_nothing(self, harness):
        # Running with no settings.json is the default install, not a fault.
        assert not settings_path(harness.switcher.backup_dir).exists()
        harness.tick_with_usage({"1": _usage(50), "2": _usage(10), "3": _usage(10)})
        assert self._warnings(harness) == []

    def test_a_repaired_file_is_adopted_again(self, temp_home):
        h = self._running(temp_home)
        path = settings_path(h.switcher.backup_dir)
        good = json.loads(path.read_text(encoding="utf-8"))
        path.write_text('{"autoswitch": {"model": "Fable",}}', encoding="utf-8")
        self._quiet_tick(h)
        good["autoswitch"].pop("model", None)
        path.write_text(json.dumps(good), encoding="utf-8")
        self._quiet_tick(h)
        assert h.engine._models == ()
        assert h.engine.settings.strategy == "consume-first"  # untouched key
        assert h.switcher._poll_inputs_override == (80.0, ())


class TestPctLabel:
    def test_whole_numbers_drop_the_decimal(self):
        assert pct_label(90.0) == "90"

    def test_fractional_threshold_keeps_one_decimal(self):
        # .0f would render the valid maximum 99.9 as a lying "100".
        assert pct_label(99.9) == "99.9"

    def test_configured_precision_is_preserved(self):
        # settings.json accepts arbitrary floats; display must not round.
        assert pct_label(85.55) == "85.55"
        assert pct_label(85.555555) == "85.555555"

    def test_float_noise_is_absorbed(self):
        assert pct_label(100.0 - 37.4) == "62.6"
        assert pct_label(99.85000000000001) == "99.85"

    def test_poll_event_shows_fractional_threshold(self):
        poll = PollEvent(
            active={"number": 1, "email": "a@example.com"},
            headroom={"1": 40.0},
            threshold=99.9,
        )
        assert "switch at 99.9%" in poll.human()

    def test_below_threshold_detail_shows_fractional_threshold(self, temp_home):
        h = EngineHarness(temp_home, threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": _usage(50), "2": _usage(10)})
        details = [
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        ]
        assert details == ["50% < 99.9%"]

    def test_below_threshold_detail_never_shows_impossible_comparison(
        self, temp_home
    ):
        # utilization 99.85 with threshold 99.9: .0f on the left side used
        # to render the logically impossible "100% < 99.9%".
        h = EngineHarness(temp_home, threshold=99.9)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        h.tick_with_usage({"1": _usage(99.85), "2": _usage(10)})
        details = [
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        ]
        assert details == ["99.85% < 99.9%"]


class TestTokenIdentity:
    """The token endpoint's free identity data: uuid backfill and the
    identity-conflict detector (the zero-request check that catches a
    poisoned slot the moment auto freshens it)."""

    def test_uuid_backfill_from_token_account_on_freshen(self, harness):
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["uuid"] = ""
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        # Slot 2 near expiry → freshen path runs.
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-2-real", "email": "b@example.com",
                 "organizationUuid": ""},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "ok"
        assert harness.switcher._get_sequence_data()["accounts"]["2"]["uuid"] == (
            "uuid-2-real"
        )

    def test_conflicting_token_identity_returns_identity_conflict(self, harness):
        """A slot whose credential authenticates as a different account is not
        a viable target — but the rotated generation is still persisted (the
        grant consumed its predecessor)."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-somebody-else", "email": "z@example.com",
                 "organizationUuid": ""},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"
        # The consumed generation's successor was persisted regardless.
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh

    def test_identity_conflict_quarantines_instead_of_activating(self, harness):
        """Tick path: the conflicted slot is quarantined (wrong-account switch
        prevented); rotation falls through to the next candidate."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})

        def refresh(creds):
            data = json.loads(creds)["claudeAiOauth"]
            if data["refreshToken"] == "rt-2":
                return oauth.RefreshOutcome(
                    fresh, None,
                    {"uuid": "uuid-somebody-else", "email": "z@example.com",
                     "organizationUuid": ""},
                )
            return oauth.RefreshOutcome(creds, None)

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            side_effect=refresh,
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(80),
            })
        # Account 2 had the most headroom but is conflicted → quarantined,
        # and the switch landed elsewhere.
        assert "account-quarantined" in harness.kinds()
        q = harness.state().get("quarantine", {})
        assert q.get("2", {}).get("reason") == "identity-conflict"
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_dead_slot_quarantined_even_with_safety_copy_present(self, harness):
        """No automatic promotion (fail-open rework of the issue #117 guard):
        a dead slot is quarantined outright; safety copies are forensic
        material, and recovery is the documented /login + cswap add."""
        harness.switcher._store._write_unclaimed_credential(
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2-successor",
                "refreshToken": "rt-2-successor",
                "expiresAt": 99_999_999_999_000,
            }}),
            {"resolvedIdentity": {
                "uuid": "uuid-2", "email": "b@example.com",
                "organizationUuid": "",
            }},
        )
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2-dead", "refreshToken": "rt-2-dead",
                "expiresAt": 0,
            }}),
        )

        def refresh(creds):
            data = json.loads(creds)["claudeAiOauth"]
            if data["refreshToken"] == "rt-2-dead":
                return oauth.RefreshOutcome(None, "invalid_grant")
            return oauth.RefreshOutcome(creds, None)

        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            side_effect=refresh,
        ):
            outcome = harness.tick_with_usage({
                "1": _usage(95), "2": _usage(10), "3": _usage(80),
            })
        q = harness.state().get("quarantine", {})
        assert q.get("2", {}).get("reason") == "invalid_grant"
        # The safety copy was not consumed, and the switch landed elsewhere.
        assert len(harness.switcher.list_unclaimed_credentials()) == 1
        assert outcome is TickOutcome.SWITCHED
        assert harness.active_number() == 3

    def test_same_uuid_different_org_is_identity_conflict(self, harness):
        """Organization is part of account identity everywhere else in the
        codebase: the same account uuid under a different org is a conflict
        (org compared only when both sides record one)."""
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["organizationUuid"] = "org-2"
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-2", "email": "b@example.com",
                 "organizationUuid": "org-other"},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"

    def test_malformed_token_identity_never_breaks_freshen(self, harness):
        """A schema change feeding a non-string uuid must be ignored, not
        raise — by this point the refreshed credential is already persisted,
        and a crash here would skip the persist bookkeeping and error the
        tick."""
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None, {"uuid": 12345, "email": ["weird"]},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "ok"
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh

    def test_blank_uuid_slot_with_org_conflict_quarantines_not_backfills(
        self, harness,
    ):
        """Org conflict must be checked before the blank-uuid backfill: a
        wrong-org credential is evidence the slot holds the wrong account,
        and backfilling its uuid would stick a foreign identity onto the
        slot (backfill never rewrites a non-empty uuid). Blank-uuid slots
        with a recorded org are what accounts added by older versions look
        like."""
        data = harness.switcher._get_sequence_data()
        data["accounts"]["2"]["uuid"] = ""
        data["accounts"]["2"]["organizationUuid"] = "org-A"
        harness.switcher._write_json(harness.switcher.sequence_file, data)
        harness.switcher._write_account_credentials(
            "2", "b@example.com",
            json.dumps({"claudeAiOauth": {
                "accessToken": "sk-2", "refreshToken": "rt-2", "expiresAt": 0,
            }}),
        )
        fresh = json.dumps({"claudeAiOauth": {
            "accessToken": "sk-2f", "refreshToken": "rt-2f",
            "expiresAt": 99_999_999_999_000,
        }})
        with patch(
            "claude_swap.autoswitch.oauth.try_refresh_oauth_credentials",
            return_value=oauth.RefreshOutcome(
                fresh, None,
                {"uuid": "uuid-real", "email": "z@example.com",
                 "organizationUuid": "org-B"},
            ),
        ):
            status = harness.engine._freshen_target("2", "b@example.com")
        assert status == "identity-conflict"
        # The foreign uuid was NOT backfilled onto the slot.
        assert harness.switcher._get_sequence_data()["accounts"]["2"]["uuid"] == ""
        # The successor generation was still persisted (grant consumed it).
        assert harness.switcher.read_account_credentials(
            "2", "b@example.com"
        ) == fresh


def _model_usage(five_h: float, fable: float) -> dict:
    """Usage with a low 5h/7d but a per-model (Fable) weekly window."""
    return {
        "five_hour": {"pct": five_h},
        "seven_day": {"pct": 0.0},
        "scoped": [{"name": "Fable", "pct": fable}],
    }


class TestModelAwareSwitch:
    """`autoswitch.model` folds a per-model weekly limit into the decision."""

    def _seed(self, temp_home: Path, **kw) -> EngineHarness:
        h = EngineHarness(temp_home, **kw)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_model_maxed_switches_despite_session_headroom(self, temp_home):
        # Active #1: 5h only 5% used, but Fable is maxed → must leave.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),
            "2": _model_usage(5, 30),
            "3": _model_usage(5, 60),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2  # most Fable headroom
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_ref == {"number": 2, "email": "b@example.com"}

    def test_without_model_setting_the_same_usage_holds(self, temp_home):
        # Default engine ignores scoped windows → #1 reads 5% used, no switch.
        h = self._seed(temp_home)
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),
            "2": _model_usage(5, 30),
            "3": _model_usage(5, 60),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_model_headroom_still_gated_by_session_window(self, temp_home):
        # Fable has room on every account, but #1's 5h is maxed → still leaves.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(100, 40),
            "2": _model_usage(10, 40),
            "3": _model_usage(20, 40),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2  # lowest binding (max of 5h, Fable)

    def test_comma_separated_models_switch_on_any(self, temp_home):
        # Configured for "Fable,Opus"; active #1 is fine on Fable but maxed on
        # Opus → must leave. Candidate scoped windows carry both models.
        h = self._seed(temp_home, model="Fable,Opus")

        def usage(five_h, fable, opus):
            return {
                "five_hour": {"pct": five_h},
                "seven_day": {"pct": 0.0},
                "scoped": [
                    {"name": "Fable", "pct": fable},
                    {"name": "Opus", "pct": opus},
                ],
            }

        outcome = h.tick_with_usage({
            "1": usage(5, 20, 100),   # Opus maxed
            "2": usage(5, 20, 30),    # most headroom
            "3": usage(5, 20, 70),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_all_sentinel_binds_every_scoped_window(self, temp_home):
        # "all" needs no names: each account's own scoped windows bind,
        # whatever they're called.
        h = self._seed(temp_home, model="all")
        outcome = h.tick_with_usage({
            "1": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Sonnet", "pct": 100.0}]},
            "2": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Sonnet", "pct": 20.0}]},
            "3": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 0.0},
                  "scoped": [{"name": "Opus", "pct": 60.0}]},
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_missing_scoped_window_is_never_a_target(self, temp_home):
        # 2026-08-02 fleet incident: a candidate reporting healthy 5h/7d but
        # NO Fable scoped window read as 92% free and won the at-limit escape
        # — landing every live session on unverified Fable access. Unknown
        # must lose to a candidate whose Fable status is proven.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),  # active: Fable maxed -> must leave
            "2": {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 8.0}},
            "3": _model_usage(5, 60),   # verified Fable room
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_all_candidates_missing_scoped_window_stays_put(self, temp_home):
        # Nowhere verified to land -> stay, never gamble on unknown access.
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(5, 100),
            "2": {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 8.0}},
            "3": {"five_hour": {"pct": 5.0}, "seven_day": {"pct": 12.0}},
        })
        assert outcome is not TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_dual_exhausted_candidate_recovers_at_its_later_reset(self, temp_home):
        # #2 is blocked on both its 5h (resets 12:00) and Fable (15:00): it's
        # only usable again at the LATER one. #3 recovers later still (20:00),
        # so the all-exhausted wake is #2's Fable reset — which the old
        # earliest-of-any-window scan (12:00) would have jumped early for.
        h = self._seed(temp_home, model="Fable")
        fable_reset = "2026-07-05T15:00:00Z"
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10),
            "2": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T12:00:00Z"},
                "seven_day": {"pct": 0.0},
                "scoped": [
                    {"name": "Fable", "pct": 100.0, "resets_at": fable_reset},
                ],
            },
            "3": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T20:00:00Z"},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": 0.0}],
            },
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at == fable_reset

    def test_unknown_recovery_falls_back_instead_of_oversleeping(self, temp_home):
        # #2 is exhausted with NO reset timestamp — it could recover any
        # moment. Sleeping toward #3's known 20:00 reset would suppress
        # checks for hours, so the wake time must be unprovable (bounded
        # blocked-cadence fallback instead of a reset sleep).
        h = self._seed(temp_home, model="Fable")
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10),
            "2": {
                "five_hour": {"pct": 0.0},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": 100.0}],  # no resets_at
            },
            "3": {
                "five_hour": {"pct": 100.0, "resets_at": "2026-07-05T20:00:00Z"},
                "seven_day": {"pct": 0.0},
                "scoped": [{"name": "Fable", "pct": 0.0}],
            },
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at is None
        assert h.engine._sleep_until_ts is None
        assert h.engine._next_delay(outcome) == NO_RESET_FALLBACK_S

    def test_scoped_only_exhaustion_drives_the_wake_time(self, temp_home):
        # Candidates blocked ONLY by Fable: the wake must come from the scoped
        # reset — the 5h/7d-only scan would find no ≥100% window at all.
        h = self._seed(temp_home, model="Fable")
        fable_reset = "2026-07-06T09:00:00Z"
        blocked = {
            "five_hour": {"pct": 3.0, "resets_at": "2026-07-05T12:00:00Z"},
            "seven_day": {"pct": 0.0},
            "scoped": [{"name": "Fable", "pct": 100.0, "resets_at": fable_reset}],
        }
        outcome = h.tick_with_usage({
            "1": _model_usage(95, 10), "2": blocked, "3": blocked,
        })
        assert outcome is TickOutcome.BLOCKED
        exhausted = next(e for e in h.events if isinstance(e, AllExhaustedEvent))
        assert exhausted.earliest_reset_at == fable_reset

    def test_scoped_binding_window_keeps_active_cadence_tight(self, temp_home):
        # Fable moving at 88% is inside the escalation band: with the model
        # configured the urgent cadence engages, while the 5%-used 5h window
        # alone would just decay the interval.
        kwargs = dict(
            prev_interval_s=poll_policy.MIN_INTERVAL_S,
            prev_usage=_model_usage(5, 84),
            new_usage=_model_usage(5, 88),
            is_active=True,
            threshold=90.0,
            recent_429=False,
            now=1000.0,
            rng=lambda: 0.5,
        )
        _, scoped = poll_policy.plan_after_fetch(models=("Fable",), **kwargs)
        assert scoped == poll_policy.URGENT_INTERVAL_S
        _, unscoped = poll_policy.plan_after_fetch(models=(), **kwargs)
        assert unscoped > poll_policy.MIN_INTERVAL_S  # plain decay

    def test_unmatched_model_name_warns_once(self, temp_home):
        h = self._seed(temp_home, model="Fabel")  # deliberate typo
        usage = {
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        }
        h.tick_with_usage(usage)
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1
        assert "Fabel" in warnings[0].message
        assert warnings[0].to_json()["event"] == "config-warning"
        h.tick_with_usage(usage)
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 1  # once per run, not per tick

    def test_no_false_warning_while_an_account_is_unreadable(self, temp_home):
        h = self._seed(temp_home, model="Fabel")
        h.tick_with_usage({
            "1": _model_usage(5, 10), "2": _model_usage(5, 10), "3": None,
        })
        assert not any(isinstance(e, ConfigWarningEvent) for e in h.events)
        # Once every account reports, the check completes and warns.
        h.tick_with_usage({
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        })
        assert any(isinstance(e, ConfigWarningEvent) for e in h.events)

    def test_matching_name_never_warns(self, temp_home):
        h = self._seed(temp_home, model="Fable")
        h.tick_with_usage({
            "1": _model_usage(5, 10),
            "2": _model_usage(5, 10),
            "3": _model_usage(5, 10),
        })
        assert not any(isinstance(e, ConfigWarningEvent) for e in h.events)


# --- consume-first strategy ----------------------------------------------------

# Weekly-reset instants in ascending order (all valid ISO-8601, absolute).
# The 2024 dates are all far in the FUTURE relative to FakeClock's epoch
# (1_000_000.0 ≈ 1970-01-12); _R_PAST is before it.
_R_PAST = "1970-01-10T00:00:00Z"
_R_SOON = "2024-01-05T00:00:00Z"
_R_LATER = "2024-01-08T00:00:00Z"
_R_LATEST = "2024-01-10T00:00:00Z"


def _usage7(pct5: float, pct7: float, reset7: str | None = None) -> dict:
    """Usage with an explicit 7-day window (utilization + optional reset)."""
    seven: dict = {"pct": pct7}
    if reset7:
        seven["resets_at"] = reset7
    return {"five_hour": {"pct": pct5}, "seven_day": seven}


class TestConsumeFirstStrategy:
    def _harness(self, temp_home: Path) -> EngineHarness:
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        return h

    def test_below_threshold_switches_to_soonest_weekly_reset(self, temp_home):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),    # active resets later
            "2": _usage7(10, 10, _R_SOON),     # soonest -> consume first
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "consume-first"
        assert sw.to_ref == {"number": 2, "email": "b@example.com"}

    def test_stays_when_active_already_resets_soonest(self, temp_home):
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_SOON),     # active is soonest -> stay
            "2": _usage7(10, 10, _R_LATER),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["already-consuming-soonest"]

    def test_over_threshold_prefers_soonest_reset_over_max_headroom(self, temp_home):
        h = self._harness(temp_home)
        # Active over threshold -> must move. #2 has LESS headroom but resets
        # sooner; #3 has more headroom but resets latest. consume-first -> #2.
        outcome = h.tick_with_usage({
            "1": _usage7(95, 20, _R_LATER),
            "2": _usage7(50, 40, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2

    def test_at_limit_lands_healthy_over_sooner_reset(self, temp_home):
        # Incident 2026-07-31: the forced at-limit escape ranked ALL h>0
        # candidates by soonest weekly reset and landed on a 1%-headroom
        # account — instantly at the wall again. An escape must still be
        # able to land anywhere with headroom, but healthy landings
        # (utilization < threshold) sort before unhealthy ones; reset order
        # only ranks within each group.
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(100, 20, _R_LATER),   # active at its limit -> must move
            "2": _usage7(98, 40, _R_SOON),     # sooner reset, but 2% headroom
            "3": _usage7(40, 10, _R_LATEST),   # later reset, 60% headroom
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"

    def test_respects_cooldown(self, temp_home):
        h = self._harness(temp_home)  # default cooldown 300s
        h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert h.active_number() == 2  # switched to soonest
        h.events.clear()
        # Now a sooner account (#3) appears, but we're within cooldown.
        outcome = h.tick_with_usage({
            "2": _usage7(20, 20, _R_LATER),
            "1": _usage7(10, 10, _R_LATEST),
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert "cooldown" in [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def test_cooldown_expires(self, temp_home):
        # Moved from TestDecisionTable when proactive stopped honoring
        # cooldown (2026-07-31 incident): expiry is only observable on the
        # voluntary consume-first trigger now.
        h = self._harness(temp_home)  # default cooldown 300s
        h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert h.active_number() == 2
        h.clock.advance(400)  # past the 300s default cooldown
        h.events.clear()
        outcome = h.tick_with_usage({
            "2": _usage7(20, 20, _R_LATER),
            "1": _usage7(10, 10, _R_LATEST),
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_locked_recheck_stops_concurrent_engine(self, temp_home):
        """The under-lock cooldown recheck in _perform must cover consume-first.

        The tick-level gate reads state *before* the lock, so an engine that
        read state before another engine's switch passes it on a stale
        snapshot; only the recheck inside _perform serializes the two. Drive a
        loser engine through _perform with a stale pre-lock read and a usage
        view that ranks a different target, and assert it backs off instead of
        double-switching inside the cooldown window.
        """
        h = self._harness(temp_home)  # default cooldown 300s
        loser = h._make_engine()
        # Winner: 1 -> 2 (soonest reset), records lastSwitchAt.
        h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert h.active_number() == 2
        h.events.clear()
        # Loser's first (pre-lock) state read predates the winner's write; its
        # usage view ranks #3 soonest, so it reaches _perform for a different
        # target and only the locked recheck can stop it.
        real_read = loser._read_state
        calls: list[bool] = []

        def racing_read() -> dict:
            calls.append(True)
            return {} if len(calls) == 1 else real_read()

        entries = {
            num: _entry_for(value, h.clock.now)
            for num, value in {
                "2": _usage7(20, 20, _R_LATER),
                "1": _usage7(10, 10, _R_LATEST),
                "3": _usage7(10, 10, _R_SOON),
            }.items()
        }
        with patch.object(loser, "_read_state", side_effect=racing_read):
            with patch.object(
                h.switcher, "usage_entries_by_account", return_value=entries
            ):
                outcome = loser.tick()
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2  # no double-switch
        assert "cooldown" in [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def test_reset_unknown_when_active_reset_missing(self, temp_home):
        # Active has no seven_day.resets_at: the strictly-sooner filter skips
        # every candidate, so the strategy is inert — say so, instead of the
        # false "already consuming soonest".
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20),              # no reset timestamp
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["reset-unknown"]

    def test_unreadable_candidates_stay_no_comparison(self, temp_home):
        # Every candidate unreadable this tick is a BLOCKED no-comparison for
        # any strategy — consume-first must not relabel it as a healthy hold.
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": None,
            "3": None,
        })
        assert outcome is TickOutcome.BLOCKED
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["no-comparison"]

    def test_exhausted_candidates_hold_without_false_reset_claim(self, temp_home):
        # All candidates at their limit while the active account is healthy:
        # staying put is right, but the detail must not claim the active
        # account resets first.
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(100, 100, _R_SOON),
            "3": _usage7(100, 100, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        holds = [e for e in h.events if isinstance(e, NoSwitchEvent)]
        assert [e.reason for e in holds] == ["already-consuming-soonest"]
        assert holds[0].detail == "no sooner-resetting account with room to spare"

    def test_single_account_below_threshold_is_no_action(self, temp_home):
        # Exit-code parity with `best`: a healthy below-threshold tick with
        # zero candidates is NO_ACTION/below-threshold, not BLOCKED/
        # no-candidates — cron wrappers key on the documented exit codes.
        h = EngineHarness(temp_home, strategy="consume-first")
        h.seed(1, "a@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({"1": _usage7(20, 20, _R_SOON)})
        assert outcome is TickOutcome.NO_ACTION
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_api_key_only_peers_below_threshold_is_no_action(self, temp_home):
        # Same exit-code parity when the only alternatives are included
        # API-key accounts: they're never consume-first targets (no weekly
        # window), so a healthy below-threshold tick must stay
        # NO_ACTION/below-threshold — not fall through to a false
        # BLOCKED/no-comparison from the empty OAuth ranking.
        h = EngineHarness(
            temp_home, strategy="consume-first", include_api_key_accounts=True
        )
        h.seed(1, "a@example.com")
        h.seed(2, "key@token.local")
        h.make_live("a@example.com", 1)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_SOON),
            "2": "api key",
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_skips_sooner_account_that_is_exhausted(self, temp_home):
        h = self._harness(temp_home)
        # #2 resets soonest but is itself at its limit (no headroom) -> ignored;
        # #3 resets later but has room and is sooner than active -> switch there.
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATEST),   # active resets latest
            "2": _usage7(100, 100, _R_SOON),   # soonest but exhausted
            "3": _usage7(10, 10, _R_LATER),    # sooner than active, has room
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_best_strategy_unaffected_below_threshold(self, temp_home):
        # Regression: default (best) still holds below threshold even when a
        # peer resets sooner — consume-first behavior must be opt-in.
        h = EngineHarness(temp_home)  # strategy defaults to "best"
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.make_live("a@example.com", 1)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert [e.reason for e in h.events if isinstance(e, NoSwitchEvent)] == [
            "below-threshold"
        ]

    def test_candidate_with_past_reset_is_not_selected(self, temp_home):
        # A stale snapshot whose resets_at has already elapsed means the
        # weekly window just rolled over — the LEAST perishable quota. It
        # must rank as unknown, never as "soonest".
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_PAST),     # inverted pick pre-fix
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.to_ref == {"number": 3, "email": "c@example.com"}

    def test_active_past_reset_holds_reset_unknown(self, temp_home):
        # The active account's own reset can be stale too: past == unknown,
        # which lands on the existing reset-unknown hold.
        h = self._harness(temp_home)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_PAST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["reset-unknown"]

    def _two_phase_tick(
        self, h: EngineHarness, stored: dict, fresh: dict
    ) -> tuple[TickOutcome, list[set]]:
        """Drive one tick where stored-snapshot collections serve ``stored``
        and the all-candidates escalation serves ``fresh``.

        These ticks run outside the escalation band (utilization far below
        threshold - ESCALATION_MARGIN_PCT), so the collector never escalates
        on its own and the only all-candidates call a tick can make is the
        consume-first phase-2 refetch — the returned fetch sets prove whether
        it happened.
        """
        fetch_sets: list[set] = []

        def collect(fetch=None, **_kwargs):
            requested = set(fetch or ())
            fetch_sets.append(requested)
            view = fresh if requested == {"1", "2", "3"} else stored
            return {
                num: _entry_for(value, h.clock.now)
                for num, value in view.items()
            }

        with patch.object(
            h.switcher, "usage_entries_by_account", side_effect=collect
        ):
            outcome = h.engine.tick()
        return outcome, fetch_sets

    def test_two_phase_refetch_disqualifies_stale_pick(self, temp_home):
        # The stored snapshot ranks #2; the phase-2 refetch shows it
        # exhausted. The tick must re-decide on the fresh data and hold.
        h = self._harness(temp_home)
        stored = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        fresh = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(100, 100, _R_SOON),   # burned out since the snapshot
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome, fetch_sets = self._two_phase_tick(h, stored, fresh)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["already-consuming-soonest"]
        assert fetch_sets.count({"1", "2", "3"}) == 1  # phase 2 fired once

    def test_two_phase_refetch_confirms_switch(self, temp_home):
        # Fresh data agrees with the stored pick: the switch proceeds through
        # the freshness gate (entries served by phase 2 are age-0).
        h = self._harness(temp_home)
        view = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome, fetch_sets = self._two_phase_tick(h, view, view)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        assert {"1", "2", "3"} in fetch_sets

    def test_two_phase_refetch_reranks_to_fresh_best(self, temp_home):
        # Phase 2 is a full re-rank, not a yes/no check on the provisional
        # target: #2 stays eligible on fresh data, but #3 now resets sooner
        # and must win.
        h = self._harness(temp_home)
        stored = {
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATER),
        }
        fresh = {
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_LATER),    # still sooner than active
            "3": _usage7(10, 10, _R_SOON),     # but #3 is now soonest
        }
        outcome, _ = self._two_phase_tick(h, stored, fresh)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_threshold_crossed_in_phase_two_holds_then_escapes_next_tick(
        self, temp_home
    ):
        # Deliberate design pin: phase 2 never re-classifies the trigger
        # mid-tick. When the fresh active is over the threshold with no
        # strictly-sooner candidate, the tick holds; the NEXT tick classifies
        # at-limit and escapes normally (no freshness gate on escapes).
        h = self._harness(temp_home)
        stored = {
            "1": _usage7(20, 20, _R_LATER),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        fresh = {
            "1": _usage7(100, 20, _R_LATER),   # crossed while the snapshot aged
            "2": _usage7(10, 10, _R_LATEST),   # no longer strictly sooner
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome, fetch_sets = self._two_phase_tick(h, stored, fresh)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert not any(isinstance(e, SwitchEvent) for e in h.events)
        assert {"1", "2", "3"} in fetch_sets
        reasons = [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["already-consuming-soonest"]
        h.events.clear()
        outcome = h.tick_with_usage(fresh)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2
        sw = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert sw.trigger == "at-limit"


def _park_row(
    name: str,
    status: str = "busy",
    state: str | None = "working",
    kind: str = "background",
    pid: int = 100,
) -> ParkSession:
    return ParkSession(
        name=name,
        session_id=f"sid-{name}",
        kind=kind,
        status=status,
        state=state,
        pid=pid,
    )


class FakePark:
    """Scripted stand-in for park.ParkChannel: canned roster, recorded waves.

    ``roster_value`` is returned as-is (None = channel failure); tests mutate
    it between ticks. ``wave_results`` is a FIFO of scripted outcomes; when
    empty, a wave succeeds with every target confirmed.
    """

    def __init__(self, roster: list[ParkSession] | None = None):
        self.roster_value = roster
        self.waves: list[tuple[list[str], str]] = []
        self.wave_results: list[WaveResult] = []
        self.roster_calls = 0

    def roster(self) -> list[ParkSession] | None:
        self.roster_calls += 1
        return self.roster_value

    def send_wave(self, targets: list[str], message: str) -> WaveResult:
        self.waves.append((list(targets), message))
        if self.wave_results:
            return self.wave_results.pop(0)
        return WaveResult(ok=True, delivered=list(targets))


class TestDrainV2:
    """Drain v2 (CON-433): the engine CREATES the park pause instead of
    waiting for one — checkpoint signal to every mid-turn session, machine
    confirmation of fixation (checkpoint receipt primary; a sustained
    not-busy roster streak as the soft fallback — CON-461), swap, verify
    the new account answers, resume signal to the sessions that provably
    parked. Passive transcript silence (v1) stays the path for failover and
    for every failure of the channel; at-limit swaps immediately (CON-486:
    the binding window is at 100%, nothing left to drain);
    ``drain2WaitSeconds=0`` (default) keeps v1 behavior bit-for-bit.
    """

    _PROACTIVE = {"1": _usage(96), "2": _usage(40), "3": _usage(20)}
    _AT_LIMIT = {"1": _usage(100), "2": _usage(40), "3": _usage(20)}

    def _harness(self, temp_home: Path, **kwargs) -> tuple[EngineHarness, FakePark]:
        kwargs.setdefault("drain2_wait_seconds", 180.0)
        kwargs.setdefault("drain_timeout_seconds", 600.0)
        kwargs.setdefault("switch_under_load", True)
        h = EngineHarness(temp_home, **kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        park = FakePark()
        h.engine = h._make_engine(park=park)
        return h, park

    def _reasons(self, h: EngineHarness) -> list[str]:
        return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def _ack(self, h: EngineHarness, name: str, *, at: float | None = None) -> None:
        """Plant a checkpoint receipt the way the wave text tells agents to
        (``touch <backup>/drain2-ack/<name>``), dated ``at`` (default: now)."""
        ack = h.switcher.backup_dir / "drain2-ack" / name
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.touch()
        ts = h.clock.now if at is None else at
        os.utime(ack, (ts, ts))

    # -- scenario (а): the park writes constantly, v2 still reaches a clean swap

    def test_busy_park_signals_and_waits(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [
            _park_row("fix-a", pid=201),
            _park_row("fix-b", pid=202),
            _park_row("Yor", kind="interactive", state=None, pid=203),
            _park_row("parked-c", status="idle", pid=204),
        ]
        _write_transcript(h, age_s=1.0)  # v1 would call this busy and wait
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.trigger == "proactive"
        assert sorted(signal.targets) == ["fix-a", "fix-b"]
        assert signal.delivered == signal.targets
        assert signal.skipped_interactive == 1
        assert self._reasons(h) == ["drain2-wait"]  # never the passive v1 wait
        assert len(park.waves) == 1
        names, message = park.waves[0]
        assert sorted(names) == ["fix-a", "fix-b"]
        assert message.startswith("cswap drain")
        assert "TaskStop" in message and "Заверши ход" in message
        record = h.state()["drain2"]
        assert record["phase"] == "signaled"
        assert sorted(record["signaled"]) == ["fix-a", "fix-b"]
        assert "lastSwitchAt" not in h.state()

    def test_all_fixed_swaps_ready_and_resumes(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        # Both agents follow the wave text: checkpoint receipt, then park.
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        h.clock.advance(60.0)
        _write_transcript(h, age_s=1.0)  # transcripts NEVER go quiet
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b", status="idle"),
        ]
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"] == {
            "outcome": "ready",
            "waitedSeconds": 60,
            "fixed": 2,
            "forced": 0,
            "ackFixed": 2,
            "softFixed": 0,
        }
        assert not [e for e in h.events if isinstance(e, Drain2TimeoutEvent)]
        verify = next(e for e in h.events if isinstance(e, Drain2VerifyEvent))
        assert verify.ok is True and verify.number == "3"
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert sorted(resume.targets) == ["fix-a", "fix-b"]
        assert resume.skipped == ""
        assert park.waves[-1] == (resume.targets, DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()  # episode closed
        # The resume wave follows the switch in the event stream.
        kinds = h.kinds()
        assert kinds.index("switch") < kinds.index("drain2-resume")

    # -- scenario (б): partial fixation at the cap — honest waited/torn count

    def test_partial_fixation_forces_with_honest_count(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")  # fix-a checkpoints; fix-b never does
        h.clock.advance(200.0)  # past the 180s fixation cap
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b"),  # still mid-turn
        ]
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        warn = next(e for e in h.events if isinstance(e, Drain2TimeoutEvent))
        assert warn.human().startswith("WARN")
        assert warn.fixed == ["fix-a"]
        assert warn.forced == ["fix-b"]
        assert warn.acked == ["fix-a"] and warn.soft == []
        assert warn.waited_seconds == 200
        assert warn.max_wait_seconds == 180
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"] == {
            "outcome": "timeout",
            "waitedSeconds": 200,
            "fixed": 1,
            "forced": 1,
            "ackFixed": 1,
            "softFixed": 0,
        }
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        # Only the session that provably parked gets the wave (CON-461);
        # the forced one never stopped — "пауза кончилась" would be noise.
        assert resume.targets == ["fix-a"]
        assert resume.unacked == ["fix-b"]
        kinds = h.kinds()
        assert kinds.index("drain2-timeout") < kinds.index("switch")

    # -- scenario (в): channel failure falls back to passive v1 drain

    def test_unreadable_roster_falls_back_to_v1(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = None  # `claude agents --json` failed
        _write_transcript(h, age_s=1.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        unavailable = next(
            e for e in h.events if isinstance(e, Drain2UnavailableEvent)
        )
        assert "roster" in unavailable.reason
        assert self._reasons(h) == ["drain-wait"]  # the passive v1 wait took over
        assert "drain" in h.state() and "drain2" not in h.state()
        assert park.waves == []

    def test_failed_stop_wave_falls_back_to_v1_with_backoff(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        park.wave_results = [WaveResult(ok=False, detail="claude CLI not found")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert [e.reason for e in h.events if isinstance(e, Drain2UnavailableEvent)]
        assert self._reasons(h) == ["drain-wait"]
        # The failure backs the channel off in-process: the next tick goes
        # straight to v1 without a second herald attempt or a second WARN.
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert len(park.waves) == 1
        assert (
            len([e for e in h.events if isinstance(e, Drain2UnavailableEvent)]) == 1
        )
        assert self._reasons(h) == ["drain-wait", "drain-wait"]

    def test_default_off_keeps_v1_bit_for_bit(self, temp_home):
        h, park = self._harness(temp_home, drain2_wait_seconds=0.0)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert AutoSwitchSettings().drain2_wait_seconds == 0.0
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["drain-wait"]
        assert park.roster_calls == 0 and park.waves == []
        assert not [
            e
            for e in h.events
            if isinstance(
                e,
                (
                    Drain2SignalEvent,
                    Drain2TimeoutEvent,
                    Drain2ResumeEvent,
                    Drain2UnavailableEvent,
                ),
            )
        ]

    def test_at_limit_skips_v2_and_swaps_immediately(self, temp_home):
        # The checkpoint pause is for the proactive forewarning only: at the
        # hard limit calls are already failing, so orchestrating a pause
        # spends minutes the park doesn't have — and the passive v1 wait is
        # skipped too (CON-486): the binding window is at 100%, so there is
        # no cache left for any wait to protect. The swap lands immediately.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        outcome = h.tick_with_usage(self._AT_LIMIT)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert "drain" not in switch.to_json()
        assert "drain-wait" not in self._reasons(h)
        assert park.waves == [] and park.roster_calls == 0

    def test_threshold_pierced_inside_cooldown_starts_drain2(self, temp_home):
        # CON-485 live episode (cswap-auto.log 2026-08-14T23:20-23:54Z):
        # under consume-first with cooldownSeconds=7200 the panel showed
        # "no-switch cooldown" every tick while the active 5h window climbed
        # 48%->81% — that hold is the voluntary-rotation debounce and is
        # correct. The moment the window pierced the 90% threshold (93% at
        # 23:44:35Z) the trigger became proactive, and the drain2 wave must
        # start on that very tick, cooldown notwithstanding: past the
        # threshold the swap is an escape, not an optimization.
        h, park = self._harness(
            temp_home,
            strategy="consume-first",
            cooldown_seconds=7200.0,
            threshold=90.0,
        )
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        # Last swap 30 minutes ago — deep inside the two-hour cooldown.
        h.engine._mutate_state(
            lambda s: s.update(lastSwitchAt=h.clock() - 1800)
        )
        _write_transcript(h, age_s=1.0)
        below = {
            "1": _usage7(81, 35, _R_LATER),
            "2": _usage7(20, 16, _R_SOON),
            "3": _usage7(20, 4, _R_LATEST),
        }
        assert h.tick_with_usage(below) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["cooldown"]  # the debounce, by design
        assert not [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        h.events.clear()
        h.clock.advance(35.0)
        _write_transcript(h, age_s=1.0)
        pierced = {
            "1": _usage7(93, 37, _R_LATER),
            "2": _usage7(20, 16, _R_SOON),
            "3": _usage7(20, 4, _R_LATEST),
        }
        assert h.tick_with_usage(pierced) is TickOutcome.NO_ACTION
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.trigger == "proactive"
        assert self._reasons(h) == ["drain2-wait"]  # never "cooldown"
        # Fixation completes -> the swap itself must also land inside the
        # cooldown window, labeled proactive.
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        h.clock.advance(60.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b", status="idle"),
        ]
        h.events.clear()
        assert h.tick_with_usage(pierced) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert h.active_number() == 2  # soonest weekly reset wins

    # -- mid-episode arrivals, restarts, verify failure, dry-run

    def test_top_up_wave_for_session_appearing_mid_episode(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-c"),  # woke/spawned mid-episode
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        top_up = next(
            e
            for e in h.events
            if isinstance(e, Drain2SignalEvent) and e.top_up
        )
        assert top_up.targets == ["fix-c"]
        names, message = park.waves[-1]
        assert names == ["fix-c"] and message.startswith("cswap drain")
        assert sorted(h.state()["drain2"]["signaled"]) == ["fix-a", "fix-c"]
        self._ack(h, "fix-c")  # the newcomer checkpoints too
        h.clock.advance(30.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-c", status="idle"),
        ]
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"]["fixed"] == 2
        assert switch.to_json()["drain2"]["ackFixed"] == 2

    def test_resume_survives_engine_restart(self, temp_home):
        # Daemon died between the swap and the resume wave: a fresh engine
        # finds the "swapped" episode in the state file and finishes it.
        h, park = self._harness(temp_home)
        state_file = h.switcher.backup_dir / "autoswitch_state.json"
        state_file.write_text(json.dumps({
            "schemaVersion": 1,
            "drain2": {
                "phase": "swapped",
                "trigger": "proactive",
                "startedAt": h.clock.now - 100.0,
                "updatedAt": h.clock.now - 30.0,
                "swappedAt": h.clock.now - 30.0,
                "to": "3",
                "verifyAttempts": 0,
                "signaled": {"fix-a": {"sessionId": "sid-fix-a"}},
                "frozen": ["fix-a"],
            },
        }))
        park.roster_value = [_park_row("fix-a", status="idle")]
        outcome = h.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(20),
        })
        assert outcome is TickOutcome.NO_ACTION  # normal below-threshold tick
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]
        assert park.waves == [(["fix-a"], DRAIN2_RESUME_MESSAGE)]
        assert "drain2" not in h.state()

    def test_stale_swapped_episode_skips_the_resume_wave(self, temp_home):
        # Past the self-rescue window the frozen sessions already resumed
        # themselves (the STOP wave says so) — waking them again would only
        # burn turns.
        h, park = self._harness(temp_home)
        state_file = h.switcher.backup_dir / "autoswitch_state.json"
        state_file.write_text(json.dumps({
            "schemaVersion": 1,
            "drain2": {
                "phase": "swapped",
                "trigger": "proactive",
                "startedAt": h.clock.now - DRAIN2_SELF_RESCUE_S - 200.0,
                "updatedAt": h.clock.now - DRAIN2_SELF_RESCUE_S - 100.0,
                "swappedAt": h.clock.now - DRAIN2_SELF_RESCUE_S - 100.0,
                "to": "3",
                "signaled": {"fix-a": {"sessionId": "sid-fix-a"}},
                "frozen": ["fix-a"],
            },
        }))
        outcome = h.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(20),
        })
        assert outcome is TickOutcome.NO_ACTION
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.skipped != "" and resume.targets == []
        assert park.waves == []
        assert "drain2" not in h.state()

    def test_verify_failure_never_freezes_the_park(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)

        def collector(usage):
            full = {
                num: _entry_for(value, h.clock.now)
                for num, value in usage.items()
            }

            def collect(fetch=frozenset(), scheduled=True):
                # The verify fetch is the only single-account fetch that can
                # run with account 3 already active: report it unreadable.
                if h.active_number() == 3 and set(fetch) == {"3"}:
                    return {"3": UsageEntry()}
                return full

            return collect

        with patch.object(
            h.switcher,
            "usage_entries_by_account",
            side_effect=collector(self._PROACTIVE),
        ):
            assert h.engine.tick() is TickOutcome.NO_ACTION  # STOP wave out
            self._ack(h, "fix-a")
            h.clock.advance(30.0)
            park.roster_value = [_park_row("fix-a", status="idle")]
            outcome = h.engine.tick()
        assert outcome is TickOutcome.SWITCHED
        verify1 = next(e for e in h.events if isinstance(e, Drain2VerifyEvent))
        assert verify1.ok is False and verify1.attempt == 1
        assert not [e for e in h.events if isinstance(e, Drain2ResumeEvent)]
        assert h.state()["drain2"]["phase"] == "swapped"
        # Next tick: the second (final) verify attempt fails too — the park
        # is resumed anyway, with the failure on the record.
        h.clock.advance(30.0)
        below = {"3": _usage(20), "1": _usage(50), "2": _usage(10)}
        with patch.object(
            h.switcher,
            "usage_entries_by_account",
            side_effect=collector(below),
        ):
            h.engine.tick()
        verifies = [e for e in h.events if isinstance(e, Drain2VerifyEvent)]
        assert [v.attempt for v in verifies] == [1, 2]
        assert all(v.ok is False for v in verifies)
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]
        assert park.waves[-1] == (["fix-a"], DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()

    def test_episode_survives_failed_switch_attempt(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        with patch.object(
            h.engine, "_freshen_target", return_value="transient"
        ):
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.ERROR
        assert h.state()["drain2"]["phase"] == "signaled"
        h.clock.advance(30.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"]["outcome"] == "ready"
        assert switch.to_json()["drain2"]["waitedSeconds"] == 60

    def test_dry_run_sends_no_waves_and_writes_no_state(self, temp_home):
        h, park = self._harness(temp_home)
        h.engine = h._make_engine(dry_run=True, park=park)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.dry_run is True
        assert park.waves == []
        assert h.state() == {}
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.dry_run is True
        assert switch.to_json()["drain2"]["outcome"] == "ready"
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.skipped == "dry-run"
        assert park.waves == []
        assert h.state() == {}
        assert h.active_number() == 1

    def test_drain2_setting_spec(self):
        spec = SETTING_SPECS["autoswitch.drain2WaitSeconds"]
        assert spec.field == "drain2_wait_seconds"
        assert spec.kind == "float"
        assert spec.lo == 0.0
        assert spec.hi == 3600.0
        assert AutoSwitchSettings().drain2_wait_seconds == 0.0

    # -- review r1: an abandoned signaled episode must still resume the park

    def test_foreign_switch_closes_the_episode_with_a_resume(self, temp_home):
        # Review r1 finding 2 (scenario A): the interactive sessions the wave
        # never signals can burn the account to its hard limit while the park
        # is frozen; the at-limit escape then swaps through the v1 path with
        # no drain2 label. The engine KNOWS the episode is dead (a switch
        # landed past it) — the frozen sessions must be woken by machine,
        # not by the STOP text's self-rescue prose.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=400.0)  # transcript-quiet: v1 gate opens
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b", status="idle"),
        ]
        outcome = h.tick_with_usage(self._AT_LIMIT)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert "drain2" not in switch.to_json()
        # Next tick: the engine reconciles the orphaned episode — resume
        # wave to the frozen sessions, episode closed.
        h.clock.advance(30.0)
        below = {"3": _usage(20), "1": _usage(100), "2": _usage(40)}
        assert h.tick_with_usage(below) is TickOutcome.NO_ACTION
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert sorted(resume.targets) == ["fix-a", "fix-b"]
        assert park.waves[-1] == (resume.targets, DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()

    def test_stale_signaled_episode_resumes_before_it_rots(self, temp_home):
        # Review r1 finding 2 (scenario B): the forcing condition went away
        # (weekly reset dropped utilization below the threshold) and the gate
        # stopped observing the episode. The stale episode must be closed
        # with a machine resume, not abandoned to the self-rescue prose.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(DRAIN_STALE_GAP_S + 30.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        below = {"1": _usage(50), "2": _usage(10), "3": _usage(20)}
        assert h.tick_with_usage(below) is TickOutcome.NO_ACTION
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]
        assert park.waves[-1] == (["fix-a"], DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()

    def test_dead_episode_never_resurrects_into_a_new_swap(self, temp_home):
        # Review r1 finding 3: a rotten signaled record must not be adopted
        # by a later episode's mark-swapped (fake verify/resume for sessions
        # long gone). The reconcile closes it first — and with its sessions
        # absent from the roster there is nobody to wake.
        h, park = self._harness(temp_home)
        state_file = h.switcher.backup_dir / "autoswitch_state.json"
        state_file.write_text(json.dumps({
            "schemaVersion": 1,
            "drain2": {
                "phase": "signaled",
                "trigger": "proactive",
                "startedAt": h.clock.now - 5000.0,
                "updatedAt": h.clock.now - DRAIN_STALE_GAP_S - 1.0,
                "signaled": {"long-gone": {"sessionId": "sid-old"}},
                "frozen": ["long-gone"],
            },
        }))
        park.roster_value = []  # nobody mid-turn, old sessions exited
        _write_transcript(h, age_s=1.0)
        outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == [] and resume.skipped != ""
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"] == {
            "outcome": "ready",
            "waitedSeconds": 0,
            "fixed": 0,
            "forced": 0,
            "ackFixed": 0,
            "softFixed": 0,
        }
        # No wave ever targeted the dead episode's session.
        assert all("long-gone" not in names for names, _ in park.waves)
        assert not [e for e in h.events if isinstance(e, Drain2VerifyEvent) and e.number != "3"]
        assert "drain2" not in h.state()

    # -- CON-451: the first live episode (14-08) turned its defect catalog
    # into machine guarantees — forced sessions get the resume too, the
    # fixation judge sees through background-task "busy", the wave text
    # prescribes ONE self-waking wait channel.

    def test_forced_busy_session_is_resumed_after_the_swap(self, temp_home):
        # Episode 14-08, class 1 (CON-451), narrowed by CON-461: sessions
        # froze AFTER the swap landed — a pre-swap snapshot never reaches
        # them. The resume targets are re-derived live on every wave, from
        # the receipts on disk: a late freezer that acks after the swap is
        # picked up by the retry. (A late freezer that never acks self-wakes
        # via its marker watch — a wave to it would be guesswork noise.)
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(200.0)  # past the 180s fixation cap
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b"),  # forced: still busy when the swap lands
        ]
        park.wave_results = [
            WaveResult(ok=False, detail="herald timeout 120s")  # resume fails
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]  # the mid-turn worker is not one
        assert h.state()["drain2"]["phase"] == "swapped"  # retry pending
        # fix-b checkpoints only now — after the swap, like the episode's
        # six: the next tick's re-derivation sees the fresh receipt.
        self._ack(h, "fix-b")
        h.clock.advance(30.0)
        below = {"3": _usage(20), "1": _usage(50), "2": _usage(10)}
        assert h.tick_with_usage(below) is TickOutcome.NO_ACTION
        resumes = [e for e in h.events if isinstance(e, Drain2ResumeEvent)]
        assert sorted(resumes[-1].targets) == ["fix-a", "fix-b"]
        assert sorted(park.waves[-1][0]) == ["fix-a", "fix-b"]
        assert "drain2" not in h.state()

    def test_finished_session_is_never_rewoken(self, temp_home):
        # fix-age-246 finished its whole session mid-episode (state=done).
        # A resume message to a completed background session would respawn
        # it just to say "I'm done" — skip done/failed/stopped and missing
        # rows, wake only open tasks.
        h, park = self._harness(temp_home)
        park.roster_value = [
            _park_row("fix-a"),
            _park_row("fix-b"),
            _park_row("fix-c"),
        ]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        # All three acked — even so, only the open task is ever re-woken.
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        self._ack(h, "fix-c")
        h.clock.advance(200.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b", status="idle", state="done"),
            # fix-c exited: no roster row at all
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]
        assert park.waves[-1] == (["fix-a"], DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()

    def test_resume_retries_names_the_herald_failed(self, temp_home):
        # A name the herald explicitly failed stays pending: the episode
        # survives the tick and the next one re-waves exactly that name.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        h.clock.advance(30.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b", status="idle"),
        ]
        park.wave_results = [
            WaveResult(ok=True, delivered=["fix-a"], failed={"fix-b": "boom"})
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        assert h.state()["drain2"]["phase"] == "swapped"  # fix-b still pending
        h.clock.advance(30.0)
        below = {"3": _usage(20), "1": _usage(50), "2": _usage(10)}
        assert h.tick_with_usage(below) is TickOutcome.NO_ACTION
        resumes = [e for e in h.events if isinstance(e, Drain2ResumeEvent)]
        assert resumes[-1].targets == ["fix-b"]
        assert park.waves[-1] == (["fix-b"], DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()

    def test_resume_channel_failure_retries_next_tick(self, temp_home):
        # A failed resume wave used to close the episode anyway, leaving the
        # park frozen on the self-rescue prose alone. Now the episode
        # survives (within the self-rescue window) and the next tick retries.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        park.wave_results = [WaveResult(ok=False, detail="herald timeout 120s")]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        assert h.state()["drain2"]["phase"] == "swapped"
        h.clock.advance(30.0)
        below = {"3": _usage(20), "1": _usage(50), "2": _usage(10)}
        assert h.tick_with_usage(below) is TickOutcome.NO_ACTION
        resumes = [e for e in h.events if isinstance(e, Drain2ResumeEvent)]
        assert resumes[-1].targets == ["fix-a"] and resumes[-1].skipped == ""
        assert park.waves[-1] == (["fix-a"], DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()
        # And only one verify ran: retries re-wave, they don't re-verify.
        assert len([e for e in h.events if isinstance(e, Drain2VerifyEvent)]) == 1

    def test_ack_receipt_counts_a_busy_session_fixed(self, temp_home):
        # Episode 14-08, the un-named defect: 4 of 6 "forced" sessions had
        # checkpointed before the cap, but their own background fallback
        # timers kept them status=busy in the roster — the judge could never
        # count them fixed. The wave now prescribes an ack receipt file; a
        # busy session with a fresh receipt is fixed.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        ack = h.switcher.backup_dir / "drain2-ack" / "fix-a"
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.touch()
        os.utime(ack, (h.clock.now + 5.0, h.clock.now + 5.0))
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        # fix-a holds its background watcher: still busy in the roster.
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"] == {
            "outcome": "ready",
            "waitedSeconds": 30,
            "fixed": 1,
            "forced": 0,
            "ackFixed": 1,
            "softFixed": 0,
        }
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]  # busy+working is still woken
        assert "drain2" not in h.state()

    def test_stale_ack_is_cleared_at_signal_and_ignored_by_mtime(self, temp_home):
        # A receipt left over from a previous episode must not count: the
        # signal wave clears receipts for its targets, and the judge ignores
        # any file older than the episode start.
        h, park = self._harness(temp_home)
        ack = h.switcher.backup_dir / "drain2-ack" / "fix-a"
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.touch()
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert not ack.exists()  # cleared when the episode signaled fix-a
        ack.touch()
        os.utime(ack, (h.clock.now - 100.0, h.clock.now - 100.0))  # pre-episode
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._reasons(h)[-1] == "drain2-wait"

    def test_stop_message_carries_the_one_channel_protocol(self, temp_home):
        # Class 2+3: the wave text must name async Task/Agent subagents (the
        # "лёгкий разовый вызов" loophole chore-ops-340 walked through), and
        # prescribe ONE self-waking wait — a run_in_background watch on the
        # swap marker, with the signal epoch baked in so a late freezer sees
        # "the swap already happened" instead of hanging.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        signal_epoch = int(h.clock.now)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        _, message = park.waves[0]
        marker = h.switcher.backup_dir / "drain2-last-switch"
        ack_dir = h.switcher.backup_dir / "drain2-ack"
        assert str(marker) in message
        assert str(ack_dir) in message
        assert str(signal_epoch) in message
        assert "TaskStop" in message
        assert "Task/Agent" in message
        assert "run_in_background" in message
        for placeholder in ("{marker}", "{ack_dir}", "{signal_epoch}"):
            assert placeholder in DRAIN2_STOP_MESSAGE  # the template
            assert placeholder not in message  # the delivered text

    def test_stop_message_carries_machine_stop_protocol(self, temp_home):
        # CON-486: "останови все вложенные прогоны" as prose did not stop a
        # grandchild reviewer spawned by a subagent — the parent stopped the
        # one builder it remembered, receipted, and the grandchild burned
        # the window to 100%. The wave must prescribe the machine loop:
        # TaskList, TaskStop each listed run, receipt ONLY when a re-run
        # TaskList shows no token-spending runs left.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        _, message = park.waves[0]
        assert "TaskList" in message
        assert "TaskStop" in message
        assert "ТОЛЬКО когда" in message  # the receipt gate on the empty list
        # the stop loop comes first, the receipt is gated behind it
        assert message.index("TaskStop") < message.index("ТОЛЬКО когда")

    def test_switch_writes_the_swap_marker_for_agent_watchers(self, temp_home):
        # Every real switch stamps the marker the wave text tells agents to
        # watch: an integer epoch, comparable with `[ "$(cat ...)" -ge N ]`.
        h, park = self._harness(temp_home)
        park.roster_value = []  # nobody mid-turn: ready path, instant swap
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        marker = h.switcher.backup_dir / "drain2-last-switch"
        assert marker.exists()
        assert marker.read_text() == f"{int(h.clock.now)}\n"

    def test_new_episode_waits_for_a_pending_resume(self, temp_home):
        # Review r1 finding 1: cascading exhaustion (the new account is over
        # the threshold too) used to start a fresh episode right over a
        # swapped record whose resume was still pending — destroying the
        # ``resumed`` bookkeeping without an event, so the pending session
        # would never be re-waved. The gate must hold until ``_drain2_finish``
        # closes the old episode (retry success, or the self-rescue cap).
        h, park = self._harness(temp_home)
        state_file = h.switcher.backup_dir / "autoswitch_state.json"
        state_file.write_text(json.dumps({
            "schemaVersion": 1,
            "drain2": {
                "phase": "swapped",
                "trigger": "proactive",
                "startedAt": h.clock.now - 100.0,
                "updatedAt": h.clock.now - 30.0,
                "swappedAt": h.clock.now - 30.0,
                "to": "3",
                "verified": True,
                "signaled": {
                    "fix-a": {"sessionId": "sid-fix-a"},
                    "fix-b": {"sessionId": "sid-fix-b"},
                },
                "acked": ["fix-a", "fix-b"],
                "resumed": ["fix-a"],
            },
        }))
        park.roster_value = [
            _park_row("fix-b", status="idle"),  # pending from the old episode
            _park_row("fix-c"),  # new mid-turn session: a fresh episode's target
        ]
        park.wave_results = [
            WaveResult(ok=False, detail="herald down")  # the finish retry fails
        ]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        # No STOP wave went out and the old episode survived intact.
        assert not [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        record = h.state()["drain2"]
        assert record["phase"] == "swapped"
        assert record["resumed"] == ["fix-a"]
        assert self._reasons(h) == ["drain2-wait"]
        # Next tick the herald recovers: the pending name is re-waved, the
        # old episode closes, and only then does a new episode signal fix-c.
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        resumes = [e for e in h.events if isinstance(e, Drain2ResumeEvent)]
        assert resumes[-1].targets == ["fix-b"]
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.targets == ["fix-c"]
        assert h.state()["drain2"]["phase"] == "signaled"

    def test_unconfirmed_resume_delivery_closes_the_episode(self, temp_home):
        # Review r1 nit: ``ok=True, delivered=None`` (the herald ran but its
        # report was unparseable) counts every target as sent — the fixation
        # loop's law: never respam on a fuzzy channel.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        park.wave_results = [
            WaveResult(ok=True, delivered=None, detail="no report object")
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        assert "drain2" not in h.state()
        assert (
            len([e for e in h.events if isinstance(e, Drain2ResumeEvent)]) == 1
        )

    def test_ack_name_sanitizer_rejects_hostile_names(self):
        # Review r1 nit: a NUL byte survives ``Path(name).name == name`` and
        # then raises ValueError (not OSError) from stat/unlink — the
        # sanitizer must reject it outright.
        ok = AutoSwitchEngine._drain2_ack_name_ok
        assert ok("fix-a") is True
        for hostile in ("", ".", "..", ".hidden", "a/b", "fix\x00a"):
            assert ok(hostile) is False, hostile

    def test_reconcile_wakes_busy_working_sessions_too(self, temp_home):
        # An abandoned episode's reconcile must use the same live filter:
        # a session frozen behind a background task looks busy but still
        # holds its open task — it gets the wave like everyone else.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=400.0)  # transcript-quiet: v1 gate opens
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b"),  # busy behind its own background watcher
        ]
        assert h.tick_with_usage(self._AT_LIMIT) is TickOutcome.SWITCHED
        h.clock.advance(30.0)
        below = {"3": _usage(20), "1": _usage(100), "2": _usage(40)}
        assert h.tick_with_usage(below) is TickOutcome.NO_ACTION
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert sorted(resume.targets) == ["fix-a", "fix-b"]
        assert "drain2" not in h.state()

    # -- CON-461: fixation is the agent's receipt, not one roster poll.
    # Episode 14-08 16:41:48–16:44:27Z: fix-age-267 wrote 53 transcript
    # records without a pause, but one daemon poll landed in the sub-second
    # turn-boundary gap between two tool calls — the roster answered "not
    # busy", the judge called it fixed, the account switched under its live
    # turn, and the resume wave then told the never-paused agent "пауза
    # кончилась". The receipt (wave step 3) is the fixation proof; the
    # roster alone fixes only via a sustained not-busy STREAK, marked soft.

    def test_working_session_without_receipt_is_never_fixed_by_one_poll(
        self, temp_home
    ):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-age-267")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        # The poll lands between two tool calls: not busy for a moment.
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-age-267", status="idle")]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert h.active_number() == 1  # never switched off a single blip
        assert not [e for e in h.events if isinstance(e, SwitchEvent)]
        # Next poll it is visibly mid-turn again: the streak resets.
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-age-267")]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        # Two more blips after the reset (streak 2 of 3): still holding.
        for _ in range(2):
            h.clock.advance(30.0)
            _write_transcript(h, age_s=1.0)
            park.roster_value = [_park_row("fix-age-267", status="idle")]
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        # The cap lands with the agent visibly mid-turn and unproven: it is
        # FORCED with the proof breakdown saying so — never "fixed" — and
        # the resume wave has nobody to wake (no receipt = nobody parked).
        h.clock.advance(60.0)  # 180s: the fixation cap
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-age-267")]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        warn = next(e for e in h.events if isinstance(e, Drain2TimeoutEvent))
        assert warn.forced == ["fix-age-267"]
        assert warn.fixed == [] and warn.acked == [] and warn.soft == []
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        drain2 = switch.to_json()["drain2"]
        assert drain2["fixed"] == 0 and drain2["forced"] == 1
        assert drain2["ackFixed"] == 0 and drain2["softFixed"] == 0
        assert not any(
            msg == DRAIN2_RESUME_MESSAGE and "fix-age-267" in names
            for names, msg in park.waves
        )

    def test_roster_streak_soft_fixes_a_silent_session_without_receipt(
        self, temp_home, caplog
    ):
        # No receipt (the agent died, or never ran the wave's step 3): the
        # roster stays the coarse fallback, but only a STREAK counts — 3
        # consecutive not-busy polls at the engine's ≥30s cadence is a
        # ≥60s sustained turn boundary, which no between-tools blip (доли
        # секунды) can fake. The fixation is marked soft in the event
        # fields and said out loud in the log.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        park.roster_value = [_park_row("fix-a", status="idle")]
        with caplog.at_level(logging.INFO, logger="claude-swap"):
            for _ in range(2):
                h.clock.advance(30.0)
                _write_transcript(h, age_s=1.0)
                assert (
                    h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
                )
            h.clock.advance(30.0)
            _write_transcript(h, age_s=1.0)
            outcome = h.tick_with_usage(self._PROACTIVE)
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"] == {
            "outcome": "ready",
            "waitedSeconds": 90,
            "fixed": 1,
            "forced": 0,
            "ackFixed": 0,
            "softFixed": 1,
        }
        assert "soft" in caplog.text and "fix-a" in caplog.text
        # A soft fixation is not a parked agent: nobody left a receipt, so
        # the resume wave has no targets — a genuinely frozen no-receipt
        # session self-wakes via its marker watch / the self-rescue clause.
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == []
        assert not any(msg == DRAIN2_RESUME_MESSAGE for _, msg in park.waves)

    def test_stale_receipt_plus_roster_blip_never_fixes(self, temp_home):
        # The acceptance pair from CON-461: a receipt of a PREVIOUS episode
        # (mtime before this episode's start) must not count — and with the
        # receipt path closed, one roster blip must not fix either. On the
        # pre-fix code the roster branch answered first, so exactly this
        # combination (stale receipt + momentary idle) sailed through.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        # Planted AFTER the signal wave's own cleanup, dated BEFORE the
        # episode start: a leftover from a past episode.
        self._ack(h, "fix-a", at=h.clock.now - 100.0)
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]  # the blip
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert not [e for e in h.events if isinstance(e, SwitchEvent)]
        # A receipt of THIS episode fixes on the very next poll — even
        # with the session reading busy behind its own background watch.
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a")]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        drain2 = switch.to_json()["drain2"]
        assert drain2["ackFixed"] == 1 and drain2["softFixed"] == 0

    def test_no_swap_candidate_releases_the_park(self, temp_home):
        # Live hole 14-08 17:10–17:13Z (found by Yor, CON-461 add-on): the
        # gate reached its swap decision, but every candidate was exhausted
        # — and the resume, which only ever followed a swap, went to
        # NOBODY. The episode stayed open and the park stood parked until a
        # manual wake. An episode that cannot swap must machine-release the
        # parked sessions with an honest "no swap is coming" wave and close.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")  # fix-a parks; fix-b keeps working
        h.clock.advance(200.0)  # past the 180s cap
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b"),
        ]
        # Every candidate hit 100% during the pause: nobody to switch to.
        exhausted = {"1": _usage(96), "2": _usage(100), "3": _usage(100)}
        assert h.tick_with_usage(exhausted) is TickOutcome.BLOCKED
        assert h.active_number() == 1  # no switch happened
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]  # the parked one is released
        assert resume.unacked == ["fix-b"]
        assert "released without a swap" in resume.reason
        names, message = park.waves[-1]
        assert names == ["fix-a"]
        assert "своп НЕ случился" in message
        assert "drain2" not in h.state()  # episode closed
        # And the next ticks do NOT re-signal a fresh pause into the
        # just-released park while there is still nobody to switch to.
        # Since CON-572 the guard is structural — candidates are judged
        # before any wave — so no release backoff is armed for the
        # no-candidate case (it would stall the orderly episode a fresh
        # ``cswap add`` candidate deserves; see
        # test_candidate_added_after_release_starts_a_fresh_episode).
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        h.events.clear()
        waves_before = len(park.waves)
        h.tick_with_usage(exhausted)
        assert not [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        assert len(park.waves) == waves_before
        assert "drain2ReleaseUntil" not in h.state()
        # A fresh engine must not re-signal a pause either.
        h.engine = h._make_engine(park=park)
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        h.events.clear()
        h.tick_with_usage(exhausted)
        assert not [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        assert len(park.waves) == waves_before

    def test_no_qualifying_candidate_releases_the_park_too(self, temp_home):
        # Same hole, the exact reason string of the live log: candidates
        # exist but none qualifies (all above the threshold, none at 100%).
        # A retryable block still must not keep the park frozen — release
        # now; if a candidate recovers while the account is still over the
        # threshold, a fresh episode re-signals after the release backoff.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        crowded = {"1": _usage(96), "2": _usage(97), "3": _usage(98)}
        assert h.tick_with_usage(crowded) is TickOutcome.BLOCKED
        reasons = self._reasons(h)
        assert reasons[-1] == "no-qualifying-candidate"
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]
        assert park.waves[-1][0] == ["fix-a"]
        assert "drain2" not in h.state()

    # -- CON-572 (postmortem 15-08): candidates are judged BEFORE any wave
    # or passive wait, and no drain artifact outlives its episode.

    def test_no_candidate_alerts_instead_of_waving(self, temp_home):
        # CON-572 class A (17:16:56Z and 17:29:24Z): the engine paused six
        # working sessions with a stop wave and only THEN judged candidates
        # — found nobody, resumed the park 85 seconds later; twice in 13
        # minutes. The wave belongs to a switch that can actually happen:
        # with no qualifying candidate the park is on its last account, and
        # the engine's job is one deduped human alert, not a pause.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        crowded = {"1": _usage(96), "2": _usage(97), "3": _usage(98)}
        outcome = h.tick_with_usage(crowded)
        assert park.waves == []  # the park was never paused
        assert outcome is TickOutcome.BLOCKED
        assert not [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        assert "drain2" not in h.state() and "drain" not in h.state()
        alerts = [e for e in h.events if e.kind == "last-account"]
        assert len(alerts) == 1
        assert alerts[0].to_json()["reason"]  # says why nobody qualifies
        # Same condition on later ticks: the alert does not repeat.
        for _ in range(2):
            h.clock.advance(30.0)
            _write_transcript(h, age_s=1.0)
            assert h.tick_with_usage(crowded) is TickOutcome.BLOCKED
        assert len([e for e in h.events if e.kind == "last-account"]) == 1
        assert park.waves == []
        # A restarted engine shares the dedup through the state file.
        h.engine = h._make_engine(park=park)
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(crowded) is TickOutcome.BLOCKED
        assert len([e for e in h.events if e.kind == "last-account"]) == 1
        # A candidate appearing re-arms the alert and gets the normal wave.
        h.seed(4, "d@example.com")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        with_new = {**crowded, "4": _usage(5)}
        assert h.tick_with_usage(with_new) is TickOutcome.NO_ACTION
        assert [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        assert "lastAccountAlertedAt" not in h.state()

    def test_drain2_release_resets_the_whole_drain_state(self, temp_home):
        # CON-572 class B, root cause: the 15-08 episode left the passive
        # v1 drain record alive across two drain2 releases; its "already
        # waited 1648s" timeout later authorized a forced swap into a
        # WORKING park. A release ends the switch intent — every drain
        # artifact dies with it, and later no-candidate ticks must not
        # quietly accumulate a new passive wait in the background.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        crowded = {"1": _usage(96), "2": _usage(97), "3": _usage(98)}
        assert h.tick_with_usage(crowded) is TickOutcome.BLOCKED  # released
        assert "drain2" not in h.state() and "drain" not in h.state()
        # The park works on; ticks keep finding nobody. Neither wave nor
        # wait may build up while there is nobody to switch to.
        park.roster_value = [_park_row("fix-a")]
        waves_before = len(park.waves)
        for _ in range(4):
            h.clock.advance(300.0)
            _write_transcript(h, age_s=1.0)
            assert h.tick_with_usage(crowded) is TickOutcome.BLOCKED
            assert "drain" not in h.state()
            assert "drain2" not in h.state()
        assert len(park.waves) == waves_before  # no re-signal, no re-pause

    def test_candidate_added_after_release_starts_a_fresh_episode(
        self, temp_home
    ):
        # CON-572 class B, the money shot (17:34:05Z): ``cswap add`` put a
        # fresh account onto a park that was WORKING again after two
        # no-candidate releases — and the engine swapped the same tick,
        # gate=forced, authorized by a drain episode resumed 3 minutes
        # earlier (drain: timeout, waitedSeconds=1648). A new candidate on
        # a working park deserves the normal orderly episode — wave →
        # checkpoints → swap → resume — never a swap on stale waited time.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        crowded = {"1": _usage(96), "2": _usage(97), "3": _usage(98)}
        assert h.tick_with_usage(crowded) is TickOutcome.BLOCKED  # released
        # The park resumes work; the account stays over the threshold and
        # ticks keep finding nobody — the 17:18–17:33Z stretch.
        park.roster_value = [_park_row("fix-a")]
        for _ in range(4):
            h.clock.advance(300.0)
            _write_transcript(h, age_s=1.0)
            h.tick_with_usage(crowded)
        h.events.clear()
        waves_before = len(park.waves)
        # 17:33Z: a fresh slot is added onto the working park.
        h.seed(4, "d@example.com")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        with_new = {**crowded, "4": _usage(5)}
        outcome = h.tick_with_usage(with_new)
        assert not [e for e in h.events if isinstance(e, SwitchEvent)]
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1  # nobody swapped under the working park
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.targets == ["fix-a"]
        assert len(park.waves) == waves_before + 1
        # The fresh episode then completes normally: checkpoint → swap →
        # resume (the ticket's regression pin for the reordered tick).
        self._ack(h, "fix-a")
        h.clock.advance(60.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        assert h.tick_with_usage(with_new) is TickOutcome.SWITCHED
        assert h.active_number() == 4
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        payload = switch.to_json()
        assert payload["drain2"]["outcome"] == "ready"
        assert "drain" not in payload  # no stale passive-drain label
        resume = [e for e in h.events if isinstance(e, Drain2ResumeEvent)][-1]
        assert resume.targets == ["fix-a"]
        assert "drain2" not in h.state()
        # The alert dedup re-armed the moment a candidate existed again.
        assert "lastAccountAlertedAt" not in h.state()

    def test_unreadable_candidates_hold_a_live_episode(self, temp_home):
        # CON-572 review r1, Important 1: one tick of unreadable candidate
        # usage mid-episode must not release the pause — the first cut of
        # the fix released it, and the next readable tick re-froze the
        # park with a fresh STOP wave (thrash wave→release→wave). Same law
        # as the transient freshen failure: a blip holds every artifact.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert len(park.waves) == 1  # the STOP wave
        # Blip: no candidate has readable usage this tick.
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        blip = {"1": _usage(96), "2": None, "3": None}
        assert h.tick_with_usage(blip) is TickOutcome.BLOCKED
        assert not [e for e in h.events if isinstance(e, Drain2ResumeEvent)]
        assert h.state()["drain2"]["phase"] == "signaled"  # pause survived
        assert len(park.waves) == 1  # no release wave went out
        # Usage readable again: the SAME episode proceeds — no second STOP.
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        assert len(
            [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        ) == 1
        assert "drain2" not in h.state()

    def test_freshen_dead_targets_release_with_backoff(self, temp_home):
        # CON-572 review r1, Important 2: the one release path left with a
        # backoff — candidates keep qualifying but none can be activated
        # ("every ranked target failed to freshen") — had no live test, so
        # a mutation dropping the backoff passed the whole suite. Pin it:
        # the release arms ``drain2ReleaseUntil``, and the next tick with
        # the same broken candidates goes passive v1, never a fresh pause.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        with patch.object(
            h.engine, "_freshen_target", return_value="skip-live-session"
        ):
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.BLOCKED
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.reason == (
            "released without a swap: every ranked target failed to freshen"
        )
        assert resume.targets == ["fix-a"]
        assert "drain2" not in h.state()
        assert h.state()["drain2ReleaseUntil"] > h.clock.now  # backoff armed
        # While the same broken candidates keep qualifying, the backoff
        # stands v2 down: the passive v1 wait takes over, no new pause.
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a")]
        with patch.object(
            h.engine, "_freshen_target", return_value="skip-live-session"
        ):
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._reasons(h)[-1] == "drain-wait"
        assert len(
            [e for e in h.events if isinstance(e, Drain2SignalEvent)]
        ) == 1

    def test_release_clears_a_fallback_v1_record(self, temp_home):
        # CON-572 review r1, Minor 3: the line that drops the v1 record on
        # a dead intent was reachable by no test (the pure drain2 flows
        # never start a v1 wait). Build the record through the fallback —
        # park channel down — then crowd the candidates out: the record
        # must die with the intent, or class B comes back whenever the
        # channel is broken.
        h, park = self._harness(temp_home)
        park.roster_value = None  # `claude agents --json` down → v1 fallback
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert "drain" in h.state()  # passive v1 wait started
        # Candidates crowd out: the intent dies — the v1 record with it.
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        crowded = {"1": _usage(96), "2": _usage(97), "3": _usage(98)}
        assert h.tick_with_usage(crowded) is TickOutcome.BLOCKED
        assert "drain" not in h.state()
        assert [e for e in h.events if e.kind == "last-account"]

    def test_rapid_ticks_never_soft_fix_within_one_turn_boundary(
        self, temp_home
    ):
        # Review r1 finding 2: wake()/TUI edits and settings.json changes
        # slice the inter-tick sleep, so ticks can land seconds apart —
        # three rapid not-busy glances inside ONE stretched turn boundary
        # must not add up to a soft fixation. Only observations spaced
        # ≥DRAIN2_SOFT_FIX_MIN_GAP_S from the previous gate poll count;
        # a properly spaced streak still fixes.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        park.roster_value = [_park_row("fix-a", status="idle")]
        for _ in range(3):  # rapid ticks, 1s apart: none of them counts
            h.clock.advance(1.0)
            _write_transcript(h, age_s=1.0)
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert h.active_number() == 1  # three rapid glances fixed nothing
        assert not [e for e in h.events if isinstance(e, SwitchEvent)]
        for _ in range(2):  # properly spaced polls start counting
            h.clock.advance(30.0)
            _write_transcript(h, age_s=1.0)
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"]["softFixed"] == 1

    def test_resume_with_unreadable_roster_wakes_the_acked_set(
        self, temp_home
    ):
        # Roster down at resume time: wake everyone who provably parked
        # rather than nobody — and report the never-acked honestly (they
        # are state-file knowledge, no roster needed; review r1 nit).
        h, park = self._harness(temp_home)
        state_file = h.switcher.backup_dir / "autoswitch_state.json"
        state_file.write_text(json.dumps({
            "schemaVersion": 1,
            "drain2": {
                "phase": "swapped",
                "trigger": "proactive",
                "startedAt": h.clock.now - 100.0,
                "updatedAt": h.clock.now - 30.0,
                "swappedAt": h.clock.now - 30.0,
                "to": "3",
                "verified": True,
                "signaled": {
                    "fix-a": {"sessionId": "sid-a"},
                    "fix-b": {"sessionId": "sid-b"},
                },
                "acked": ["fix-a"],
            },
        }))
        park.roster_value = None
        outcome = h.tick_with_usage({
            "1": _usage(50), "2": _usage(10), "3": _usage(20),
        })
        assert outcome is TickOutcome.NO_ACTION
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]
        assert resume.unacked == ["fix-b"]
        assert park.waves[-1] == (["fix-a"], DRAIN2_RESUME_MESSAGE)
        assert "drain2" not in h.state()

    def test_transient_switch_failure_keeps_the_episode(self, temp_home):
        # The boundary of the release: a transient freshen failure retries
        # next tick with the episode intact (an orderly pause must not be
        # thrown away on a network blip) — pinned so the release never
        # swallows this path.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        with patch.object(
            h.engine, "_freshen_target", return_value="transient"
        ):
            assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.ERROR
        assert h.state()["drain2"]["phase"] == "signaled"
        assert not [e for e in h.events if isinstance(e, Drain2ResumeEvent)]

    def test_resume_wave_targets_only_the_acked(self, temp_home):
        # Point 3 of CON-461: the resume wave goes to sessions that really
        # parked (receipt on disk, task still open) — not to every signaled
        # name. fix-b never acked and hammered through the cap: "пауза
        # кончилась, продолжай" to an agent that never stopped is the
        # episode's exact noise.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")  # fix-a checkpoints and parks
        h.clock.advance(200.0)  # past the 180s cap
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b"),  # forced: worked through the whole episode
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        warn = next(e for e in h.events if isinstance(e, Drain2TimeoutEvent))
        assert warn.acked == ["fix-a"] and warn.soft == []
        assert warn.forced == ["fix-b"]
        assert "receipt" in warn.human()
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]
        assert resume.unacked == ["fix-b"]
        assert park.waves[-1] == (["fix-a"], DRAIN2_RESUME_MESSAGE)


def _write_session_usage(
    h: EngineHarness, sid: str, tokens: int
) -> Path:
    """A live-shaped transcript for roster session ``sid`` whose last
    assistant usage shows a ``tokens`` context (all in cache_read)."""
    line = json.dumps({
        "type": "assistant",
        "isSidechain": False,
        "message": {
            "usage": {
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": tokens,
                "output_tokens": 1,
            }
        },
    })
    path = h.temp_home / ".claude" / "projects" / "-Users-x" / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line + "\n", encoding="utf-8")
    ts = h.clock.now - 1.0
    os.utime(path, (ts, ts))
    return path


class TestEarlySwap:
    """CON-582 direction (а): the proactive threshold folds in the park
    size. The migration price of a swap is the sum of the live contexts on
    the account being left, so at high-but-below-threshold utilization with
    only a few sessions mid-turn, the engine swaps NOW instead of waiting
    for the threshold under a full park. Voluntary economics: cooldown and
    the quiet gate hold it, an unprovable park size declines it, and
    ``earlySwapThreshold=0`` (the default) keeps it off entirely.
    """

    _EARLY = {"1": _usage(75), "2": _usage(40), "3": _usage(20)}

    def _harness(self, temp_home: Path, **kwargs) -> tuple[EngineHarness, FakePark]:
        kwargs.setdefault("early_swap_threshold", 70.0)
        kwargs.setdefault("early_swap_max_busy", 2)
        kwargs.setdefault("drain2_wait_seconds", 180.0)
        kwargs.setdefault("drain_timeout_seconds", 600.0)
        kwargs.setdefault("switch_under_load", True)
        h = EngineHarness(temp_home, **kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        park = FakePark()
        h.engine = h._make_engine(park=park)
        return h, park

    def _reasons(self, h: EngineHarness) -> list[str]:
        return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]

    def _ack(self, h: EngineHarness, name: str) -> None:
        ack = h.switcher.backup_dir / "drain2-ack" / name
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.touch()
        os.utime(ack, (h.clock.now, h.clock.now))

    def test_small_busy_park_swaps_early(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        early = next(e for e in h.events if isinstance(e, EarlySwapEvent))
        assert early.utilization_pct == 75.0
        assert early.early_threshold == 70.0
        assert early.busy_sessions == 2 and early.max_busy == 2
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.trigger == "proactive"
        assert sorted(signal.targets) == ["fix-a", "fix-b"]
        assert self._reasons(h) == ["drain2-wait"]
        # Fixation completes -> the swap lands below the threshold, marked
        # early in the event (the burn report correlates on it).
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        h.clock.advance(60.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b", status="idle"),
        ]
        assert h.tick_with_usage(self._EARLY) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.to_json()["early"] is True
        assert ", early" in switch.human()
        # One announcement per episode: the continuation tick must not
        # re-emit the early event.
        assert len([e for e in h.events if isinstance(e, EarlySwapEvent)]) == 1

    def test_big_park_holds_for_the_threshold(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [
            _park_row("fix-a"),
            _park_row("fix-b"),
            _park_row("fix-c"),
        ]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["below-threshold"]
        assert park.waves == []
        assert not [e for e in h.events if isinstance(e, EarlySwapEvent)]

    def test_interactive_sessions_count_toward_park_size(self, temp_home):
        # 2 background + 1 interactive busy = 3 > 2: interactive sessions
        # pay the migration too, so they weigh in the "small park" call.
        h, park = self._harness(temp_home)
        park.roster_value = [
            _park_row("fix-a"),
            _park_row("fix-b"),
            _park_row("Yor", kind="interactive", state=None),
        ]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["below-threshold"]
        assert park.waves == []

    def test_off_by_default(self, temp_home):
        h, park = self._harness(temp_home, early_swap_threshold=0.0)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["below-threshold"]
        assert park.roster_calls == 0  # off costs no roster subprocess
        assert AutoSwitchSettings().early_swap_threshold == 0.0

    def test_unreadable_roster_stays_put(self, temp_home):
        # A park that can't be measured can't be called small: no early
        # swap, and no drain2-unavailable theater (nothing was draining).
        h, park = self._harness(temp_home)
        assert park.roster_value is None
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["below-threshold"]
        assert not [e for e in h.events if isinstance(e, Drain2UnavailableEvent)]

    def test_cooldown_holds_early(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        h.engine._mutate_state(lambda s: s.update(lastSwitchAt=h.clock() - 60))
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["below-threshold"]
        assert park.roster_calls == 0 and park.waves == []

    def test_quiet_gate_holds_early_without_switch_under_load(self, temp_home):
        h, park = self._harness(temp_home, switch_under_load=False)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)  # live traffic
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["below-threshold"]
        assert park.roster_calls == 0 and park.waves == []

    def test_quiet_empty_park_swaps_early_without_switch_under_load(
        self, temp_home
    ):
        # The free move: transcripts quiet, nobody busy — the early swap
        # relocates at 75% for zero cache cost instead of waiting for 90%.
        h, park = self._harness(temp_home, switch_under_load=False)
        park.roster_value = []
        _write_transcript(h, age_s=400.0)  # past QUIET_WINDOW_S
        assert h.tick_with_usage(self._EARLY) is TickOutcome.SWITCHED
        assert h.active_number() == 3
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"
        assert switch.to_json()["early"] is True
        assert switch.gate == "quiet"

    def test_growing_park_mid_episode_finishes_the_swap(self, temp_home):
        # One decision per episode: the pause is already bought, so a park
        # that grows past the cap mid-episode is topped up and the swap
        # completes — abandoning would pay the pause twice.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        h.events.clear()
        park.roster_value = [
            _park_row("fix-a"),
            _park_row("fix-b"),
            _park_row("fix-c"),
            _park_row("fix-d"),
        ]
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._EARLY) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["drain2-wait"]  # never below-threshold
        topup = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert topup.top_up is True
        assert sorted(topup.targets) == ["fix-c", "fix-d"]

    def test_no_qualifying_candidate_stays_quiet(self, temp_home):
        # Early opportunism that finds nothing better simply stays put:
        # no last-account alert, no all-exhausted sleep — those belong to
        # ticks that MUST move.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        inside_hysteresis = {
            "1": _usage(75),
            "2": _usage(72),
            "3": _usage(74),
        }
        assert h.tick_with_usage(inside_hysteresis) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["no-qualifying-candidate"]
        assert not [e for e in h.events if isinstance(e, LastAccountAlertEvent)]
        assert "lastAccountAlertedAt" not in h.state()
        assert park.waves == []

    def test_early_under_consume_first_honors_hysteresis(self, temp_home):
        # Review r2: the early swap is voluntary under EITHER strategy.
        # Unlike the at-threshold proactive (must move, any healthy landing
        # qualifies), it must clear the same hysteresis margin `best`
        # applies — or accounts hovering together in the early band
        # ping-pong every cooldown, each cycle at full park price.
        h, park = self._harness(temp_home, strategy="consume-first")
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        inside_hysteresis = {
            "1": _usage(75),
            "2": _usage(74),
            "3": _usage(73),  # a 2% win must never freeze and move the park
        }
        assert h.tick_with_usage(inside_hysteresis) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["no-qualifying-candidate"]
        assert park.waves == []
        assert "drain2" not in h.state()
        # A candidate that clears the margin still qualifies and lands.
        h.events.clear()
        clears = {"1": _usage(75), "2": _usage(74), "3": _usage(40)}
        park.roster_value = [_park_row("fix-a", status="idle")]
        assert h.tick_with_usage(clears) is TickOutcome.SWITCHED
        assert h.active_number() == 3

    def test_early_never_takes_the_api_key_last_resort(self, temp_home):
        # Review r1 finding 1: the api-key last resort is for ticks that
        # MUST move. An early tick that took it would freeze the park in a
        # signaled episode whose swap can never pass the stale-usage gate
        # (api-key rows carry no usage) — a below-threshold livelock.
        h, park = self._harness(temp_home, include_api_key_accounts=True)
        data = h.switcher._get_sequence_data()
        data["accounts"]["2"]["kind"] = "api_key"
        h.switcher._write_json(h.switcher.sequence_file, data)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        inside_hysteresis = {
            "1": _usage(75),
            "2": "api key",
            "3": _usage(74),  # readable but inside the hysteresis margin
        }
        assert h.tick_with_usage(inside_hysteresis) is TickOutcome.NO_ACTION
        assert self._reasons(h) == ["no-qualifying-candidate"]
        assert park.waves == []  # nobody frozen for an impossible swap
        assert "drain2" not in h.state()
        assert h.active_number() == 1

    def test_early_with_no_candidates_never_cries_last_account(
        self, temp_home
    ):
        # Review r1 finding 2: with no candidates at all, the early tick
        # must stay a quiet below-threshold hold — on main this very tick
        # was a plain below-threshold NO_ACTION, and a voluntary trigger
        # must not add the last-account alert or the long blocked wait.
        h = EngineHarness(
            temp_home,
            early_swap_threshold=70.0,
            early_swap_max_busy=2,
            drain2_wait_seconds=180.0,
            drain_timeout_seconds=600.0,
            switch_under_load=True,
        )
        h.seed(1, "a@example.com")
        h.make_live("a@example.com", 1)
        park = FakePark()
        h.engine = h._make_engine(park=park)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage({"1": _usage(75)}) is TickOutcome.NO_ACTION
        assert not [e for e in h.events if isinstance(e, LastAccountAlertEvent)]
        assert "lastAccountAlertedAt" not in h.state()
        assert self._reasons(h) == ["no-candidates"]
        assert park.waves == []


class TestDrain2MoveCost:
    """CON-582 directions (б) + (в): the drain wave's composition and its
    price tag. Before any wave, the engine reads each judged session's
    transcript and estimates the migration price (the context its next turn
    re-creates at full price on the new account); sessions whose context is
    pocket change ride through the swap unfrozen — the checkpoint ceremony
    would cost more than their cache re-create.
    """

    _PROACTIVE = {"1": _usage(96), "2": _usage(40), "3": _usage(20)}

    def _harness(self, temp_home: Path, **kwargs) -> tuple[EngineHarness, FakePark]:
        kwargs.setdefault("drain2_wait_seconds", 180.0)
        kwargs.setdefault("drain_timeout_seconds", 600.0)
        kwargs.setdefault("switch_under_load", True)
        h = EngineHarness(temp_home, **kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        park = FakePark()
        h.engine = h._make_engine(park=park)
        return h, park

    def _ack(self, h: EngineHarness, name: str) -> None:
        ack = h.switcher.backup_dir / "drain2-ack" / name
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.touch()
        os.utime(ack, (h.clock.now, h.clock.now))

    def test_estimate_logged_before_the_wave(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_session_usage(h, "sid-fix-a", 300_000)
        _write_session_usage(h, "sid-fix-b", 250_000)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.est_move_tokens == 550_000
        assert signal.est_session_tokens == {
            "fix-a": 300_000,
            "fix-b": 250_000,
        }
        assert signal.to_json()["estMoveTokens"] == 550_000
        assert "550000 tokens to move" in signal.human()
        # The estimate rides the episode into the switch event.
        self._ack(h, "fix-a")
        self._ack(h, "fix-b")
        h.clock.advance(60.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b", status="idle"),
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"]["estMoveTokens"] == 550_000

    def test_unknown_transcripts_leave_the_estimate_out(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)  # not the session's own transcript
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.est_move_tokens is None
        assert signal.est_session_tokens == {"fix-a": None}
        assert "estMoveTokens" not in signal.to_json()

    def test_small_context_session_rides_through_unfrozen(self, temp_home):
        h, park = self._harness(temp_home)  # default small cap: 50k tokens
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_session_usage(h, "sid-fix-a", 300_000)
        _write_session_usage(h, "sid-fix-b", 9_000)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.targets == ["fix-a"]
        assert signal.skipped_small == ["fix-b"]
        assert signal.est_move_tokens == 309_000  # the small one pays too
        assert park.waves[0][0] == ["fix-a"]
        record = h.state()["drain2"]
        assert sorted(record["signaled"]) == ["fix-a"]
        assert record["small"] == ["fix-b"]
        # fix-b keeps working (busy) and never holds fixation hostage.
        self._ack(h, "fix-a")
        h.clock.advance(60.0)
        _write_session_usage(h, "sid-fix-b", 9_500)  # still writing
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b"),  # still mid-turn
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        drain2 = switch.to_json()["drain2"]
        assert drain2["outcome"] == "ready"
        assert drain2["fixed"] == 1 and drain2["forced"] == 0
        assert drain2["skippedSmall"] == 1
        resume = next(e for e in h.events if isinstance(e, Drain2ResumeEvent))
        assert resume.targets == ["fix-a"]  # the unfrozen one is not woken

    def test_zero_cap_checkpoints_everyone(self, temp_home):
        h, park = self._harness(temp_home, drain2_small_context_tokens=0)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_session_usage(h, "sid-fix-a", 300_000)
        _write_session_usage(h, "sid-fix-b", 9_000)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert sorted(signal.targets) == ["fix-a", "fix-b"]
        assert signal.skipped_small == []

    def test_unknown_context_is_checkpointed(self, temp_home):
        # Unknown is not small: a transcript the engine can't read could be
        # a 900k context — the conservative side is to freeze it.
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        signal = next(e for e in h.events if isinstance(e, Drain2SignalEvent))
        assert signal.targets == ["fix-a"]
        assert signal.skipped_small == []

    def test_all_small_swaps_without_a_pause(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_session_usage(h, "sid-fix-a", 9_000)
        _write_session_usage(h, "sid-fix-b", 8_000)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        switch = next(e for e in h.events if isinstance(e, SwitchEvent))
        assert switch.to_json()["drain2"] == {
            "outcome": "ready",
            "waitedSeconds": 0,
            "fixed": 0,
            "forced": 0,
            "ackFixed": 0,
            "softFixed": 0,
            "skippedSmall": 2,
            "estMoveTokens": 17_000,
        }
        assert park.waves == []  # nobody was frozen, nobody needs waking

    def test_topup_skips_small_newcomer_once(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_session_usage(h, "sid-fix-a", 300_000)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        h.events.clear()
        # A small newcomer appears mid-episode: judged small, not signaled.
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_session_usage(h, "sid-fix-b", 9_000)
        h.clock.advance(30.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        topup = next(
            e
            for e in h.events
            if isinstance(e, Drain2SignalEvent) and e.top_up
        )
        assert topup.targets == []
        assert topup.skipped_small == ["fix-b"]
        assert len(park.waves) == 1  # no second wave for the small one
        assert h.state()["drain2"]["small"] == ["fix-b"]
        h.events.clear()
        # And only once: the next tick must not re-announce it.
        h.clock.advance(30.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert not [
            e
            for e in h.events
            if isinstance(e, Drain2SignalEvent) and e.top_up
        ]


class TestEpisodeLatch:
    """Swap-episode latch + orchestrator notice (CON-581).

    Live hole 15-08 20:33–20:35Z: three door spawns landed seconds before
    the drain2 STOP wave and rode PAST it — nobody signaled them, the swap
    risked tearing their first turns. Two machine guarantees close it:

    - The engine mirrors the live episode (any live ``drain``/``drain2``
      record) into a latch file in the cswap state catalog and publishes
      the latch's path as a fleet file-parameter; the spawn door holds new
      spawns while a FRESH latch exists. The reader judges AGE (mtime
      against the cap baked into the latch), never bare existence — a
      daemon killed mid-episode must not hold the door forever.
    - An advisory herald notice to the orchestrator at each episode
      boundary («спавны держи» / «можно спавнить») — one attempt per
      boundary, deduped through the state file; the episode NEVER waits
      on delivery (the latch is the enforcement, the notice a courtesy).
    """

    _PROACTIVE = {"1": _usage(96), "2": _usage(40), "3": _usage(20)}

    def _harness(self, temp_home: Path, **kwargs) -> tuple[EngineHarness, FakePark]:
        kwargs.setdefault("drain2_wait_seconds", 180.0)
        kwargs.setdefault("drain_timeout_seconds", 600.0)
        kwargs.setdefault("switch_under_load", True)
        h = EngineHarness(temp_home, **kwargs)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.seed(3, "c@example.com")
        h.make_live("a@example.com", 1)
        park = FakePark()
        h.engine = h._make_engine(park=park)
        return h, park

    def _latch(self, h: EngineHarness) -> Path:
        return h.switcher.backup_dir / EPISODE_LATCH_NAME

    def _pointer(self, h: EngineHarness) -> Path:
        return (
            h.temp_home / ".local" / "state" / "fleet"
            / EPISODE_LATCH_POINTER_NAME
        )

    def _name_orchestrator(self, h: EngineHarness, name: str) -> None:
        f = h.temp_home / ".local" / "state" / "fleet" / "orchestrator-name"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{name}\n")

    def _ack(self, h: EngineHarness, name: str) -> None:
        ack = h.switcher.backup_dir / "drain2-ack" / name
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.touch()
        os.utime(ack, (h.clock.now, h.clock.now))

    # -- the latch mirrors the episode --------------------------------------

    def test_signal_raises_the_latch_and_publishes_the_path(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        latch = self._latch(h)
        assert latch.exists()
        payload = json.loads(latch.read_text())
        assert payload["trigger"] == "proactive"
        assert payload["kind"] == "drain2"
        assert payload["phase"] == "signaled"
        assert payload["startedAt"] == h.clock.now
        assert payload["staleAfterSeconds"] == int(DRAIN_STALE_GAP_S)
        # The door never hardcodes the latch path: cswap publishes it as a
        # fleet file-parameter (the orchestrator-name pattern).
        assert self._pointer(h).read_text() == f"{latch}\n"

    def test_clean_swap_drops_the_latch(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._latch(h).exists()
        self._ack(h, "fix-a")
        h.clock.advance(60.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [_park_row("fix-a", status="idle")]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        assert "drain2" not in h.state()  # episode closed…
        assert not self._latch(h).exists()  # …and the latch with it

    def test_release_without_swap_drops_the_latch(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a"), _park_row("fix-b")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._latch(h).exists()
        self._ack(h, "fix-a")
        h.clock.advance(200.0)  # past the 180s fixation cap
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("fix-b"),
        ]
        exhausted = {"1": _usage(96), "2": _usage(100), "3": _usage(100)}
        assert h.tick_with_usage(exhausted) is TickOutcome.BLOCKED
        assert "drain2" not in h.state()
        assert not self._latch(h).exists()

    def test_v1_drain_raises_and_drops_the_latch(self, temp_home):
        # The passive v1 drain is an episode too: a spawn during its wait
        # keeps the account busy and stretches the very silence the swap
        # waits for.
        h, _ = self._harness(temp_home, drain2_wait_seconds=0.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        latch = self._latch(h)
        assert latch.exists()
        payload = json.loads(latch.read_text())
        assert payload["kind"] == "drain"
        assert payload["trigger"] == "proactive"
        h.clock.advance(60.0)
        _write_transcript(h, age_s=QUIET_WINDOW_S + 1.0)  # park went quiet
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        assert not latch.exists()

    def test_restart_clears_a_leftover_latch(self, temp_home):
        # A daemon killed mid-episode leaves the latch on disk; the reader
        # side already ignores it by age, and the next engine tick must
        # remove it (no live record in state = no episode).
        h, park = self._harness(temp_home)
        latch = self._latch(h)
        latch.parent.mkdir(parents=True, exist_ok=True)
        latch.write_text('{"trigger": "proactive", "kind": "drain2"}')
        park.roster_value = []
        _write_transcript(h, age_s=QUIET_WINDOW_S + 1.0)
        h.tick_with_usage({"1": _usage(50), "2": _usage(10), "3": _usage(10)})
        assert not latch.exists()

    def test_stale_record_drops_the_latch_by_age(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._latch(h).exists()
        # The engine goes silent past the staleness gap (stopped daemon,
        # forcing condition gone): the record is another episode's leftovers
        # and the latch must die with it on the next tick.
        h.clock.advance(DRAIN_STALE_GAP_S + 1.0)
        _write_transcript(h, age_s=QUIET_WINDOW_S + 1.0)
        park.roster_value = []
        h.tick_with_usage({"1": _usage(50), "2": _usage(10), "3": _usage(10)})
        assert not self._latch(h).exists()

    def test_dry_run_writes_no_latch(self, temp_home):
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        h.engine = h._make_engine(park=park, dry_run=True)
        h.tick_with_usage(self._PROACTIVE)
        assert not self._latch(h).exists()
        assert not self._pointer(h).exists()

    # -- the orchestrator notice --------------------------------------------

    def test_start_notice_reaches_the_live_orchestrator(self, temp_home):
        h, park = self._harness(temp_home)
        self._name_orchestrator(h, "Yor")
        park.roster_value = [
            _park_row("fix-a"),
            _park_row("Yor", kind="interactive", state=None),
        ]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        # Separate channel: the STOP wave never carries the orchestrator.
        stop_names, stop_msg = park.waves[0]
        assert stop_names == ["fix-a"]
        assert "Yor" not in stop_names
        notice_names, notice_msg = park.waves[-1]
        assert notice_names == ["Yor"]
        assert "спавны держи" in notice_msg
        assert "gate=swap-episode" in notice_msg
        notice = next(
            e for e in h.events if isinstance(e, EpisodeNoticeEvent)
        )
        assert notice.phase == "start"
        assert notice.target == "Yor"
        assert notice.delivered is True
        assert h.state()["episodeNoticeSentAt"] == h.clock.now
        # One notice per boundary: the next holding tick stays quiet.
        h.events.clear()
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        waves_before = len(park.waves)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert len(park.waves) == waves_before
        assert not [e for e in h.events if isinstance(e, EpisodeNoticeEvent)]

    def test_end_notice_follows_the_close(self, temp_home):
        h, park = self._harness(temp_home)
        self._name_orchestrator(h, "yor")  # liveness matches case-insensitively
        park.roster_value = [
            _park_row("fix-a"),
            _park_row("Yor", kind="interactive", state=None),
        ]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        self._ack(h, "fix-a")
        h.clock.advance(60.0)
        _write_transcript(h, age_s=1.0)
        park.roster_value = [
            _park_row("fix-a", status="idle"),
            _park_row("Yor", kind="interactive", state=None),
        ]
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.SWITCHED
        notice_names, notice_msg = park.waves[-1]
        assert notice_names == ["yor"]
        assert "можно спавнить" in notice_msg
        end = [
            e
            for e in h.events
            if isinstance(e, EpisodeNoticeEvent) and e.phase == "end"
        ]
        assert len(end) == 1 and end[0].delivered is True
        assert "episodeNoticeSentAt" not in h.state()

    def test_no_live_orchestrator_is_an_honest_fallback(self, temp_home):
        # No orchestrator in the roster: an honest undelivered event, no
        # herald spawn — and no retry spam (the boundary is handled; the
        # latch, not the notice, is the enforcement).
        h, park = self._harness(temp_home)
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert [names for names, _ in park.waves] == [["fix-a"]]
        notice = next(
            e for e in h.events if isinstance(e, EpisodeNoticeEvent)
        )
        assert notice.phase == "start"
        assert notice.target == "orchestrator"  # the file-parameter default
        assert notice.delivered is False
        assert "no live" in notice.detail
        assert h.state()["episodeNoticeSentAt"] == h.clock.now
        h.events.clear()
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert not [e for e in h.events if isinstance(e, EpisodeNoticeEvent)]

    def test_unreadable_roster_retries_the_notice_next_tick(self, temp_home):
        # Roster down at the boundary: liveness unknown, so the notice is
        # NOT written off — the next tick retries. The stamp moves only on
        # a handled boundary.
        h, park = self._harness(temp_home)
        self._name_orchestrator(h, "Yor")
        park.roster_value = [_park_row("fix-a")]
        _write_transcript(h, age_s=1.0)
        assert h.tick_with_usage(self._PROACTIVE) is TickOutcome.NO_ACTION
        assert self._latch(h).exists()
        park.roster_value = None  # channel dies AFTER the signal
        h.clock.advance(30.0)
        _write_transcript(h, age_s=1.0)
        h.events.clear()
        h.tick_with_usage(self._PROACTIVE)
        retry = [
            e
            for e in h.events
            if isinstance(e, EpisodeNoticeEvent) and e.phase == "start"
        ]
        assert retry and retry[-1].delivered is False
        assert "retry" in retry[-1].detail
        assert "episodeNoticeSentAt" not in h.state()
