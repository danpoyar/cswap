"""The TUI and the menu bar hand a live-session slot to ``cswap run`` (CON-1595).

CON-1579 made ``switch`` REFUSE a slot whose live ``cswap run`` session owns a
rotated token family (the recipe is in the error text: ``cswap run N``). The
terminal user reads that text; the TUI and the menu bar — the two surfaces
the operator actually switches from — only showed it as a failed action.
Now the refusal is a typed ``LiveSessionRefusal`` carrying the slot and the
recipe, the TUI offers the recipe as a confirm and, on yes, exits and execs
``cswap run N`` in the same terminal; the menu bar shows the recipe with a
copy-to-clipboard button.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from textual.widgets import Static

from claude_swap import tui
from claude_swap.exceptions import LiveSessionRefusal, SwitchError
from claude_swap.tui.data import RunHandoff
from tests.test_switch_heal import (
    TARGET_EMAIL,
    TARGET_NUM,
    _make_switcher,
    _rotated_profile,
    no_network,  # noqa: F401 — fixture registered by import
)
from tests.test_tui import FakeSwitcher, make_account, make_app, settle


def _refusal(number: str = "2") -> LiveSessionRefusal:
    return LiveSessionRefusal(
        f"Account-{number} (user{number}@example.com) has a live session-mode "
        f"Claude instance (PID 4242) … For a terminal on this slot run: "
        f"cswap run {number}",
        account_num=number,
        email=f"user{number}@example.com",
        pids=[4242],
    )


class TestLiveSessionRefusalType:
    def test_switch_refusal_is_typed_with_the_recipe(
        self, temp_home, mock_claude_config, no_network
    ):
        """RED on main: a bare ``SwitchError`` — no slot, no PIDs, no command."""
        s = _make_switcher(temp_home)
        _rotated_profile(s)

        with patch.object(s, "_live_session_pids", return_value=[4242]):
            with pytest.raises(LiveSessionRefusal) as exc:
                s.switch_to(TARGET_NUM, json_output=True)

        e = exc.value
        assert isinstance(e, SwitchError)  # every existing handler still catches it
        assert e.account_num == TARGET_NUM
        assert e.email == TARGET_EMAIL
        assert e.pids == [4242]
        assert e.command == f"cswap run {TARGET_NUM}"
        assert e.command in str(e)


@pytest.mark.asyncio
class TestTuiOffersRunOnLiveSession:
    async def test_confirm_hands_off_to_cswap_run(self, tmp_path):
        """RED on main: the refusal lands in a failed-action modal; the app
        never offers the recipe and never exits with a handoff."""
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)

        def refuse(identifier, json_output=False, force=False):
            raise _refusal(str(identifier))

        fake.switch_to = refuse
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("2")
            await settle(pilot)
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal), type(app.screen)
            text = app.screen._message  # the body Static renders exactly this
            assert "cswap run 2" in text
            assert "4242" in text
            assert app.screen.query_one(".modal-body", Static) is not None
            await pilot.press("y")
            await pilot.pause()

        assert app.return_value == RunHandoff(number="2")
        assert (app.return_code or 0) == 0

    async def test_decline_keeps_the_app_running_and_switches_nothing(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)

        def refuse(identifier, json_output=False, force=False):
            raise _refusal(str(identifier))

        fake.switch_to = refuse
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("2")
            await settle(pilot)
            from claude_swap.tui.dashboard import DashboardScreen
            from claude_swap.tui.modals import ConfirmModal

            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, DashboardScreen)
            assert app.return_value is None
            assert fake.active == "1"

    async def test_other_switch_errors_still_show_the_failed_action(self, tmp_path):
        fake = FakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)

        def fail(identifier, json_output=False, force=False):
            raise SwitchError("boom")

        fake.switch_to = fail
        app = make_app(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.do_switch("2")
            await settle(pilot)
            from claude_swap.tui.modals import OutputModal

            assert isinstance(app.screen, OutputModal)
            assert app.return_value is None


class TestTuiRunHandsOffToSessionManager:
    def test_run_execs_cswap_run_after_the_app_exits(self):
        calls: list = []

        class FakeApp:
            def __init__(self, switcher, *, start="dashboard", detected=None):
                self.return_value = RunHandoff(number="2")
                self.return_code = 0

            def run(self):
                return self.return_value

        class FakeSessionManager:
            def __init__(self, switcher):
                calls.append(("init", switcher))

            def run(self, identifier, claude_args, share=True, share_history=False):
                calls.append(("run", identifier, list(claude_args), share, share_history))

        switcher = object()
        with (
            patch("claude_swap.tui.app.CswapApp", FakeApp),
            patch("claude_swap.session.SessionManager", FakeSessionManager),
            patch("claude_swap.appearance.detect_terminal_background", return_value=None),
            patch("claude_swap.appearance.drain_stdin"),
        ):
            rc = tui.run(switcher)

        assert ("init", switcher) in calls
        assert ("run", "2", [], True, False) in calls
        assert rc == 0

    def test_plain_exit_does_not_touch_the_session_manager(self):
        calls: list = []

        class FakeApp:
            def __init__(self, switcher, *, start="dashboard", detected=None):
                self.return_value = None
                self.return_code = 0

            def run(self):
                return None

        class FakeSessionManager:
            def __init__(self, switcher):
                calls.append("init")

        with (
            patch("claude_swap.tui.app.CswapApp", FakeApp),
            patch("claude_swap.session.SessionManager", FakeSessionManager),
            patch("claude_swap.appearance.detect_terminal_background", return_value=None),
            patch("claude_swap.appearance.drain_stdin"),
        ):
            assert tui.run(object()) == 0

        assert calls == []


class TestMenubarLiveSessionAlert:
    def test_alert_names_the_slot_pids_and_the_command(self):
        from claude_swap.menubar import live_session_alert

        title, message, command = live_session_alert(_refusal("31"))

        assert command == "cswap run 31"
        assert "31" in title
        assert "4242" in message
        assert command in message
