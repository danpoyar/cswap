"""The home pin holds through a burned model window (CON-2069).

History. CON-1581 (31-08) taught the pin to judge the pinned model's scoped
window: ``return-home`` had landed the login on a home whose Fable window
sat at 100% and the interactive terminal — whose work was pinned to that
model — went mute until the weekly reset. So a model-burned home became
neither a return target (``home-model-burned`` wait) nor a resting place
(the pin yielded to the at-limit/proactive escapes).

CON-2069 (04-09, the third time the owner said "the active login is always
on slot 32"): the terminals no longer ride the global login — they run as
``cswap run`` sessions in the home slot's own profile — and the global login
serves only the couriers (jobs without ``cswap run``), which ride the model
ladder (Opus/Sonnet) and do not need the pinned model at all. The model-window
escape therefore bought nothing for the terminal and cost the fleet a seat:
every slot the login moved to fell out of the seat-picker's rotation
(``active`` class) for as long as the home stayed burned — two days at a
time, with a 50+ ticket queue. With the pin active the login is judged on
token liveness only, everywhere:

- at home, a burned scoped window is a ``home-pinned`` hold whose detail
  names the window — never an at-limit/proactive escape;
- away from home, the return lands as soon as the home slot proves alive,
  whatever its scoped window reads;
- the account-wide 5h/7d walls keep holding at home (CON-1070), a dead
  token still fails over (``test_home_account``), and an explicit
  ``cswap config set autoswitch.homeAccount`` (or ``cswap disable``) is the
  only voluntary way off the home slot.

The pool-shield stand-down while pinned (CON-712/CON-1581) is unchanged.
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


def _detail(h: EngineHarness, reason: str) -> str:
    return next(
        e.detail
        for e in h.events
        if isinstance(e, NoSwitchEvent) and e.reason == reason
    )


class TestReturnHomeIgnoresModelWindow:
    def test_return_lands_on_a_model_burned_home(self, temp_home):
        # The CON-1581 shape inverted: away on a healthy slot, home reads
        # fine but its Fable window is maxed. The login belongs home
        # regardless — the couriers there do not need Fable, and the slot
        # the login is squatting on is a fleet seat.
        h = _harness(temp_home, live=2)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "return-home"
        assert h.active_number() == 1
        assert "home-model-burned" not in _reasons(h)

    def test_return_lands_at_the_threshold_too(self, temp_home):
        h = _harness(temp_home, live=2)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 90.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 1

    def test_model_all_sentinel_does_not_block_the_return(self, temp_home):
        h = _harness(temp_home, live=2, model="all")
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.SWITCHED
        assert h.active_number() == 1

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


class TestHomeHoldsThroughBurnedModel:
    def test_maxed_model_at_home_holds_as_home_pinned(self, temp_home):
        # The 04-09 steady state: at home, Fable 100%, 5h/7d healthy. The
        # pin used to yield to at-limit and take a fleet seat; now it holds
        # and says which window is burned.
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _switches(h) == []
        assert _reasons(h) == ["home-pinned"]
        detail = _detail(h, "home-pinned")
        assert "Fable" in detail and "100%" in detail
        assert "Account-1" in detail

    def test_model_past_the_threshold_at_home_holds_too(self, temp_home):
        # Below 100% but at/over the threshold: the proactive escape used
        # to fire here. Same hold, same reason.
        h = _harness(temp_home, live=1)
        outcome = h.tick_with_usage({
            "1": _scoped_usage(5.0, 92.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        })
        assert outcome is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _switches(h) == []
        assert _reasons(h) == ["home-pinned"]
        assert "92%" in _detail(h, "home-pinned")

    def test_burned_hold_at_home_resets_the_unhealthy_count(self, temp_home):
        # A burned window is not a dead token: it must reset the unhealthy
        # count the way any readable tick does (the default failover needs
        # three unreadable ticks in a row). dead → burned → dead → dead must
        # not fail over: the burned tick in between broke the streak, and
        # only the two trailing dead ticks count.
        h = _harness(temp_home, live=1)
        dead = {"1": None, "2": _scoped_usage(10.0, 20.0), "3": _scoped_usage(10.0, 30.0)}
        burned = {
            "1": _scoped_usage(5.0, 100.0),
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 30.0),
        }
        assert h.tick_with_usage(dead) is TickOutcome.NO_ACTION
        assert h.tick_with_usage(burned) is TickOutcome.NO_ACTION
        assert "home-pinned" in _reasons(h)
        assert h.tick_with_usage(dead) is TickOutcome.NO_ACTION
        assert h.tick_with_usage(dead) is TickOutcome.NO_ACTION
        assert h.active_number() == 1
        assert _switches(h) == []
        # The third consecutive dead tick still fails over (CON-1070 escape).
        assert h.tick_with_usage(dead) is TickOutcome.SWITCHED
        (switch,) = _switches(h)
        assert switch.trigger == "failover"

    def test_five_hour_wall_still_holds_at_home(self, temp_home):
        # CON-1070 stands: an account-wide wall at home is the user's own
        # to wait out.
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
        assert "80%" not in _detail(h, "home-pinned")


class TestPinSuspendsPoolShield:
    def test_no_rescue_onto_a_burned_host_while_pinned(self, temp_home):
        # Pool-shield's rescue (CON-712) parks the login on a model-burned
        # host to free a model-fresh one for the fleet. With the pin active
        # that trade is pointless: the login is coming home anyway.
        h = _harness(temp_home, live=2)
        h.tick_with_usage({
            "1": None,  # home unreadable: the return cannot land
            "2": _scoped_usage(10.0, 20.0),
            "3": _scoped_usage(10.0, 95.0),
        })
        assert h.active_number() == 2
        assert _switches(h) == []


class TestIncidentShape:
    def test_stale_record_burned_home_adopts_and_holds(self, temp_home):
        # The CON-1581 fixture with the CON-2069 verdict: the record names
        # slot 2, the real login sits on the home slot 1 after a manual
        # /login, and home's Fable window is at 100%. One tick adopts the
        # real login into the record AND leaves it where it is.
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
        assert outcome is TickOutcome.NO_ACTION
        assert _switches(h) == []
        assert h.active_number() == 1
        assert _reasons(h) == ["home-pinned"]
