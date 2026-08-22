"""Tool settings persisted at ``<backup_root>/settings.json``.

One versioned JSON file for user-tunable claude-swap preferences, written
atomically with the backup dir's 0600/0700 modes. v1 carries the
``autoswitch`` and ``ui`` sections; other sections can be added additively.
Unknown keys (future fields, other tools' experiments) survive a round trip.

Reading is forgiving — a missing or corrupt file yields defaults with a logged
warning, never a crash — so a bad hand edit degrades to default behavior.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_swap.exceptions import ConfigError

SETTINGS_SCHEMA_VERSION = 1
SETTINGS_FILENAME = "settings.json"

_logger = logging.getLogger("claude-swap")


@dataclass(frozen=True)
class AutoSwitchSettings:
    """Policy knobs for the auto-switch engine (``cswap auto``).

    ``threshold`` is binding-window utilization (max of the 5h/7d percentages):
    at or above it the engine looks for a better account. 90 rather than 95
    leaves margin for the macOS ~30s Keychain pickup tail and for heavy
    subagent turns burning past the mark before a swap lands. A proactive
    candidate must itself sit below the threshold (never land somewhere that
    re-triggers next tick) and beat the active account's utilization by at
    least ``hysteresis_pct``, so two accounts hovering at the line never
    ping-pong while a strictly better account is always taken.
    """

    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    strategy: str = "best"  # "best" (most headroom) or "consume-first" (soonest weekly reset)
    include_api_key_accounts: bool = False
    unhealthy_ticks: int = 3
    # Let the at-threshold (proactive) switch land while sessions are running,
    # instead of waiting for the 5-minute transcript-silence gate. An
    # unattended fleet never goes quiet, so the gate would hold the swap until
    # the account hit the wall and only the at-limit escape got out — after
    # in-flight agents already failed on the limit. Costs the prompt caches of
    # live sessions on the account being left; leave off for interactive work.
    # The below-threshold consume-first rotation stays gated either way.
    switch_under_load: bool = False
    # Bounded wait ("drain") before a FORCED switch — failover, and proactive
    # under switchUnderLoad — lands while sessions are running: the engine
    # holds the swap, re-checking every tick, until transcripts have been
    # quiet for the cache window, and at this many seconds swaps anyway with
    # a warning (an account pinned at its limit breaks live agents harder
    # than the cache miss does). An at-limit switch skips the wait outright
    # (CON-486): its binding window is at 100%, calls on the account are
    # already failing, so there is no cache left for the wait to protect.
    # 0 = swap immediately (no drain).
    drain_timeout_seconds: float = 0.0
    # Drain v2 (active checkpoint) for the proactive-under-load switch: the
    # engine SIGNALS every mid-turn background session to checkpoint and
    # freeze (via a headless herald session sending SendMessage), waits until
    # the roster shows each one at a turn boundary — machine confirmation,
    # not a timer — swaps, verifies the new account answers, then signals
    # "resume". This many seconds bounds the wait for fixation; sessions
    # still mid-turn at the cap are swapped under honestly-counted force.
    # 0 = drain v2 off (forced proactive switches use the passive
    # drainTimeoutSeconds wait above, exactly as before).
    drain2_wait_seconds: float = 0.0
    # Early swap on a small park (CON-582). The migration price of a swap is
    # the sum of the live contexts on the account being left — prompt caches
    # are per-organization, so every running session re-creates its whole
    # context at full price after the move — and that sum grows with the
    # park. At/above this binding-window pct (but still below ``threshold``)
    # a proactive switch may fire early when at most ``early_swap_max_busy``
    # sessions are mid-turn: moving two contexts now is strictly cheaper
    # than moving twelve at the threshold (measured 15-08: 10.2M cache-write
    # tokens for one at-threshold swap under a full park). 0 = off.
    early_swap_threshold: float = 0.0
    # How many mid-turn sessions still count as a small park.
    early_swap_max_busy: int = 2
    # Drain v2 wave composition (CON-582): a mid-turn session whose
    # transcript shows a context at/below this many tokens is left running
    # through the swap instead of being checkpointed — its post-swap cache
    # re-create is pocket change next to the checkpoint ceremony (commit,
    # TaskList sweep, receipt, resume) and the wall-clock it would add to
    # the pause. 0 = checkpoint every mid-turn session.
    drain2_small_context_tokens: int = 50_000
    # Comma-separated model display name(s) (e.g. "Fable" or "Fable,Opus"),
    # or "all" for every scoped window an account reports. Each named model's
    # per-model weekly limit is folded into the binding window, so the engine
    # switches off an account whose model quota is exhausted even while its
    # 5h/7d windows still have headroom. None = account-wide 5h/7d only
    # (default). Under the consume-first strategy the named windows bind
    # only the at-limit escape and escape landings; voluntary moves judge
    # the account-wide 5h/7d axis and prefer model-burned hosts, so the
    # rotation never hoards a model-fresh account the fleet's model-pinned
    # work needs (pool-shield, CON-712).
    model: str | None = None
    # Slot number or email the live login is pinned to (CON-1070). With a
    # home the daemon never moves the login off it on its own — threshold,
    # consume-first, the early swap and even a maxed window all hold; a dead
    # token (failover) is the only escape — and, away from home, returns as
    # soon as the home slot proves alive (readable usage), ignoring the
    # cooldown. A disabled or unknown home leaves the pin inert with one
    # warning. None = plain rotation (default).
    home_account: str | None = None


@dataclass(frozen=True)
class UiSettings:
    """Appearance preferences (``ui`` section). ``theme`` selects the TUI/CLI
    color theme; ``auto`` follows terminal-background detection."""

    theme: str = "auto"


_SECTION_DEFAULT_SOURCES = {"autoswitch": AutoSwitchSettings, "ui": UiSettings}


@dataclass(frozen=True)
class SettingSpec:
    """Metadata for one user-tunable settings.json key.

    Single source of truth for bounds/choices: both the lenient clamp on load
    (`_clamped`) and the strict validation in `cswap config set`
    (`parse_setting_value`) read from here, so the two can't drift.
    """

    section: str  # top-level JSON section ("autoswitch", "ui")
    json_key: str  # camelCase key inside the section
    field: str  # snake_case AutoSwitchSettings field
    kind: str  # "float" | "int" | "bool" | "choice"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.json_key}"

    @property
    def default(self):
        return getattr(_SECTION_DEFAULT_SOURCES[self.section](), self.field)


# settings.json uses camelCase (matching the repo's other JSON artifacts);
# dataclass fields stay snake_case.
SETTING_SPECS: dict[str, SettingSpec] = {
    spec.dotted: spec
    for spec in (
        SettingSpec(
            "autoswitch", "threshold", "threshold", "float", 50.0, 99.9,
            help="Switch when the binding 5h/7d window reaches this pct",
        ),
        SettingSpec(
            "autoswitch", "intervalSeconds", "interval_seconds", "float", 15.0, 3600.0,
            help="Poll interval for the cswap auto loop, in seconds",
        ),
        SettingSpec(
            "autoswitch", "cooldownSeconds", "cooldown_seconds", "float", 0.0, 86400.0,
            help="Minimum seconds between proactive switches",
        ),
        SettingSpec(
            "autoswitch", "hysteresisPct", "hysteresis_pct", "float", 0.0, 50.0,
            help="A target must beat the active account by this many pct",
        ),
        SettingSpec(
            "autoswitch", "strategy", "strategy", "choice",
            choices=("best", "consume-first"),
            help="How auto-switch picks the target account",
        ),
        SettingSpec(
            "autoswitch", "includeApiKeyAccounts", "include_api_key_accounts", "bool",
            help="Allow rotating onto managed API-key accounts (bill per token)",
        ),
        SettingSpec(
            "autoswitch", "unhealthyTicks", "unhealthy_ticks", "int", 1, 100,
            help="Consecutive failed polls before an account is unhealthy",
        ),
        SettingSpec(
            "autoswitch", "switchUnderLoad", "switch_under_load", "bool",
            help="Let the at-threshold switch land while sessions are running",
        ),
        SettingSpec(
            "autoswitch", "drainTimeoutSeconds", "drain_timeout_seconds",
            "float", 0.0, 86400.0,
            help="Max seconds a forced switch waits for session silence (0 = don't wait)",
        ),
        SettingSpec(
            "autoswitch", "drain2WaitSeconds", "drain2_wait_seconds",
            "float", 0.0, 3600.0,
            help=(
                "Drain v2: max seconds to wait for signaled sessions to "
                "checkpoint before a proactive switch (0 = v2 off)"
            ),
        ),
        SettingSpec(
            "autoswitch", "earlySwapThreshold", "early_swap_threshold",
            "float", 0.0, 99.9,
            help=(
                "Swap early from this binding-window pct while few sessions "
                "are busy (0 = off)"
            ),
        ),
        SettingSpec(
            "autoswitch", "earlySwapMaxBusy", "early_swap_max_busy",
            "int", 0, 1000,
            help=(
                "Most busy sessions that still count as a small park for "
                "the early swap"
            ),
        ),
        SettingSpec(
            "autoswitch", "drain2SmallContextTokens",
            "drain2_small_context_tokens", "int", 0, 100_000_000,
            help=(
                "Leave sessions whose context is at/below this many tokens "
                "running through a swap (0 = checkpoint all)"
            ),
        ),
        SettingSpec(
            "autoswitch", "model", "model", "string",
            help="Also switch on these models' weekly limits (e.g. Fable, Fable,Opus, or all)",
        ),
        SettingSpec(
            "autoswitch", "homeAccount", "home_account", "string",
            help=(
                "Pin the live login to this slot (num or email): auto-switch "
                "only leaves it on a dead token and returns once it reads again"
            ),
        ),
        SettingSpec(
            "ui", "theme", "theme", "choice", choices=("dark", "light", "auto"),
            help="Color theme; auto follows the terminal background",
        ),
    )
}

_AUTOSWITCH_KEYS: dict[str, str] = {
    spec.field: spec.json_key
    for spec in SETTING_SPECS.values()
    if spec.section == "autoswitch"
}


def settings_path(backup_root: Path) -> Path:
    return backup_root / SETTINGS_FILENAME


def parse_model_names(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated model list, trimmed and case-insensitively
    deduped (first spelling wins). Shared by the auto engine and the manual
    switch strategies so both read ``autoswitch.model`` identically."""
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return tuple(seen.values())


