"""``autoswitch.homeAccount`` (CON-1070): the live login rests on one pinned
slot instead of riding the rotation.

Background: the fleet seats background agents per slot through ``cswap run``
and the seat-picker refuses the *active* slot (``cswap run <active>`` takes
the same-account fast path, which rides the global login and would be swapped
from under the agent). With the orchestrator's own slot held out of rotation
(``cswap disable``), the daemon parked the global login on a rotational slot
forever — one seat lost to the fleet at all times, and every hand-made
``cswap switch <home>`` landed on a stale backup copy and died (failover).

The pin: the daemon never moves the login off the home slot on its own
(threshold, consume-first, early swap and at-limit all hold); a dead token
(failover) is the only escape; away from home it returns as soon as the home
slot proves alive (readable usage), ignoring the cooldown. A disabled or
unknown home makes the pin inert with one warning.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from claude_swap.autoswitch import (
    ConfigWarningEvent,
    ErrorEvent,
    NoSwitchEvent,
    QuarantineEvent,
    SwitchEvent,
    TickOutcome,
)
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.settings import (
    SETTING_SPECS,
    AutoSwitchSettings,
    load_settings,
    set_setting,
    settings_path,
    unset_setting,
)
from tests.test_autoswitch import (
    _R_LATEST,
    _R_SOON,
    EngineHarness,
    _usage,
    _usage7,
    _write_transcript,
)

HOME = "home@example.com"


def _harness(temp_home: Path, *, live: int, **kwargs) -> EngineHarness:
    """Three seeded slots — 1 is home, 2 and 3 rotate — consume-first and no
    cooldown, so any voluntary move the strategy wants fires on tick one."""
    settings = {
        "strategy": "consume-first",
        "cooldown_seconds": 0.0,
        "home_account": "1",
        **kwargs,
    }
    h = EngineHarness(temp_home, **settings)
    h.seed(1, HOME)
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    emails = {1: HOME, 2: "b@example.com", 3: "c@example.com"}
    h.make_live(emails[live], live)
    data = h.switcher._get_sequence_data()
    assert data is not None
    data["activeAccountNumber"] = live
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


class TestHomePinHolds:
    def test_consume_first_never_leaves_home(self, temp_home):
        # The bug shape: home resets latest, a rotational slot resets soonest
        # with room to spare — consume-first moved the login there, and the
        # fleet lost that slot to the active-marker.
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _switches(h) == []
        assert _reasons(h) == ["home-pinned"]

    def test_threshold_crossed_still_holds(self, temp_home):
        h = _harness(temp_home, live=1, strategy="best")
        outcome = h.tick_with_usage({
            "1": _usage(95), "2": _usage(10), "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["home-pinned"]

    def test_at_limit_holds_through_the_wall(self, temp_home):
        # The pin holds through limits: a maxed window on the home slot is
        # the user's own wall to wait out, not a reason to take a fleet seat.
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _usage(100, "2024-01-05T00:00:00Z"),
            "2": _usage(10),
            "3": _usage(10),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["home-pinned"]

    def test_dead_home_token_still_fails_over(self, temp_home):
        # The one escape: the home login cannot authenticate at all. Three
        # unreadable ticks (the unhealthyTicks default) escalate to failover
        # exactly as without a pin.
        h = _harness(temp_home, live=1)
        usage = {"1": None, "2": _usage(10), "3": _usage(10)}
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() != 1
        (switch,) = _switches(h)
        assert switch.trigger == "failover"


class TestReturnHome:
    def test_returns_as_soon_as_home_reads(self, temp_home):
        # Away from home (the failover landing), home's usage reads again:
        # the login goes back even though the rotation would prefer to stay
        # (home resets latest, so consume-first would never pick it).
        h = _harness(temp_home, live=2)
        outcome = h.tick_with_usage({
            "1": _usage7(30, 30, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 1
        (switch,) = _switches(h)
        assert switch.trigger == "return-home"
        assert switch.to_ref == {"number": 1, "email": HOME}
        assert h.state()["lastSwitchTrigger"] == "return-home"

    def test_returns_past_the_cooldown(self, temp_home):
        # A correction, not churn: the cooldown that debounces consume-first
        # must not hold the login on a fleet seat for half an hour.
        h = _harness(temp_home, live=2, cooldown_seconds=1800.0)
        h.switcher._write_json(
            h.switcher.backup_dir / "autoswitch_state.json",
            {"lastSwitchAt": h.clock.now - 10, "lastSwitchTo": "2"},
        )
        outcome = h.tick_with_usage({
            "1": _usage7(30, 30, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_stays_away_while_home_token_is_dead(self, temp_home):
        # No proof of life, no return: the rotation keeps working as before
        # (a stale backup must never be switched onto — that is how the
        # home slot died in the first place).
        h = _harness(temp_home, live=2)
        usage = {
            "1": USAGE_TOKEN_EXPIRED,
            "2": _usage7(10, 10, _R_LATEST),
            "3": _usage7(10, 10, _R_SOON),
        }
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        (switch,) = _switches(h)
        assert switch.trigger == "consume-first"

    def test_unreadable_home_is_not_a_return(self, temp_home):
        h = _harness(temp_home, live=2)
        usage = {
            "1": None,
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert _switches(h) == []

    def test_disabled_home_makes_the_pin_inert_with_one_warning(self, temp_home):
        # ``cswap disable <home>`` is the user's explicit hold-out and wins;
        # the daemon says so once, not every tick.
        h = _harness(temp_home, live=2)
        h.switcher.set_account_disabled("1", True)
        usage = {
            "1": _usage7(30, 30, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_SOON),
        }
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        h.clock.advance(60)
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        warnings = [
            e.message for e in h.events if isinstance(e, ConfigWarningEvent)
        ]
        assert len(warnings) == 1
        assert "homeAccount" in warnings[0] and "disabled" in warnings[0]
        assert "cswap enable 1" in warnings[0]

    def test_unknown_home_warns_once_and_rotates_normally(self, temp_home):
        h = _harness(temp_home, live=2, home_account="nobody@example.com")
        usage = {
            "1": _usage7(10, 10, _R_SOON),
            "2": _usage7(10, 10, _R_LATEST),
            "3": _usage7(10, 10, _R_LATEST),
        }
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED  # plain consume-first to #1
        h.clock.advance(60)
        h.tick_with_usage(usage)
        warnings = [
            e.message for e in h.events if isinstance(e, ConfigWarningEvent)
        ]
        assert len(warnings) == 1
        assert "homeAccount" in warnings[0]

    def test_quarantined_home_is_left_alone(self, temp_home):
        # A landing on the home slot already failed on a dead lineage: the
        # engine's own quarantine (fingerprinted, released only by a re-add)
        # holds the return, and the rotation leaves the slot alone too.
        h = _harness(temp_home, live=2)
        h.engine._quarantine("1", HOME, "invalid_grant")
        h.events.clear()
        usage = {
            "1": _usage7(30, 30, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert _switches(h) == []

    def test_no_pin_keeps_the_old_behaviour(self, temp_home):
        h = _harness(temp_home, live=1, home_account=None)
        outcome = h.tick_with_usage({
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 2


class TestHomeAccountSetting:
    def test_spec_is_a_plain_string(self):
        spec = SETTING_SPECS["autoswitch.homeAccount"]
        assert spec.field == "home_account"
        assert spec.kind == "string"
        assert AutoSwitchSettings().home_account is None

    def test_set_and_unset_roundtrip(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.homeAccount", "32") == "32"
        assert load_settings(tmp_path).home_account == "32"
        assert set_setting(tmp_path, "autoswitch.homeAccount", "yor@x.com") == "yor@x.com"
        assert load_settings(tmp_path).home_account == "yor@x.com"
        assert unset_setting(tmp_path, "autoswitch.homeAccount") is True
        assert load_settings(tmp_path).home_account is None


class TestReturnHomeWaits:
    """A return that cannot land yet must not silence the rotation's escapes
    for the slot the login is on (review r1)."""

    def _away(self, temp_home, **kwargs) -> EngineHarness:
        return _harness(temp_home, live=2, **kwargs)

    def test_traffic_holds_the_return_but_not_the_at_limit_escape(self, temp_home):
        h = self._away(temp_home)
        _write_transcript(h, age_s=10.0)
        outcome = h.tick_with_usage({
            "1": _usage(30),
            "2": _usage(100, "2024-01-05T00:00:00Z"),
            "3": _usage(10),
        })
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "at-limit"
        assert h.active_number() != 2
        assert "return-home-wait" in _reasons(h)

    def test_traffic_holds_the_return_and_logs_why(self, temp_home):
        h = self._away(temp_home)
        _write_transcript(h, age_s=10.0)
        outcome = h.tick_with_usage({
            "1": _usage7(30, 30, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_SOON),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert _reasons(h)[0] == "return-home-wait"

    def test_live_session_on_home_holds_the_return(self, temp_home):
        h = self._away(temp_home)
        with patch.object(h.engine, "_freshen_target", return_value="skip-live-session"):
            outcome = h.tick_with_usage({
                "1": _usage7(30, 30, _R_LATEST),
                "2": _usage7(10, 10, _R_SOON),
                "3": _usage7(10, 10, _R_SOON),
            })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert _reasons(h)[0] == "return-home-wait"
        assert "cswap run" in next(
            e.detail for e in h.events if isinstance(e, NoSwitchEvent)
        )

    def test_dead_current_still_fails_over_while_home_is_held(self, temp_home):
        # The login's own token died on a rotational slot while a `cswap run`
        # session holds the home slot: failover must still escape (to any
        # other healthy slot — the held home is skipped by the same rule).
        h = self._away(temp_home)
        real = h.engine._freshen_target

        def freshen(number, email):
            return "skip-live-session" if number == "1" else real(number, email)

        usage = {"1": _usage(30), "2": None, "3": _usage(10)}
        with patch.object(h.engine, "_freshen_target", side_effect=freshen):
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
            assert h.tick_with_usage(usage) is TickOutcome.NO_ACTION
            outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "failover"
        assert h.active_number() == 3

    def test_network_blip_waits_and_leaves_the_rotation_alone(self, temp_home):
        h = self._away(temp_home)
        with patch.object(h.engine, "_freshen_target", return_value="transient"):
            outcome = h.tick_with_usage({
                "1": _usage7(30, 30, _R_LATEST),
                "2": _usage7(10, 10, _R_SOON),
                "3": _usage7(10, 10, _R_SOON),
            })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 2
        assert _reasons(h)[0] == "return-home-wait"
        assert not any(isinstance(e, ErrorEvent) for e in h.events)


class TestReturnHomeQuarantine:
    """A dead lineage found by the return is quarantined for the whole tick:
    the same tick's rotation must not land on it (review r1)."""

    def _over_threshold(self, temp_home) -> tuple[EngineHarness, dict]:
        h = _harness(temp_home, live=2, strategy="best")
        usage = {"1": _usage(10), "2": _usage(95), "3": _usage(20)}
        return h, usage

    def test_identity_conflict_is_not_landed_on_by_the_same_tick(self, temp_home):
        # First freshen persisted a fresh token for the conflicting slot, so
        # a second look would say "ok" — only the in-place quarantine keeps
        # the proactive escape off it.
        h, usage = self._over_threshold(temp_home)
        real = h.engine._freshen_target
        calls: list[str] = []

        def freshen(number, email):
            calls.append(number)
            if number == "1" and calls.count("1") == 1:
                return "identity-conflict"
            return real(number, email)

        with patch.object(h.engine, "_freshen_target", side_effect=freshen):
            outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "proactive"
        assert h.active_number() == 3
        assert "1" in h.state()["quarantine"]
        assert calls.count("1") == 1
        assert len([e for e in h.events if isinstance(e, QuarantineEvent)]) == 1

    def test_invalid_grant_is_quarantined_once(self, temp_home):
        h, usage = self._over_threshold(temp_home)
        real = h.engine._freshen_target

        def freshen(number, email):
            return "invalid_grant" if number == "1" else real(number, email)

        with patch.object(h.engine, "_freshen_target", side_effect=freshen):
            outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 3
        assert len([e for e in h.events if isinstance(e, QuarantineEvent)]) == 1
        # Quarantined: the next tick never tries the return again.
        h.events.clear()
        h.clock.advance(60)
        with patch.object(h.engine, "_freshen_target", side_effect=freshen) as spy:
            h.tick_with_usage({"1": _usage(10), "2": _usage(95), "3": _usage(20)})
        assert "1" not in [c.args[0] for c in spy.call_args_list]


