"""Tests for settings.json load/save/merge (settings.py)."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path

import pytest

from claude_swap.exceptions import ConfigError
from claude_swap.settings import (
    SETTING_SPECS,
    AutoSwitchSettings,
    UiSettings,
    cli_overrides,
    effective_settings,
    load_settings,
    load_ui_settings,
    read_settings,
    save_settings,
    set_setting,
    settings_path,
    unset_setting,
    with_overrides,
)


def _args(**kwargs) -> argparse.Namespace:
    defaults = {
        "threshold": None,
        "interval": None,
        "cooldown": None,
        "include_api_key_accounts": None,
        "strategy": None,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestLoadSettings:
    def test_missing_file_gives_defaults(self, tmp_path: Path):
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_corrupt_file_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_non_object_gives_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text("[1, 2]")
        assert load_settings(tmp_path) == AutoSwitchSettings()

    def test_partial_section_fills_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": {"threshold": 80}})
        )
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 80.0
        assert loaded.interval_seconds == AutoSwitchSettings().interval_seconds

    def test_values_are_clamped(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {
                "threshold": 200,
                "intervalSeconds": 1,
                "hysteresisPct": -5,
                "unhealthyTicks": 0,
            }
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == 99.9
        assert loaded.interval_seconds == 15.0  # usage-cache TTL floor
        assert loaded.hysteresis_pct == 0.0
        assert loaded.unhealthy_ticks == 1

    def test_read_settings_reports_whether_the_file_answered(self, tmp_path: Path):
        # The status is what a re-reading consumer needs: defaults from a
        # missing/corrupt file must not be mistaken for a real edit.
        missing = read_settings(tmp_path)
        assert (missing.status, missing.ok) == ("missing", False)
        settings_path(tmp_path).write_text("{not json")
        broken = read_settings(tmp_path)
        assert (broken.status, broken.ok) == ("unreadable", False)
        assert broken.error and broken.settings == AutoSwitchSettings()
        settings_path(tmp_path).write_text("[1, 2]")
        assert read_settings(tmp_path).status == "unreadable"
        settings_path(tmp_path).write_text(
            json.dumps({"schemaVersion": 1, "autoswitch": {"threshold": 80}})
        )
        good = read_settings(tmp_path)
        assert (good.status, good.ok) == ("ok", True)
        assert good.settings.threshold == 80.0

    def test_bad_types_fall_back_to_defaults(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "autoswitch": {"threshold": "high", "includeApiKeyAccounts": 1}
        }))
        loaded = load_settings(tmp_path)
        assert loaded.threshold == AutoSwitchSettings().threshold
        assert loaded.include_api_key_accounts is True

    def test_unsupported_strategy_falls_back_to_best(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "chaos"}})
        )
        assert load_settings(tmp_path).strategy == "best"

    def test_consume_first_is_a_valid_strategy(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"strategy": "consume-first"}})
        )
        assert load_settings(tmp_path).strategy == "consume-first"

    def test_set_strategy_consume_first(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.strategy", "consume-first")
        assert load_settings(tmp_path).strategy == "consume-first"


class TestSaveSettings:
    def test_roundtrip(self, tmp_path: Path):
        custom = AutoSwitchSettings(threshold=85.0, cooldown_seconds=60.0)
        save_settings(tmp_path, custom)
        assert load_settings(tmp_path) == custom

    def test_unknown_keys_survive(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "futureSection": {"x": 1},
            "autoswitch": {"threshold": 80, "futureKnob": True},
        }))
        save_settings(tmp_path, AutoSwitchSettings(threshold=70.0))
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["futureSection"] == {"x": 1}
        assert raw["autoswitch"]["futureKnob"] is True
        assert raw["autoswitch"]["threshold"] == 70.0

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_file_mode_is_0600(self, tmp_path: Path):
        save_settings(tmp_path, AutoSwitchSettings())
        mode = stat.S_IMODE(settings_path(tmp_path).stat().st_mode)
        assert mode == 0o600


class TestUiSettings:
    def test_missing_file_defaults_to_auto(self, tmp_path: Path):
        assert load_ui_settings(tmp_path) == UiSettings(theme="auto")

    def test_reads_auto(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "auto"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_reads_light(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "light"}}))
        assert load_ui_settings(tmp_path).theme == "light"

    def test_unknown_theme_clamps_to_default(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({"ui": {"theme": "purple"}}))
        assert load_ui_settings(tmp_path).theme == "auto"

    def test_set_and_unset_ui_theme(self, tmp_path: Path):
        assert set_setting(tmp_path, "ui.theme", "light") == "light"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "ui": {"theme": "light"}}
        assert unset_setting(tmp_path, "ui.theme") is True
        assert "ui" not in json.loads(settings_path(tmp_path).read_text())

    def test_set_rejects_bad_choice(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="dark, light"):
            set_setting(tmp_path, "ui.theme", "purple")


class TestSettingSpecs:
    def test_registry_covers_every_dataclass_field(self):
        by_section: dict[str, set[str]] = {}
        for spec in SETTING_SPECS.values():
            by_section.setdefault(spec.section, set()).add(spec.field)
        assert by_section["autoswitch"] == {
            f.name for f in AutoSwitchSettings.__dataclass_fields__.values()
        }
        assert by_section["ui"] == {
            f.name for f in UiSettings.__dataclass_fields__.values()
        }

    def test_defaults_match_dataclass(self):
        sources = {"autoswitch": AutoSwitchSettings(), "ui": UiSettings()}
        for spec in SETTING_SPECS.values():
            assert spec.default == getattr(sources[spec.section], spec.field)


class TestSetUnsetSetting:
    def test_set_writes_minimal_file(self, tmp_path: Path):
        value = set_setting(tmp_path, "autoswitch.threshold", "80")
        assert value == 80.0
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw == {"schemaVersion": 1, "autoswitch": {"threshold": 80.0}}

    def test_set_int_kind_coerces_and_rejects_floats(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.unhealthyTicks", "5") == 5
        with pytest.raises(ConfigError, match="integer"):
            set_setting(tmp_path, "autoswitch.unhealthyTicks", "3.5")

    def test_set_rejects_out_of_range_without_writing(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="between 50 and 99.9"):
            set_setting(tmp_path, "autoswitch.threshold", "200")
        assert not settings_path(tmp_path).exists()

    def test_set_rejects_unknown_key(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unknown setting"):
            set_setting(tmp_path, "autoswitch.bogus", "1")

    def test_set_string_kind_round_trips(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.model", "Fable") == "Fable"
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["autoswitch"]["model"] == "Fable"
        assert load_settings(tmp_path).model == "Fable"

    def test_set_string_kind_rejects_empty(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="unset"):
            set_setting(tmp_path, "autoswitch.model", "   ")
        assert not settings_path(tmp_path).exists()

    def test_garbage_model_value_falls_back_to_none(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"model": 123}})
        )
        assert load_settings(tmp_path).model is None

    def test_set_rejects_bool_words_strictly(self, tmp_path: Path):
        assert set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "FALSE") is False
        with pytest.raises(ConfigError, match="true or false"):
            set_setting(tmp_path, "autoswitch.includeApiKeyAccounts", "falsy")

    def test_set_on_corrupt_file_raises_and_preserves_it(self, tmp_path: Path):
        settings_path(tmp_path).write_text("{not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            set_setting(tmp_path, "autoswitch.threshold", "80")
        assert settings_path(tmp_path).read_text() == "{not json"

    def test_unset_removes_key_and_empty_section(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "80")
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert "autoswitch" not in raw

    def test_unset_stamps_schema_version_on_unversioned_file(self, tmp_path: Path):
        settings_path(tmp_path).write_text(
            json.dumps({"autoswitch": {"threshold": 80}})
        )
        assert unset_setting(tmp_path, "autoswitch.threshold") is True
        raw = json.loads(settings_path(tmp_path).read_text())
        assert raw["schemaVersion"] == 1

    def test_unset_absent_key_is_noop(self, tmp_path: Path):
        assert unset_setting(tmp_path, "autoswitch.threshold") is False
        assert not settings_path(tmp_path).exists()


class TestEffectiveSettings:
    def test_missing_file_reports_all_defaults(self, tmp_path: Path):
        rows = effective_settings(tmp_path)
        assert len(rows) == len(SETTING_SPECS)
        assert all(not is_set for _, _, is_set in rows)

    def test_presence_not_value_equality_marks_set(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.threshold", "90")  # equals default
        by_key = {spec.dotted: is_set for spec, _, is_set in effective_settings(tmp_path)}
        assert by_key["autoswitch.threshold"] is True
        assert by_key["autoswitch.intervalSeconds"] is False


class TestCliOverrides:
    """``cli_overrides`` + ``with_overrides`` — the pair ``cswap auto`` uses.

    The flags are kept as a dict and applied separately (rather than merged
    once) because the engine needs them again on every settings reload: a flag
    the user typed outranks the file for the whole run, not just at startup.
    """

    @staticmethod
    def _merged(base: AutoSwitchSettings, args) -> AutoSwitchSettings:
        return with_overrides(base, cli_overrides(args))

    def test_no_flags_returns_settings_unchanged(self):
        base = AutoSwitchSettings(threshold=80.0)
        assert cli_overrides(_args()) == {}
        assert self._merged(base, _args()) is base

    def test_cli_beats_settings(self):
        base = AutoSwitchSettings(threshold=80.0, cooldown_seconds=10.0)
        args = _args(threshold=60.0, interval=30.0)
        assert cli_overrides(args) == {
            "threshold": 60.0, "interval_seconds": 30.0,
        }
        merged = self._merged(base, args)
        assert merged.threshold == 60.0
        assert merged.interval_seconds == 30.0
        assert merged.cooldown_seconds == 10.0  # untouched

    def test_cli_values_are_clamped(self):
        merged = self._merged(AutoSwitchSettings(), _args(interval=1.0))
        assert merged.interval_seconds == 15.0

    def test_boolean_override(self):
        merged = self._merged(
            AutoSwitchSettings(), _args(include_api_key_accounts=True)
        )
        assert merged.include_api_key_accounts is True

    def test_model_override(self):
        merged = self._merged(AutoSwitchSettings(), _args(model="Fable"))
        assert merged.model == "Fable"

    def test_strategy_override(self):
        merged = self._merged(AutoSwitchSettings(), _args(strategy="consume-first"))
        assert merged.strategy == "consume-first"


class TestCon582Specs:
    """The CON-582 threshold knobs are settings.json keys with defaults —
    file parameters, not hardcode."""

    def test_early_swap_threshold_spec(self):
        spec = SETTING_SPECS["autoswitch.earlySwapThreshold"]
        assert spec.field == "early_swap_threshold"
        assert (spec.lo, spec.hi) == (0.0, 99.9)
        assert AutoSwitchSettings().early_swap_threshold == 0.0  # off

    def test_early_swap_max_busy_spec(self):
        spec = SETTING_SPECS["autoswitch.earlySwapMaxBusy"]
        assert spec.field == "early_swap_max_busy"
        assert spec.kind == "int"
        assert AutoSwitchSettings().early_swap_max_busy == 2

    def test_small_context_spec(self):
        spec = SETTING_SPECS["autoswitch.drain2SmallContextTokens"]
        assert spec.field == "drain2_small_context_tokens"
        assert spec.kind == "int"
        assert AutoSwitchSettings().drain2_small_context_tokens == 50_000

    def test_values_round_trip_through_the_file(self, tmp_path: Path):
        set_setting(tmp_path, "autoswitch.earlySwapThreshold", "70")
        set_setting(tmp_path, "autoswitch.earlySwapMaxBusy", "3")
        set_setting(tmp_path, "autoswitch.drain2SmallContextTokens", "80000")
        loaded = load_settings(tmp_path)
        assert loaded.early_swap_threshold == 70.0
        assert loaded.early_swap_max_busy == 3
        assert loaded.drain2_small_context_tokens == 80_000

    def test_out_of_range_values_clamp_on_load(self, tmp_path: Path):
        settings_path(tmp_path).write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {
                "earlySwapThreshold": 200,
                "earlySwapMaxBusy": -1,
                "drain2SmallContextTokens": -5,
            },
        }))
        loaded = load_settings(tmp_path)
        assert loaded.early_swap_threshold == 99.9
        assert loaded.early_swap_max_busy == 0
        assert loaded.drain2_small_context_tokens == 0