def _clamped(settings: AutoSwitchSettings) -> AutoSwitchSettings:
    """Clamp values into the SETTING_SPECS ranges; bad types → the default."""

    def num(value, default: float, lo: float, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(min(max(value, lo), hi))

    kwargs = {}
    for spec in SETTING_SPECS.values():
        if spec.section != "autoswitch":
            continue
        value = getattr(settings, spec.field)
        if spec.kind in ("float", "int"):
            clamped = num(value, spec.default, spec.lo, spec.hi)
            kwargs[spec.field] = int(clamped) if spec.kind == "int" else clamped
        elif spec.kind == "bool":
            kwargs[spec.field] = bool(value)
        elif spec.kind == "string":
            # A non-empty string keeps as-is; anything else reverts to default
            # (None) so a null/garbage settings.json value disables the filter.
            # A bare JSON number is the natural hand-written form of a slot
            # number (``"homeAccount": 32``) and reads as its string; for
            # every other string key a number is garbage as before.
            if (
                spec.json_key == "homeAccount"
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                value = str(value)
            kwargs[spec.field] = value if isinstance(value, str) and value else spec.default
        else:  # choice
            if value not in spec.choices:
                _logger.warning(
                    "settings.json: unsupported %s %r; using %r",
                    spec.dotted, value, spec.default,
                )
                value = spec.default
            kwargs[spec.field] = value
    return AutoSwitchSettings(**kwargs)


def _read_raw_checked(path: Path) -> tuple[dict, str, str]:
    """``(raw, status, error)`` — status is "ok" | "missing" | "unreadable".

    The status is what separates "the file says default" from "the file did
    not answer". Callers that read once may ignore it (defaults are the right
    degradation there); a caller that re-reads a file it is already running on
    must not, or a truncated write silently rewrites a live policy.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "missing", "no such file"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return {}, "unreadable", str(e)
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return {}, "unreadable", "not a JSON object"
    return raw, "ok", ""


def _read_raw(path: Path) -> dict:
    return _read_raw_checked(path)[0]


@dataclass(frozen=True)
class SettingsRead:
    """One settings.json read, with the file's own status attached.

    ``ok`` is False when the file is missing or could not be parsed; the
    settings are then the plain defaults, which is a fine answer for a
    one-shot read and a lie for a re-read.
    """

    settings: AutoSwitchSettings
    status: str  # "ok" | "missing" | "unreadable"
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def read_settings(backup_root: Path) -> SettingsRead:
    """Load the autoswitch section, reporting whether the file answered."""
    raw, status, error = _read_raw_checked(settings_path(backup_root))
    return SettingsRead(_autoswitch_from_raw(raw), status, error)


def _autoswitch_from_raw(raw: dict) -> AutoSwitchSettings:
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        return AutoSwitchSettings()
    kwargs = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        if json_key in section:
            kwargs[field] = section[json_key]
    try:
        settings = AutoSwitchSettings(**kwargs)
    except TypeError:
        settings = AutoSwitchSettings()
    return _clamped(settings)


def load_settings(backup_root: Path) -> AutoSwitchSettings:
    """Load the autoswitch section; missing/corrupt file or fields → defaults."""
    return read_settings(backup_root).settings


def load_ui_settings(backup_root: Path) -> UiSettings:
    """Load the ui section; missing/corrupt file or unknown theme → default."""
    raw = _read_raw(settings_path(backup_root))
    section = raw.get("ui")
    default = UiSettings()
    if not isinstance(section, dict):
        return default
    theme = section.get("theme", default.theme)
    if theme not in SETTING_SPECS["ui.theme"].choices:
        _logger.warning(
            "settings.json: unsupported ui.theme %r; using %r",
            theme, default.theme,
        )
        return default
    return UiSettings(theme=theme)


def save_settings(backup_root: Path, settings: AutoSwitchSettings) -> None:
    """Write the autoswitch section, preserving unknown keys and sections."""
    path = settings_path(backup_root)
    raw = _read_raw(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get("autoswitch")
    if not isinstance(section, dict):
        section = {}
    for field, json_key in _AUTOSWITCH_KEYS.items():
        section[json_key] = getattr(settings, field)
    raw["autoswitch"] = section
    atomic_write_json(path, raw)


def setting_spec(dotted_key: str) -> SettingSpec:
    """Look up a spec by dotted key; unknown keys raise with the valid list."""
    spec = SETTING_SPECS.get(dotted_key)
    if spec is None:
        raise ConfigError(
            f"unknown setting '{dotted_key}'\n"
            f"Valid keys: {', '.join(SETTING_SPECS)}"
        )
    return spec


_BOOL_WORDS = {
    "true": True, "1": True, "yes": True,
    "false": False, "0": False, "no": False,
}


def parse_setting_value(spec: SettingSpec, raw_value: str):
    """Strictly parse a CLI-provided string for `cswap config set`.

    Unlike the forgiving clamp on load, out-of-range or mistyped values raise
    ConfigError so the user learns about the problem when setting the value,
    not by silently degraded behavior at `cswap auto` time.
    """
    if spec.kind == "bool":
        # Never bool(str): bool("false") is True.
        parsed = _BOOL_WORDS.get(raw_value.strip().lower())
        if parsed is None:
            raise ConfigError(
                f"{spec.dotted} expects true or false (or 1/0, yes/no), "
                f"got '{raw_value}'"
            )
        return parsed
    if spec.kind == "choice":
        if raw_value not in spec.choices:
            raise ConfigError(
                f"{spec.dotted} must be one of: {', '.join(spec.choices)}"
            )
        return raw_value
    if spec.kind == "string":
        value = raw_value.strip()
        if not value:
            raise ConfigError(
                f"{spec.dotted} expects a non-empty value; use "
                f"'cswap config unset {spec.dotted}' to clear it"
            )
        return value
    try:
        value = int(raw_value) if spec.kind == "int" else float(raw_value)
    except ValueError:
        noun = "an integer" if spec.kind == "int" else "a number"
        raise ConfigError(
            f"{spec.dotted} expects {noun}, got '{raw_value}'"
        ) from None
    if not spec.lo <= value <= spec.hi:
        raise ConfigError(
            f"{spec.dotted} must be between {format_setting_value(spec.lo)} "
            f"and {format_setting_value(spec.hi)}"
        )
    return value


def format_setting_value(value) -> str:
    """Render a settings value the way settings.json writes it."""
    if value is None:
        return "(none)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_raw_for_write(path: Path) -> dict:
    """Raw read for the config write path: a corrupt file errors, never {}.

    ``_read_raw``'s degrade-to-defaults is right for reads, but a
    read-modify-write starting from ``{}`` would replace a malformed (and
    maybe hand-recoverable) file with a near-empty one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"could not read {path}: {e}") from e
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON ({e}); fix or delete it before "
            "changing settings"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path} is not a JSON object; fix or delete it before "
            "changing settings"
        )
    return raw