class TestDisabledHomeAtHome:
    def test_disabled_home_leaves_by_the_threshold_with_one_warning(self, temp_home):
        # `cswap disable <home>` while the login sits on it: the pin is
        # inert there too — the threshold moves the login as without a pin,
        # and the daemon says why once.
        h = _harness(temp_home, live=1, strategy="best")
        h.switcher.set_account_disabled("1", True)
        usage = {"1": _usage(95), "2": _usage(10), "3": _usage(20)}
        outcome = h.tick_with_usage(usage)
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "proactive"
        assert h.active_number() == 2
        warnings = [
            e.message for e in h.events if isinstance(e, ConfigWarningEvent)
        ]
        assert len(warnings) == 1 and "disabled" in warnings[0]
        assert "home-pinned" not in _reasons(h)

    def test_re_enabled_home_pins_again_and_re_warns_on_a_new_disable(self, temp_home):
        h = _harness(temp_home, live=1)
        usage = {
            "1": _usage7(20, 20, _R_LATEST),
            "2": _usage7(10, 10, _R_SOON),
            "3": _usage7(10, 10, _R_LATEST),
        }
        h.switcher.set_account_disabled("1", True)
        h.tick_with_usage(usage)  # warns, rotation moves the login
        h.switcher.set_account_disabled("1", False)
        h.clock.advance(60)
        h.tick_with_usage(usage)  # returns home
        h.switcher.set_account_disabled("1", True)
        h.clock.advance(60)
        h.tick_with_usage(usage)  # a fresh condition: warns again
        warnings = [e for e in h.events if isinstance(e, ConfigWarningEvent)]
        assert len(warnings) == 2


class TestSettingsClamp:
    def test_bare_json_number_reads_as_the_slot(self, tmp_path: Path):
        path = settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"autoswitch": {"homeAccount": 32}}))
        assert load_settings(tmp_path).home_account == "32"
