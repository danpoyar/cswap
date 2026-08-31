"""The home pin judges the pinned model's scoped window (CON-1581).

31-08 incident: ``return-home`` moved the live login back onto the home
slot whose Fable window sat at 100% while its 5h/7d windows were healthy —
the pin judged only token liveness ("only a dead token moves it"), so the
interactive terminal, whose work is pinned to that model, went mute until
the weekly reset. Now, with ``autoswitch.model`` configured and the pin
active, the login is judged model-aware everywhere:

- a home whose scoped model window is at/over ``threshold`` is not a
  return target (``home-model-burned`` wait; the return lands once the
  window resets);
- at home the same condition yields the pin's hold to the plain escape
  triggers (at-limit at 100%, proactive at/over the threshold) instead of
  resting a login that cannot serve its model;
- the pool-shield's burned-host preference (CON-712) stands down while
  the pin is active: the pin already guarantees the fleet a resting
  login, so parking the terminal on a model-burned host away from home
  would re-create the mute terminal the escape just left.

The account-wide walls keep holding at home (CON-1070: a maxed 5h/7d
window is the user's own wall to wait out) — only the scoped model window
escapes.
"""

from __future__ import annotations

from pathlib import Path

from claude_swap.autoswitch import (
    AdoptRealLoginEvent,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from tests.test_autoswitch import EngineHarness, _scoped_usage

HOME = "home@example.com"
EMAILS = {1: HOME, 2: "b@example.com", 3: "c@example.com"}


def _harness(
    temp_home: Path, *, live: int, record: int | None = None, **kwargs
) -> EngineHarness:
    """Three seeded slots — 1 is home, 2 and 3 rotate — with the Fable
    window bound into the decision, consume-first and no cooldown."""
    settings = {
        "strategy": "consume-first",
        "cooldown_seconds": 0.0,
        "home_account": "1",
        "model": "Fable",
        **kwargs,
    }
    h = EngineHarness(temp_home, **settings)
    h.seed(1, HOME)
    h.seed(2, EMAILS[2])
    h.seed(3, EMAILS[3])
    h.make_live(EMAILS[live], live)
    data = h.switcher._get_sequence_data()
    assert data is not None
    data["activeAccountNumber"] = record if record is not None else live
    h.switcher._write_json(h.switcher.sequence_file, data)
    return h


def _reasons(h: EngineHarness) -> list[str]:
    return [e.reason for e in h.events if isinstance(e, NoSwitchEvent)]


def _switches(h: EngineHarness) -> list[SwitchEvent]:
    return [e for e in h.events if isinstance(e, SwitchEvent)]


class TestReturnHomeModelBurned:
    def test_return_refuses_model_burned_home(self, temp_home):
        # The 08:17:26Z shape: away on a healthy slot, home reads fine but
        # its Fable window is maxed. The return landed and the terminal
        # went mute; now it waits with a named reason.
        h = _harness(temp_home, live=2)
        h.tick_with_usage({
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert h.active_number() == 2
        assert _switches(h) == []
        assert "home-model-burned" in _reasons(h)
        detail = next(
            e.detail
            for e in h.events
            if isinstance(e, NoSwitchEvent) and e.reason == "home-model-burned"
        )
        assert "Fable" in detail and "Account-1" in detail

    def test_return_refuses_at_the_threshold_not_only_at_100(self, temp_home):
        # Same threshold as the proactive escape: 90 means 90.
        h = _harness(temp_home, live=2)
        h.tick_with_usage({
            "1": _scoped_usage(5.0, 90.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert h.active_number() == 2
        assert _switches(h) == []
        assert "home-model-burned" in _reasons(h)

    def test_model_all_sentinel_judges_every_scoped_window(self, temp_home):
        # "all" names no particular window and matches whatever scoped
        # windows the account reports — a burned one still blocks the
        # return.
        h = _harness(temp_home, live=2, model="all")
        h.tick_with_usage({
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert h.active_number() == 2
        assert _switches(h) == []
        assert "home-model-burned" in _reasons(h)

    def test_return_lands_while_the_window_is_below_threshold(self, temp_home):
        h = _harness(temp_home, live=2)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 30.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "return-home"
        assert h.active_number() == 1


class TestHomeEscapesBurnedModel:
    def test_at_limit_escape_leaves_home_when_the_model_is_maxed(self, temp_home):
        # The incident's steady state: at home, Fable 100%, 5h/7d healthy.
        # The pin used to hold ("home-pinned") while every Fable request
        # failed; the maxed binding window must escape.
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "at-limit"
        assert h.active_number() != 1
        assert "home-pinned" not in _reasons(h)

    def test_proactive_escape_leaves_home_past_the_threshold(self, temp_home):
        # Below 100% but at/over the threshold the pin yields too — the
        # same proactive semantics as the plain rotation, not a ride into
        # the wall (the 31-07 cooldown incident class).
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 92.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "proactive"
        assert h.active_number() != 1

    def test_five_hour_wall_still_holds_at_home(self, temp_home):
        # CON-1070 stands: an account-wide wall at home is the user's own
        # to wait out. Only the scoped model window escapes.
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(100.0, 50.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["home-pinned"]

    def test_model_below_the_threshold_holds_at_home(self, temp_home):
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 80.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _reasons(h) == ["home-pinned"]


class TestPinSuspendsPoolShield:
    def test_no_rescue_onto_a_burned_host_while_pinned(self, temp_home):
        # Pool-shield's rescue (CON-712) parks the login on a model-burned
        # host to free a model-fresh one for the fleet. With the pin active
        # that trade re-creates the mute terminal: the login is coming home
        # anyway, so voluntary landings stay model-aware.
        h = _harness(temp_home, live=2)
        h.tick_with_usage({
            "1": None,  # home unreadable: the return cannot land
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 95.0),
        })
        assert h.active_number() == 2
        assert _switches(h) == []


class TestIncidentShape:
    def test_stale_record_burned_home_swaps_to_a_fit_slot(self, temp_home):
        # The brief's fixture: the record (указатель) names slot 2, the
        # real login sits on the home slot 1 after a manual /login, and
        # home's Fable window is at 100%. One tick must adopt the real
        # login into the record AND move the login to a fit slot.
        h = _harness(temp_home, live=1, record=2)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        adopts = [e for e in h.events if isinstance(e, AdoptRealLoginEvent)]
        assert len(adopts) == 1
        assert adopts[0].prior == {"number": 2, "email": EMAILS[2]}
        assert adopts[0].to_ref == {"number": 1, "email": HOME}
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "at-limit"
        assert h.active_number() not in (1, None)