def set_setting(backup_root: Path, dotted_key: str, raw_value: str):
    """Validate and persist one key for `cswap config set`; returns the value.

    Writes only the given key (plus schemaVersion) — deliberately not
    ``save_settings``, which writes every known key and would freeze the
    current defaults into the file, pinning users to them if a later version
    changes a default. Unknown keys and sections in the file survive.
    """
    spec = setting_spec(dotted_key)
    value = parse_setting_value(spec, raw_value)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    section = raw.get(spec.section)
    if not isinstance(section, dict):
        section = {}
    section[spec.json_key] = value
    raw[spec.section] = section
    atomic_write_json(path, raw)
    return value


def unset_setting(backup_root: Path, dotted_key: str) -> bool:
    """Remove one key from settings.json; False if it wasn't set (no write)."""
    spec = setting_spec(dotted_key)
    path = settings_path(backup_root)
    raw = _read_raw_for_write(path)
    section = raw.get(spec.section)
    if not isinstance(section, dict) or spec.json_key not in section:
        return False
    raw["schemaVersion"] = raw.get("schemaVersion", SETTINGS_SCHEMA_VERSION)
    del section[spec.json_key]
    if not section:
        del raw[spec.section]
    atomic_write_json(path, raw)
    return True


def effective_settings(backup_root: Path) -> list[tuple[SettingSpec, object, bool]]:
    """(spec, effective value, explicitly set?) per key, in registry order.

    "Set" means the key is present in the raw file — an explicit value equal
    to the default still counts — so `cswap config`'s "(default)" marker
    reflects the file, not value equality.
    """
    raw = _read_raw(settings_path(backup_root))
    loaded = {
        "autoswitch": load_settings(backup_root),
        "ui": load_ui_settings(backup_root),
    }
    rows = []
    for spec in SETTING_SPECS.values():
        section = raw.get(spec.section)
        is_set = isinstance(section, dict) and spec.json_key in section
        rows.append((spec, getattr(loaded[spec.section], spec.field), is_set))
    return rows


# argparse dest → AutoSwitchSettings field, for the `cswap auto` flags that
# override the file.
_CLI_OVERRIDE_FIELDS = (
    ("threshold", "threshold"),
    ("interval", "interval_seconds"),
    ("cooldown", "cooldown_seconds"),
    ("include_api_key_accounts", "include_api_key_accounts"),
    ("model", "model"),
    ("strategy", "strategy"),
    ("home", "home_account"),
)


def cli_overrides(args) -> dict[str, object]:
    """Fields the user pinned with an explicit ``cswap auto`` flag.

    Returned separately from the merged result so a long-running consumer can
    keep honoring the flag while re-reading the file underneath it: an
    explicit flag outranks settings.json for the whole run, not just at
    startup.
    """
    overrides: dict[str, object] = {}
    for attr, field in _CLI_OVERRIDE_FIELDS:
        value = getattr(args, attr, None)
        if value is not None:
            overrides[field] = value
    return overrides


def with_overrides(
    settings: AutoSwitchSettings, overrides: dict[str, object]
) -> AutoSwitchSettings:
    """Overlay explicit overrides onto settings (clamped like a file load)."""
    if not overrides:
        return settings
    return _clamped(dataclasses.replace(settings, **overrides))


def atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON with the backup dir's 0600/0700 modes.

    Shared by settings.json and the autoswitch state file (and any future
    machine-local state files beside them).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path.parent, 0o700)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(path))
        if sys.platform != "win32":
            os.chmod(str(path), 0o600)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
