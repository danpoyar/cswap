"""Auto-switch engine: poll usage, switch accounts before they hit rate limits.

``AutoSwitchEngine`` is UI-agnostic — no printing, no argparse, no TUI
imports. It composes a :class:`ClaudeAccountSwitcher`, evaluates a threshold
policy each :meth:`~AutoSwitchEngine.tick`, and reports everything through
typed events handed to an ``on_event`` callback; the CLI renders them as
human lines or JSONL, and any future frontend (TUI dashboard, menubar) can
consume the same stream.

Policy in one paragraph: when the active account's *binding window* (the
higher of its 5h/7d utilization) crosses ``settings.threshold``, switch to
the candidate with the most headroom — proactively, so the old account is
still valid while a running Claude Code picks the new one up (this is what
makes the macOS ~30s Keychain cache latency harmless). Candidates must sit
``hysteresis_pct`` below the threshold so two accounts hovering at the line
never ping-pong, and a ``cooldown_seconds`` floor bounds the switch rate
(bypassed only when the active account is hard at its limit). Voluntary
switches also wait for session-traffic silence (the quiet gate): every org
has its own prompt-cache namespace, so a swap under live traffic full-misses
the next turn of every running session — proactive switches are held until
no ``~/.claude/projects/**/*.jsonl`` transcript has been written for
``QUIET_WINDOW_S`` (by then those caches have expired on their own);
at-limit and failover are escapes and ignore the gate. Before
activation the target's token is *freshened* (refreshed if it expires within
10 minutes — twice Claude Code's refresh buffer, so a running Claude Code's
under-lock re-read sees a fresh token and aborts its own refresh); a target
whose refresh token is dead gets quarantined instead of activated. When the
active account's own usage becomes unreadable for ``unhealthy_ticks``
consecutive ticks, the engine fails over to any healthy candidate.

Cooldown and quarantine persist in ``<backup_root>/autoswitch_state.json``
(so cron-driven ``cswap auto --once`` ticks behave across processes), mutated
read-modify-write under a dedicated file lock.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from claude_swap import context_cost, oauth, paths, poll_policy
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.park import ParkChannel, ParkSession, WaveResult
from claude_swap.process_detection import pids_with_config_dir
from claude_swap.session import session_dir_for
from claude_swap.json_output import SCHEMA_VERSION, USAGE_TOKEN_EXPIRED
from claude_swap.locking import FileLock
from claude_swap.poll_policy import (
    ESCALATION_MARGIN_PCT,
    RESET_SLACK_S,
    binding_pct,
)
from claude_swap.settings import (
    AutoSwitchSettings,
    atomic_write_json,
    parse_model_names,
    read_settings,
    settings_path,
    with_overrides,
)
from claude_swap.adopt_snapshot import default_adopt_snapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import due_candidate, plan_oversleeps_interval

STATE_FILENAME = "autoswitch_state.json"
STATE_SCHEMA_VERSION = 1

_logger = logging.getLogger("claude-swap")

# Freshen targets whose access token expires within this window: twice Claude
# Code's own 5-minute refresh buffer, so its post-lock "abort refresh if not
# expired" re-read holds with margin after our swap.
FRESHEN_BUFFER_MS = 10 * 60 * 1000

# Sleep caps around a known quota reset (RESET_SLACK_S lives in poll_policy
# with the rest of the cadence numbers). Recheck at the exhausted-account poll
# cadence: providers can grant quota before the previously reported reset, and
# a long engine sleep must not suppress the fetch that discovers it.
MAX_SLEEP_S = poll_policy.EXHAUSTED_INTERVAL_S
NO_RESET_FALLBACK_S = 300.0

# Idle-hold cap (elapsed, not ticks — the hold itself slows the cadence to
# NO_RESET_FALLBACK_S): an owned-and-expired token normally means Claude Code
# is idle and will self-heal on next use, but a *dead* refresh token with an
# active user would look identical forever, so after this long the engine
# falls back to normal unhealthy counting.
IDLE_HOLD_MAX_S = 30 * 60.0

# Quiet gate for voluntary switches. Prompt caches are per-organization, so a
# switch under live session traffic full-misses the next turn of every running
# session (measured: 47/64 FULL-MISS turns landed within ±2 min of a switch).
# A session transcript under ~/.claude/projects/ is appended on every turn —
# main sessions and workflow subagents alike — so "newest *.jsonl mtime older
# than the 5-minute cache TTL" means every cache a swap could burn has already
# expired. Voluntary (proactive/consume-first) switches wait for that silence;
# at-limit and failover switches are forced and skip the gate.
QUIET_WINDOW_S = 5 * 60.0

# Drain gate for forced switches (the ones the voluntary gate does not hold).
# A drain episode lives in the state file so cron `--once` ticks share it; a
# record whose last busy observation is older than this belongs to an episode
# that ended without a switch (the forcing condition went away, or the engine
# was down) and must not count as already-waited time. Two consecutive
# observations of a live episode are never farther apart than the longest
# engine sleep (MAX_SLEEP_S), with the same again as slack.
DRAIN_STALE_GAP_S = 2 * MAX_SLEEP_S

# Drain v2 (active checkpoint) numbers. The STOP wave tells every signaled
# session to finish its current step, checkpoint (ack receipt + one
# self-waking background watch on the swap marker), and end its turn. A
# resume wave older than the self-rescue window would only wake sessions
# that already resumed themselves, so a "swapped" episode past it is closed
# without a wave. Post-swap the engine verifies the new account answers
# (usage fetch) at most this many attempts before resuming regardless: a
# park frozen on a dead verify costs more than an optimistic resume.
DRAIN2_SELF_RESCUE_S = 600.0
DRAIN2_VERIFY_ATTEMPTS = 2
# In-process backoff after a channel failure: without it a broken herald
# (worst case: a 120s spawn timeout) would stall every drain tick. Cron
# ``--once`` processes don't share it — a one-per-minute failing retry is
# harmless there.
DRAIN2_BACKOFF_S = 600.0
# Agent-facing files under the backup dir, absolute paths baked into the
# wave text. The marker holds the integer epoch of the last real switch —
# the watch target of the agents' one background wait (and the "swap already
# happened, don't freeze" guard for late freezers: the wave carries the
# signal epoch to compare against). The ack dir holds per-session receipt
# files: a session that checkpointed but keeps a background task running
# looks ``busy`` in the roster forever (episode 14-08: 4 of 6 "forced" had
# checkpointed before the cap — CON-451), so the receipt is what lets the
# fixation judge see through that.
DRAIN2_SWITCH_MARKER_NAME = "drain2-last-switch"
DRAIN2_ACK_DIR_NAME = "drain2-ack"
# Fixation proof (CON-461). The receipt is PRIMARY: the roster's "not busy"
# can be a sub-second turn-boundary blip between two tool calls, and one
# poll landing in it reads a hard-working session as checkpointed (episode
# 14-08 16:41–16:44Z: fix-age-267 wrote 53 transcript records without a
# pause, one blip poll filed it "fixed", and the account switched under its
# live turn). A session with no receipt soft-fixes only after this many
# CONSECUTIVE not-busy polls. At the gate's poll spacing (the ~30s tick
# interval; 60s for cron --once) 3 polls are a ≥60s sustained turn
# boundary, which no sub-second blip can fake: at the episode's own cadence
# (a tool round per ~3s) even a generous 10%-per-poll blip chance compounds
# to ~0.1%, and 3 — unlike 2 — also breaks the phase-lock of an agent whose
# ~30s tool rounds could land every poll on a boundary. Cost side: a
# genuinely parked-but-receiptless session still soft-confirms in ~90s,
# inside the 180s production cap, and compliant sessions ack and fix
# instantly — the streak only prices non-compliant or dead ones.
DRAIN2_SOFT_FIX_POLLS = 3
# The streak's "consecutive" is a time claim, so it needs a floor (review
# r1 finding 2): wake()/TUI edits and settings.json changes slice the
# inter-tick sleep, and three rapid glances seconds apart would collect a
# "streak" inside ONE stretched turn boundary of a working session. A
# not-busy observation closer than this to the episode's previous gate
# poll neither grows nor resets the streak; a busy observation resets at
# any spacing — work is proof at any distance. 20s sits under the ~30s
# tick interval (with its −10% jitter) so every normally-cadenced poll
# still counts.
DRAIN2_SOFT_FIX_MIN_GAP_S = 20.0

# Wave texts. The park's agents work in Russian; industry terms stay
# English. ``DRAIN2_STOP_MESSAGE`` is a template — the engine bakes in the
# marker/ack paths and the wave's epoch (``_drain2_stop_message``). The
# protocol is ONE wait channel (the first live episode produced four
# different ones — CON-451 class 3): checkpoint → receipt → one
# run_in_background watch on the marker → end the turn. The nested-run stop
# names async Task/Agent subagents explicitly and judges by "spends tokens",
# not by type: chore-ops-340 kept its code-reviewer running through the
# whole episode on the reasoning "лёгкий разовый вызов, не Workflow-прогон".
# The stop is a machine loop over TaskList, not prose over memory, and the
# receipt is gated on the emptied list (CON-486): feat-sto-2 stopped the
# one builder it remembered and receipted while the builder's own grandchild
# reviewer — invisible to memory, spawned mid-episode — burned the window
# to 100%. A re-run TaskList also catches orphans the harness re-parents
# after their spawner dies.
DRAIN2_STOP_MESSAGE = (
    "cswap drain: аккаунт парка у порога, готовится переключение — парк "
    "уходит на короткую паузу.\n"
    "1. Доведи ТЕКУЩИЙ ШАГ, не кусок целиком. Закоммить свои файлы по "
    "явным путям; точку остановки — одной строкой комментом в свой тикет.\n"
    "2. Останови вложенные прогоны МАШИННО, не по памяти: вызови TaskList "
    "и каждому фоновому прогону из списка, который тратит токены "
    "(Workflow-ран, субагент Task/Agent, включая code-reviewer; «лёгкий "
    "разовый вызов» — тоже такой прогон), — TaskStop по id из списка; "
    "runId/scriptPath/agentId запиши на диск рядом с работой, чтобы "
    "возобновить ТОГО ЖЕ после паузы. Синхронного субагента не убивай — "
    "дождись его конца, это и есть тишина. Локальные процессы без "
    "API-вызовов (тесты, сборки) не трогай.\n"
    "3. Квитанцию ставь ТОЛЬКО когда повторный TaskList показывает: "
    "токен-тратящих прогонов не осталось (локальные Bash-задачи не в счёт) "
    "— стоп по памяти вместо списка уже терял внука-ревьюера, спавнутого "
    "субагентом, и он дожёг окно до 100%. По квитанции cswap видит твою "
    "готовность и свопнет раньше: "
    'mkdir -p "{ack_dir}" && touch "{ack_dir}/<имя твоей сессии>"\n'
    "4. Поставь ОДНО фоновое ожидание конца свопа инструментом Bash с "
    "run_in_background: true — этот канал сам разбудит тебя (голый nohup/& "
    "запрещён): timeout 720 bash -c 'until [ \"$(cat \"{marker}\" "
    "2>/dev/null || echo 0)\" -ge {signal_epoch} ] 2>/dev/null; do sleep "
    "10; done'; echo cswap-swap-done\n"
    "   Если оно завершилось сразу — своп уже прошёл: не замирай, просто "
    "продолжай работу.\n"
    "5. Заверши ход и ничего не начинай: разбудит твоё же ожидание (своп "
    "или кап 12 минут) или сигнал cswap drain-resume — продолжай сам, "
    "второго разрешения не жди. Не смог поставить ожидание — просто заверши "
    "ход: resume придёт, а если тишина дольше 10 минут — продолжай сам.\n"
    "Отвечать на это сообщение не нужно: Jerry — одноразовый герольд, он "
    "уже погас."
)
DRAIN2_RESUME_MESSAGE = (
    "cswap drain-resume: аккаунт переключён, пауза кончилась — продолжай "
    "работу с места остановки. Если ты уже продолжил сам — считай это "
    "подтверждением, повторно ничего не делай."
)
# The honest wave for an episode released WITHOUT a swap (CON-461, live
# hole 14-08 17:10–17:13Z): telling a parked agent "аккаунт переключён"
# when no switch happened would be a lie it might act on.
DRAIN2_RELEASE_MESSAGE = (
    "cswap drain-resume: своп НЕ случился (переключаться некуда) — пауза "
    "отменена, продолжай работу с места остановки на текущем аккаунте. "
    "Если ты уже продолжил сам — считай это подтверждением, повторно "
    "ничего не делай."
)

# How often a sleeping loop re-stats settings.json. Every tick re-reads the
# file, but a BLOCKED/all-exhausted tick then sleeps up to MAX_SLEEP_S — and a
# settings change (say, folding a per-model weekly quota into the decision)
# honored ten minutes later is indistinguishable to a user from "you must
# restart the daemon". One stat per slice; the loop wakes on the first change.
SETTINGS_WATCH_S = 5.0


def latest_session_activity_ts(projects_dir: Path) -> float | None:
    """Newest mtime among session transcripts (``<projects_dir>/**/*.jsonl``),
    or None when there are none (or the directory doesn't exist).

    ``os.walk`` swallows unreadable directories and a per-file ``stat`` race
    is skipped: the gate must degrade toward "assume quiet" only when there
    is provably nothing to read, never crash a tick.
    """
    latest: float | None = None
    for dirpath, _dirnames, filenames in os.walk(projects_dir):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            try:
                mtime = os.stat(os.path.join(dirpath, name)).st_mtime
            except OSError:
                continue
            if latest is None or mtime > latest:
                latest = mtime
    return latest


# Adaptive scheduling: the baseline request volume is O(1) per tick — the
# active account plus ONE due candidate (stalest data first) — instead of
# every account in parallel, and the per-account cadence itself (movement,
# threshold distance, urgent mode, 429 recovery) lives in poll_policy, is
# persisted in the usage store by whichever collector fetched, and is shared
# by every surface. The engine escalates to a full candidate refresh only
# when a switch could actually be near: active utilization within
# ESCALATION_MARGIN_PCT of the threshold, or active usage unknown (failover
# needs fresh candidate data). The consume-first trigger can fire outside
# that escalation band; there it decides provisionally on the stored
# snapshot and escalates at commit time, when a switch would actually fire
# (the two-phase commit in _tick_inner).


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def pct_label(value: float) -> str:
    """A percentage for display, as configured: 85.555555 stays itself
    (never a rounded "85.5556") and 99.9 never becomes a lying "100" the
    way ``.0f`` renders it. Ten significant digits still absorb IEEE float
    noise (~15th digit) in computed utilizations (100.0 - headroom).
    Displayed comparisons must format BOTH sides with this helper — mixing
    formatters can render an impossible "85.5556% < 85.555555%"."""
    return f"{value:.10g}"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoSwitchEvent:
    """Base event. ``to_json()`` payloads are additive: consumers must ignore
    unknown ``event`` kinds and unknown fields."""

    kind: ClassVar[str] = "event"
    ts: str = field(default_factory=_now_iso, kw_only=True)

    def _fields(self) -> dict:
        return {}

    def to_json(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "event": self.kind,
            "ts": self.ts,
            **self._fields(),
        }

    def human(self) -> str:  # pragma: no cover - overridden
        return self.kind


@dataclass(frozen=True)
class PollEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "poll"
    active: dict | None  # account_ref shape, or None
    headroom: dict[str, float | None]  # account number → headroom pct (None=unknown)
    threshold: float
    # account number → last fetch-error cause ("http-429", "timeout", ...) for
    # accounts whose usage is unknown this tick. Additive field.
    fetch_errors: dict[str, str] = field(default_factory=dict)
    # account number → ordered window label → utilization pct ("5h", "7d",
    # then scoped model display names). Additive field: the binding pct alone
    # (e.g. "89%") hides which window binds — #115 was reported off that
    # ambiguity.
    windows: dict[str, dict[str, float]] = field(default_factory=dict)

    def _fields(self) -> dict:
        fields = {
            "active": self.active,
            "headroomPct": self.headroom,
            "threshold": self.threshold,
        }
        if self.fetch_errors:
            fields["fetchErrors"] = self.fetch_errors
        if self.windows:
            fields["windowsPct"] = self.windows
        return fields

    def _describe(self, num: str) -> str:
        wins = self.windows.get(num)
        if wins:
            return " · ".join(f"{name} {pct:.0f}%" for name, pct in wins.items())
        h = self.headroom.get(num)
        if h is not None:
            return f"{100 - h:.0f}%"
        err = self.fetch_errors.get(num)
        return f"? ({err})" if err else "?"

    def human(self) -> str:
        if self.active is None:
            return "poll: no active account"
        num = self.active.get("number")
        h = self.headroom.get(str(num))
        if h is not None:
            used = f"{100 - h:.0f}% used"
        else:
            err = self.fetch_errors.get(str(num))
            used = f"usage unknown ({err})" if err else "usage unknown"
        others = ", ".join(
            f"#{n}: {self._describe(n)}"
            for n in self.headroom
            if n != str(num)
        )
        tail = f" | others: {others}" if others else ""
        return (
            f"Account-{num} ({self.active.get('email')}): {used} "
            f"(switch at {pct_label(self.threshold)}%){tail}"
        )


@dataclass(frozen=True)
class SwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "switch"
    # "proactive" | "at-limit" | "failover" | "consume-first" | "return-home"
    trigger: str
    from_ref: dict | None
    to_ref: dict | None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    # Session-traffic state at the moment of the swap, so the cache damage per
    # switch is measurable from the log alone: "quiet" = no transcript write
    # for QUIET_WINDOW_S (live prompt caches already expired — the swap burned
    # nothing); "forced" = live traffic existed but the switch had to happen
    # anyway. Triggers held by the quiet gate (_gated_triggers) can only ever
    # log "quiet" — the gate blocks them otherwise. Additive field.
    gate: str = "forced"
    # Set when a drain episode preceded this switch (the forced-switch bounded
    # wait for silence): {"outcome": "go"|"timeout", "waitedSeconds": int}.
    # "go" = silence arrived within the ceiling, "timeout" = the ceiling hit
    # and the swap went through under load. Absent when the swap needed no
    # wait. Additive field.
    drain: dict | None = None
    # Set when a drain v2 (active checkpoint) episode preceded this switch:
    # {"outcome": "ready"|"timeout", "waitedSeconds": int, "fixed": int,
    # "forced": int}. "ready" = every signaled session reached a turn
    # boundary; "timeout" = the fixation cap hit and ``forced`` sessions were
    # swapped mid-turn — the honest waited/torn count in one place. Additive.
    drain2: dict | None = None
    # True when this proactive switch fired below the threshold off the
    # small-park early trigger (CON-582) — the burn report correlates the
    # migration price against it. Additive field, present only when True.
    early: bool = False

    def _fields(self) -> dict:
        fields = {
            "trigger": self.trigger,
            "from": self.from_ref,
            "to": self.to_ref,
            "warnings": self.warnings,
            "dryRun": self.dry_run,
            "gate": self.gate,
        }
        if self.drain is not None:
            fields["drain"] = self.drain
        if self.drain2 is not None:
            fields["drain2"] = self.drain2
        if self.early:
            fields["early"] = True
        return fields

    def human(self) -> str:
        src = (
            f"Account-{self.from_ref.get('number')}" if self.from_ref else "(none)"
        )
        dst = (
            f"Account-{self.to_ref.get('number')} ({self.to_ref.get('email')})"
            if self.to_ref
            else "?"
        )
        prefix = "[dry-run] would switch" if self.dry_run else "Switched"
        tail = ""
        if self.drain is not None:
            waited = self.drain.get("waitedSeconds")
            if self.drain.get("outcome") == "go":
                tail = f", drained {waited}s"
            else:
                tail = f", drain timed out at {waited}s"
        if self.drain2 is not None:
            waited = self.drain2.get("waitedSeconds")
            fixed = self.drain2.get("fixed")
            forced = self.drain2.get("forced")
            ack_n = self.drain2.get("ackFixed")
            soft_n = self.drain2.get("softFixed")
            proof = (
                f" (receipt {ack_n}, soft {soft_n})"
                if ack_n is not None and soft_n is not None
                else ""
            )
            if self.drain2.get("outcome") == "ready":
                tail += f", checkpointed {fixed} session(s){proof} in {waited}s"
            else:
                tail += (
                    f", checkpoint cap at {waited}s: {fixed} fixed{proof}, "
                    f"{forced} forced"
                )
        label = f"{self.trigger}, early" if self.early else self.trigger
        return f"{prefix} {src} -> {dst} ({label}, gate={self.gate}{tail})"


@dataclass(frozen=True)
class AdoptRealLoginEvent(AutoSwitchEvent):
    """The record's active slot was reconciled with the real login (CON-1581).

    ``activeAccountNumber`` is written only by switch/add, so a manual
    ``/login`` onto a managed account leaves it naming the old slot — and
    every record consumer (``list --json``, the ``refresh --all``
    active-slot exclusion, rotation anchors, the fresh-machine activation)
    then acts on the wrong slot. Emitted once per adoption, with both
    numbers.
    """

    kind: ClassVar[str] = "adopt-real-login"
    prior: dict | None  # account_ref shape, or None (no recorded active)
    to_ref: dict

    def _fields(self) -> dict:
        return {"from": self.prior, "to": self.to_ref}

    def human(self) -> str:
        src = (
            f"Account-{self.prior.get('number')}" if self.prior else "(none)"
        )
        return (
            f"Adopted the real login: the record said {src}, the live login "
            f"is Account-{self.to_ref.get('number')} "
            f"({self.to_ref.get('email')}) — moved outside cswap (manual "
            "/login)"
        )


@dataclass(frozen=True)
class AdoptSnapshotEvent(AutoSwitchEvent):
    """Forensic snapshot taken right after an adoption (CON-2323).

    An adoption without a ``/login`` means something wrote a foreign
    identity into the live store, and by the next tick the writer may be
    gone. The snapshot (``claude_swap.adopt_snapshot``) records the process
    table of Claude Code binaries with their ``CLAUDE_CONFIG_DIR``, the
    config file's mtime and identity, Keychain item dates and open handles
    — never a secret. ``error`` names a collector that failed outright; the
    adoption itself is never held up by it.
    """

    kind: ClassVar[str] = "adopt-real-login-snapshot"
    prior: dict | None
    to_ref: dict
    snapshot: dict | None
    error: str | None = None

    def _fields(self) -> dict:
        fields: dict = {
            "from": self.prior,
            "to": self.to_ref,
            "snapshot": self.snapshot,
        }
        if self.error:
            fields["error"] = self.error
        return fields

    def human(self) -> str:
        if self.error:
            return f"Adopt snapshot failed: {self.error}"
        snap = self.snapshot or {}
        procs = snap.get("processes") or {}
        without = procs.get("withoutConfigDir") or []
        on_profile = procs.get("onAdoptedProfile") or []
        cfg = snap.get("configFile") or {}
        live = snap.get("keychainLive") or {}
        prof = snap.get("keychainProfile") or {}
        openers = ", ".join(
            f"{o.get('command')}({o.get('pid')})" for o in snap.get("openers") or []
        )
        errors = snap.get("errors") or []
        return (
            f"Adopt snapshot: {procs.get('claude', 0)} claude process(es), "
            f"{len(without)} without CLAUDE_CONFIG_DIR"
            f"{' (' + ', '.join(map(str, without)) + ')' if without else ''}, "
            f"{len(on_profile)} on Account-{self.to_ref.get('number')}'s profile; "
            f"config mtime {cfg.get('mtime')}; live Keychain mdat {live.get('mdat')}; "
            f"profile Keychain mdat {prof.get('mdat') if prof else 'n/a'}; "
            f"open handles: {openers or 'none'}"
            f"{'; probe errors: ' + '; '.join(errors) if errors else ''}"
        )


@dataclass(frozen=True)
class NoSwitchEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "no-switch"
    reason: str
    detail: str = ""

    def _fields(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}

    def human(self) -> str:
        return f"no switch: {self.reason}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class LiveLoginSlotSkipEvent(AutoSwitchEvent):
    """A ranked candidate hosts a live ``cswap run`` session on its own
    login family (CON-2052): making it the default login too would put one
    rotating refresh token in two stores with a writer on each side (the
    CON-2030 class). The slot is dropped from this tick's targets before
    any drain gate runs. One event per skipped slot per tick."""

    kind: ClassVar[str] = "skip-live-login-slot"
    trigger: str
    number: str
    email: str
    pids: list[int] = field(default_factory=list)

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "number": self.number,
            "email": self.email,
            "pids": list(self.pids),
        }

    def human(self) -> str:
        pid_list = ", ".join(map(str, self.pids)) or "?"
        return (
            f"skip live-login-slot: Account-{self.number} ({self.email}) hosts a "
            f"live session on its login family (PID {pid_list}) — not a "
            f"{self.trigger} target"
        )


@dataclass(frozen=True)
class DrainTimeoutEvent(AutoSwitchEvent):
    """The drain ceiling hit while sessions were still writing: the forced
    switch proceeds under load, paying the prompt caches of every live
    session on the account being left. One per drain episode."""

    kind: ClassVar[str] = "drain-timeout"
    trigger: str
    waited_seconds: int
    max_wait_seconds: int
    detail: str = ""

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "waitedSeconds": self.waited_seconds,
            "maxWaitSeconds": self.max_wait_seconds,
            "detail": self.detail,
        }

    def human(self) -> str:
        return (
            f"WARN: sessions never went quiet in {self.max_wait_seconds}s — "
            f"{self.trigger} switch proceeds under load ({self.detail})"
        )


@dataclass(frozen=True)
class Drain2SignalEvent(AutoSwitchEvent):
    """A checkpoint (STOP) wave went out to mid-turn sessions."""

    kind: ClassVar[str] = "drain2-signal"
    trigger: str
    targets: list[str] = field(default_factory=list)
    # Names the herald confirmed sent; None = wave ran but the per-name
    # report was unparseable (delivery unconfirmed, not absent).
    delivered: list[str] | None = None
    skipped_interactive: int = 0
    top_up: bool = False  # mid-episode wave to sessions that appeared/woke
    dry_run: bool = False
    # Migration-price telemetry (CON-582), measured BEFORE the wave: the sum
    # of the judged sessions' context sizes (each session's last transcript
    # usage — the prompt footprint its next turn re-creates at full price on
    # the new account), with the per-session breakdown. None/empty = no
    # transcript was readable. Additive fields; future thresholds are judged
    # against these numbers.
    est_move_tokens: int | None = None
    est_session_tokens: dict[str, int | None] = field(default_factory=dict)
    # Sessions left running through the swap on purpose: their context is
    # at/below ``drain2SmallContextTokens``, so the checkpoint ceremony
    # would cost more than their cache re-create. Additive.
    skipped_small: list[str] = field(default_factory=list)

    def _fields(self) -> dict:
        fields = {
            "trigger": self.trigger,
            "targets": self.targets,
            "delivered": self.delivered,
            "skippedInteractive": self.skipped_interactive,
            "topUp": self.top_up,
            "dryRun": self.dry_run,
        }
        if self.est_move_tokens is not None:
            fields["estMoveTokens"] = self.est_move_tokens
        if self.est_session_tokens:
            fields["estSessionTokens"] = self.est_session_tokens
        if self.skipped_small:
            fields["skippedSmall"] = self.skipped_small
        return fields

    def human(self) -> str:
        confirmed = (
            f"{len(self.delivered)} confirmed"
            if self.delivered is not None
            else "delivery unconfirmed"
        )
        kind = "top-up checkpoint" if self.top_up else "checkpoint"
        tail = ""
        if self.est_move_tokens is not None:
            tail += f"; ~{self.est_move_tokens} tokens to move"
        if self.skipped_small:
            tail += (
                f"; {len(self.skipped_small)} small session(s) left running"
            )
        return (
            f"drain2: {kind} signal to {len(self.targets)} session(s) "
            f"({confirmed}; {self.trigger}){tail}"
        )


@dataclass(frozen=True)
class EarlySwapEvent(AutoSwitchEvent):
    """A proactive switch starts below the threshold because the busy park
    is small (CON-582): the migration price of a swap is the sum of the
    live contexts on the account being left, so at high utilization with
    only a few sessions mid-turn, leaving now is strictly cheaper than the
    same forced move at the threshold under a full park."""

    kind: ClassVar[str] = "early-swap"
    utilization_pct: float
    early_threshold: float
    busy_sessions: int
    max_busy: int

    def _fields(self) -> dict:
        return {
            "utilizationPct": self.utilization_pct,
            "earlyThresholdPct": self.early_threshold,
            "busySessions": self.busy_sessions,
            "maxBusy": self.max_busy,
        }

    def human(self) -> str:
        return (
            f"early swap: {pct_label(self.utilization_pct)}% ≥ "
            f"{pct_label(self.early_threshold)}% with a small park "
            f"({self.busy_sessions} busy ≤ {self.max_busy}) — proactive "
            "switch starts before the threshold"
        )


@dataclass(frozen=True)
class Drain2TimeoutEvent(AutoSwitchEvent):
    """The fixation cap hit while sessions were still mid-turn: the switch
    proceeds, paying the caches of the ``forced`` sessions. One per episode."""

    kind: ClassVar[str] = "drain2-timeout"
    trigger: str
    waited_seconds: int
    max_wait_seconds: int
    fixed: list[str] = field(default_factory=list)
    forced: list[str] = field(default_factory=list)
    # Proof breakdown of ``fixed`` (CON-461): who left a checkpoint receipt
    # vs who was soft-fixed by a sustained not-busy roster streak. Additive.
    acked: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    def _fields(self) -> dict:
        return {
            "trigger": self.trigger,
            "waitedSeconds": self.waited_seconds,
            "maxWaitSeconds": self.max_wait_seconds,
            "fixed": self.fixed,
            "forced": self.forced,
            "acked": self.acked,
            "soft": self.soft,
        }

    def human(self) -> str:
        return (
            f"WARN: {len(self.forced)} session(s) never checkpointed in "
            f"{self.max_wait_seconds}s ({', '.join(self.forced)}) — "
            f"{self.trigger} switch proceeds, {len(self.fixed)} fixed "
            f"(receipt {len(self.acked)}, soft-by-roster {len(self.soft)})"
        )


@dataclass(frozen=True)
class Drain2VerifyEvent(AutoSwitchEvent):
    """Post-swap check that the new active account answers (usage fetch)."""

    kind: ClassVar[str] = "drain2-verify"
    number: str
    ok: bool
    attempt: int
    detail: str = ""

    def _fields(self) -> dict:
        return {
            "number": self.number,
            "ok": self.ok,
            "attempt": self.attempt,
            "detail": self.detail,
        }

    def human(self) -> str:
        verdict = "answers" if self.ok else "not answering"
        return (
            f"drain2: new account {self.number} {verdict} "
            f"(attempt {self.attempt}{'; ' + self.detail if self.detail else ''})"
        )


@dataclass(frozen=True)
class Drain2ResumeEvent(AutoSwitchEvent):
    """The resume wave after a swap (or the reason none went out)."""

    kind: ClassVar[str] = "drain2-resume"
    targets: list[str] = field(default_factory=list)
    delivered: list[str] | None = None
    skipped: str = ""  # non-empty with no targets = no wave went out, and why
    # Why this resume happened outside the happy path (e.g. the episode was
    # abandoned by a switch of another trigger). Additive.
    reason: str = ""
    # Signaled sessions with an open task but NO checkpoint receipt — left
    # out of the wave on purpose (CON-461): they either never paused (the
    # wave would be noise mid-turn) or self-wake via their marker watch.
    unacked: list[str] = field(default_factory=list)

    def _fields(self) -> dict:
        fields = {
            "targets": self.targets,
            "delivered": self.delivered,
            "skipped": self.skipped,
            "unacked": self.unacked,
        }
        if self.reason:
            fields["reason"] = self.reason
        return fields

    def human(self) -> str:
        tail = f" [{self.reason}]" if self.reason else ""
        if self.unacked:
            tail += (
                f"; {len(self.unacked)} without receipt left to self-wake"
            )
        if self.skipped and self.targets:
            # The wave was attempted and failed — not the same as skipped.
            return (
                f"drain2: resume wave to {len(self.targets)} session(s) "
                f"failed ({self.skipped}){tail}"
            )
        if self.skipped:
            return f"drain2: resume wave skipped ({self.skipped}){tail}"
        confirmed = (
            f"{len(self.delivered)} confirmed"
            if self.delivered is not None
            else "delivery unconfirmed"
        )
        return (
            f"drain2: resume signal to {len(self.targets)} session(s) "
            f"({confirmed}){tail}"
        )


@dataclass(frozen=True)
class Drain2UnavailableEvent(AutoSwitchEvent):
    """The park channel failed — this episode falls back to the passive
    drain (v1) behavior for the same trigger."""

    kind: ClassVar[str] = "drain2-unavailable"
    reason: str

    def _fields(self) -> dict:
        return {"reason": self.reason}

    def human(self) -> str:
        return f"drain2 unavailable ({self.reason}); falling back to passive drain"


@dataclass(frozen=True)
class LastAccountAlertEvent(AutoSwitchEvent):
    """The engine had to switch and found nobody to switch to: the park is
    effectively down to its last working account. One deduped cry for a
    human (CON-572 class A — this alert replaces the stop wave the engine
    used to send before judging candidates; repeated every tick it would
    drown the log it is meant to surface in)."""

    kind: ClassVar[str] = "last-account"
    trigger: str
    reason: str

    def _fields(self) -> dict:
        return {"trigger": self.trigger, "reason": self.reason}

    def human(self) -> str:
        return (
            f"ALERT: the park is on its last working account "
            f"({self.reason}; trigger {self.trigger}) — add or recover an "
            "account; no switch can happen until a candidate exists"
        )


@dataclass(frozen=True)
class QuarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-quarantined"
    number: str
    email: str
    reason: str

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return (
            f"Account-{self.number} ({self.email}) quarantined: {self.reason}. "
            f"Log in with it and run 'cswap --add-account --slot {self.number}' "
            "to recover."
        )


@dataclass(frozen=True)
class UnquarantineEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "account-unquarantined"
    number: str
    email: str
    reason: str = "credentials-replaced"

    def _fields(self) -> dict:
        return {"number": self.number, "email": self.email, "reason": self.reason}

    def human(self) -> str:
        return f"Account-{self.number} ({self.email}) back in rotation ({self.reason})"


@dataclass(frozen=True)
class AllExhaustedEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "all-exhausted"
    earliest_reset_at: str | None

    def _fields(self) -> dict:
        return {"earliestResetAt": self.earliest_reset_at}

    def human(self) -> str:
        if self.earliest_reset_at:
            return f"all accounts exhausted; earliest reset {self.earliest_reset_at}"
        return "all accounts exhausted; no reset time known"


@dataclass(frozen=True)
class SleepEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "sleep"
    seconds: float
    until: str

    def _fields(self) -> dict:
        return {"seconds": round(self.seconds, 1), "until": self.until}

    def human(self) -> str:
        return f"sleeping {self.seconds / 60:.0f}m (until {self.until})"


@dataclass(frozen=True)
class ErrorEvent(AutoSwitchEvent):
    kind: ClassVar[str] = "error"
    message: str
    transient: bool = True

    def _fields(self) -> dict:
        return {"message": self.message, "transient": self.transient}

    def human(self) -> str:
        return f"error: {self.message}" + (" (will retry)" if self.transient else "")


@dataclass(frozen=True)
class ConfigWarningEvent(AutoSwitchEvent):
    """The configuration could not be taken as read: a value that is
    syntactically fine but provably inert (an ``autoswitch.model`` name no
    account reports), or a settings.json that stopped answering mid-run. Not
    an error — the engine keeps running on what it already has."""

    kind: ClassVar[str] = "config-warning"
    message: str

    def _fields(self) -> dict:
        return {"message": self.message}

    def human(self) -> str:
        return f"warning: {self.message}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TickOutcome(enum.Enum):
    """Outcome of one evaluation tick; values double as --once exit codes."""

    SWITCHED = 0
    ERROR = 1
    NO_ACTION = 2
    BLOCKED = 3  # wanted to switch but no viable target / all exhausted


# Quarantine state persisted fingerprints from a local refresh-token-only
# helper; oauth.credential_fingerprint is identical for refresh-token creds.
# Setup-token quarantines stored None where the shared helper now yields a
# full-content hash — those release once on first recheck and re-quarantine on
# the next dead freshen (one harmless extra cycle, migration only).
_refresh_fingerprint = oauth.credential_fingerprint


def _window_pcts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> dict[str, float]:
    """Ordered window label → pct: "5h", "7d", then configured scoped names.

    Deliberately restricted to the windows the *decision* reads (same
    ``models`` filter): showing an unconfigured scoped window at 100% next
    to a switch onto that account would look like a bug, when the engine
    correctly ignored it. Full per-model usage lives in ``cswap list``.
    """
    return {
        name: pct for name, pct, _ in oauth.relevant_windows(usage, models)
    }


# Reset math moved to poll_policy with the cadence numbers; aliased for the
# engine's sleep scheduling and the test suite.
_limiting_reset_ts = poll_policy.limiting_reset_ts
_earliest_future_reset_ts = poll_policy.earliest_future_reset_ts
_parse_reset_ts = poll_policy.parse_reset_ts


def _seven_day_reset_ts(usage: dict | str | None, now: float) -> float | None:
    """Epoch of an account's 7-day (weekly) window reset, or None if unknown
    or already past.

    The consume-first strategy ranks by this — the weekly window is the
    perishable quota (the 5-hour one recycles too fast to be worth planning
    around). A stale snapshot can carry a ``resets_at`` that has since
    elapsed; treated as a real instant it would sort the *just-rolled-over*
    account (the least perishable quota of all) as "soonest", so past ==
    unknown. Plain ``ts <= now``: RESET_SLACK_S is poll-scheduling lag
    tolerance, not ranking input — padding here would turn a genuinely
    imminent reset into a false reset-unknown hold.
    """
    if isinstance(usage, dict):
        window = usage.get("seven_day")
        if isinstance(window, dict):
            ts = _parse_reset_ts(window.get("resets_at"))
            if ts is not None and ts > now:
                return ts
    return None


def _ref(number: str, email: str) -> dict:
    return {"number": int(number), "email": email}


def _headroom_by_account(
    usage: dict[str, dict | str | None], models: tuple[str, ...]
) -> dict[str, float | None]:
    """Per-account headroom derived from decision values."""
    return {
        num: oauth.account_headroom(
            value if isinstance(value, dict) else None, models
        )
        for num, value in usage.items()
    }


class AutoSwitchEngine:
    """Threshold-policy auto-switcher over a :class:`ClaudeAccountSwitcher`.

    ``on_event`` receives every :class:`AutoSwitchEvent`; exceptions it raises
    are not caught (a broken frontend should fail loudly in tests). ``clock``
    is wall time (persisted cooldown timestamps must survive processes).

    ``overrides`` names the settings fields the user pinned explicitly (a
    ``cswap auto`` flag; see :func:`settings.cli_overrides`). Every tick
    re-reads settings.json and replays the fields whose *file* value changed
    since construction, so an edit lands without a restart — but never over a
    pin, and never over a value a host passed in that the file never carried.
    """

    def __init__(
        self,
        switcher: ClaudeAccountSwitcher,
        settings: AutoSwitchSettings,
        on_event: Callable[[AutoSwitchEvent], None],
        *,
        dry_run: bool = False,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        claude_projects_dir: Path | None = None,
        overrides: dict[str, object] | None = None,
        park: ParkChannel | None = None,
        adopt_snapshot: Callable[..., dict] | None = None,
    ):
        self.switcher = switcher
        self.settings = settings
        # Forensic collector run right after an adoption (CON-2323);
        # injectable so tests never touch ps/security/lsof.
        self._adopt_snapshot = (
            adopt_snapshot if adopt_snapshot is not None else default_adopt_snapshot
        )
        # Live settings tracking. ``_settings_base`` is what the engine was
        # constructed with, ``_settings_file`` the file as it read at that
        # moment, and ``_settings_pins`` the values named explicitly (a CLI
        # flag, or the TUI's session threshold). Reloading replays only the
        # fields whose file value moved, so a pin and a host-supplied value
        # both survive an unrelated edit.
        self._settings_base = settings
        read = read_settings(switcher.backup_dir)
        self._settings_file = read.settings
        # Whether the last read got an answer at all. Starting from the
        # construction read keeps the "no settings.json" install (the default)
        # silent: only a file that used to answer and stopped is worth a line.
        self._settings_file_ok = read.ok
        self._settings_pins = dict(overrides or {})
        # Model(s) whose per-model weekly limit also binds the switch decision
        # (empty = account-wide 5h/7d only). ``settings.model`` is a comma-
        # separated list ("Fable", "Opus,Sonnet", "all"); parsed here and on
        # every settings reload, then passed everywhere usage windows are read
        # — decisions, cadence, and reset scheduling must all see the same axes.
        self._models = parse_model_names(settings.model)
        # Poll plans written by the collector must key on the same threshold/
        # models the engine decides with (CLI overrides included), not on
        # whatever the settings file happens to say.
        switcher.set_poll_policy_inputs(settings.threshold, self._models)
        self.on_event = on_event
        self.dry_run = dry_run
        self.state_path = state_path or (switcher.backup_dir / STATE_FILENAME)
        self.clock = clock
        # Where the quiet gate looks for session transcripts; injectable for
        # tests, defaults to the same config home Claude Code resolves.
        self.claude_projects_dir = (
            claude_projects_dir
            if claude_projects_dir is not None
            else paths.get_claude_config_home() / "projects"
        )
        self._stop = threading.Event()
        # Cuts the current inter-tick sleep short (a session threshold change
        # from the TUI should show a fresh decision now, not next interval).
        self._wake = threading.Event()
        self._unhealthy_ticks = 0
        # Both set per tick: a known-reset sleep target, and whether a BLOCKED
        # outcome is static enough (truly exhausted / no candidates) to wait
        # longer than the normal interval.
        self._sleep_until_ts: float | None = None
        self._blocked_wait_long = False
        # Idle-hold: when the active token expired while Claude Code owns it
        # (and is therefore idle), crawl instead of counting unhealthy ticks.
        # ``_idle_hold_since`` survives across ticks (elapsed-time cap);
        # ``_idle_hold_slow`` is per-tick like ``_blocked_wait_long``.
        self._idle_hold_since: float | None = None
        self._idle_hold_slow = False
        # One-shot typo guard for ``autoswitch.model``: resolved (and possibly
        # warned) on the first tick where every relevant account has readable
        # usage — adaptive polling legitimately leaves gaps before that.
        self._model_check_done = not self._models
        # ``autoswitch.homeAccount`` (CON-1070) warns once per inert
        # condition (unknown account / disabled home), keyed here so a
        # daemon does not repeat itself every tick and says it again only
        # when the condition changes.
        self._home_warned: str | None = None
        # Drain episode under dry-run lives here instead of the state file
        # (dry-run must not write anything).
        self._drain_mem: dict | None = None
        # Park channel for drain v2 waves/roster; built lazily so an engine
        # that never drains (or a test that injects a fake) costs nothing.
        self._park = park
        self._drain2_mem: dict | None = None
        # Set after a channel failure: until then, drain v2 stands down and
        # forced proactive switches take the passive v1 drain.
        self._park_backoff_until = 0.0
        # Set after an episode is released without a swap because every
        # ranked target failed to freshen (CON-461, narrowed by CON-572):
        # without it the next tick would re-signal a fresh pause and thrash
        # STOP/release every interval for as long as the same broken
        # candidate keeps qualifying. The no-candidate case no longer needs
        # it — candidates are judged before any wave, so a pause cannot be
        # signaled while there is nobody to switch to — and must not arm
        # it: the backoff would stall the orderly episode a fresh ``cswap
        # add`` candidate deserves. Unlike the channel backoff this one
        # guards a HEALTHY channel, so it must survive the process: the
        # truth lives in the state file (``drain2ReleaseUntil``, review r1
        # finding 1) — this field is the in-process mirror, and the only
        # copy under dry-run (which never writes state).
        self._drain2_release_until = 0.0
        # In-process half of the "park is on its last account" alert dedup
        # (CON-572 class A): the durable half is ``lastAccountAlertedAt``
        # in the state file (shared with cron ``--once`` ticks), and the
        # only copy under dry-run. Re-armed the moment a qualifying
        # candidate exists again.
        self._last_account_alerted = False

    # -- state file ---------------------------------------------------------

    def _state_lock(self) -> FileLock:
        return FileLock(self.state_path.parent / ".autoswitch_state.lock")

    def _read_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _mutate_state(self, mutator: Callable[[dict], None]) -> dict:
        """Read-modify-write the state file under its lock; returns new state.

        The lock prevents two concurrent engines (loop + cron ``--once``) from
        overwriting each other's quarantine/cooldown updates. Never called
        while any other lock is held.
        """
        with self._state_lock():
            state = self._read_state()
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            mutator(state)
            atomic_write_json(self.state_path, state)
            return state

    # -- quarantine -----------------------------------------------------------

    def _quarantine(self, number: str, email: str, reason: str) -> None:
        creds = self.switcher.read_account_credentials(number, email)
        fingerprint = _refresh_fingerprint(creds) if creds else None

        def add(state: dict) -> None:
            state.setdefault("quarantine", {})[number] = {
                "email": email,
                "reason": reason,
                "at": _now_iso(),
                "refreshTokenFingerprint": fingerprint,
            }

        self._mutate_state(add)
        self._emit(QuarantineEvent(number=number, email=email, reason=reason))

    def _release_recovered_quarantines(self, state: dict) -> dict:
        """Drop quarantine entries whose credential was replaced since.

        A changed refresh-token fingerprint (or a removed/re-added slot) means
        the user re-logged in and re-captured the account — the dead lineage
        is gone, so it re-enters rotation.
        """
        quarantine = state.get("quarantine")
        if not isinstance(quarantine, dict) or not quarantine:
            return state
        to_release: list[tuple[str, str, str]] = []
        for number, entry in quarantine.items():
            email_now = self.switcher.account_email(number)
            if not email_now or email_now != entry.get("email"):
                to_release.append(
                    (number, entry.get("email", ""), "account-replaced")
                )
                continue
            creds = self.switcher.read_account_credentials(number, email_now)
            fingerprint = _refresh_fingerprint(creds) if creds else None
            if fingerprint != entry.get("refreshTokenFingerprint"):
                to_release.append((number, email_now, "credentials-replaced"))
        if not to_release:
            return state

        def drop(s: dict) -> None:
            q = s.get("quarantine")
            if isinstance(q, dict):
                for number, _, _ in to_release:
                    q.pop(number, None)

        state = self._mutate_state(drop)
        for number, email, reason in to_release:
            self._emit(UnquarantineEvent(number=number, email=email, reason=reason))
        return state

    # -- freshening -----------------------------------------------------------

    def _live_login_pids(self, number: str, email: str) -> list[int]:
        """PIDs of live ``cswap run`` sessions on ``number`` that run on the
        slot's own LOGIN family — empty when there is no live session, or
        the sessions are proven independent of the login (API-key slot,
        token-seeded profile, drifted identity, a profile still at its seed
        generation over a moved backup: ``_live_session_shares_login``).
        Read-only: a PID scan and, only when sessions exist, one profile
        credential read."""
        if self.switcher.account_kind_for(number) == "api_key":
            return []
        pids = self.switcher.live_session_pids_for(number, email)
        if pids and self.switcher.live_session_shares_login_for(number, email):
            return pids
        return []

    def _drop_live_login_slots(
        self, ordered: list[str], trigger: str
    ) -> tuple[list[str], list[tuple[str, str, list[int]]]]:
        """Split the ranked targets into (takeable, skipped). A slot hosting
        a live session on its login family is skipped with a
        ``skip-live-login-slot`` event naming the slot and its PIDs
        (CON-2052)."""
        kept: list[str] = []
        skipped: list[tuple[str, str, list[int]]] = []
        for num in ordered:
            email = self.switcher.account_email(num)
            pids = self._live_login_pids(num, email)
            if pids:
                skipped.append((num, email, pids))
                self._emit(
                    LiveLoginSlotSkipEvent(
                        trigger=trigger, number=num, email=email, pids=pids
                    )
                )
            else:
                kept.append(num)
        return kept, skipped

    @staticmethod
    def _live_login_detail(skipped: list[tuple[str, str, list[int]]]) -> str:
        """``no-viable-target`` detail naming the skipped live login slots."""
        if not skipped:
            return ""
        slots = "; ".join(
            f"Account-{num} ({email}, PID {', '.join(map(str, pids))})"
            for num, email, pids in skipped
        )
        return (
            "skipped live login slot(s) — a 'cswap run' session there runs "
            f"on the slot's own login family: {slots}"
        )

    def _freshen_target(self, number: str, email: str) -> str:
        """Ensure a candidate's stored token outlives Claude Code's 5-min
        refresh buffer before it gets activated.

        Returns ``"ok"``, ``"invalid_grant"`` (dead lineage — quarantine),
        ``"identity-conflict"`` (alive but authenticates as a different
        account — quarantine, do not activate), ``"transient"`` (network
        trouble — try again next tick) or ``"skip-live-session"``. Only ever
        touches the slot's *backup* store; the active credential belongs to
        Claude Code.
        """
        if self.switcher.account_kind_for(number) == "api_key":
            return "ok"  # API keys don't expire/refresh
        if self._live_login_pids(number, email):
            # A live `cswap run` session owns this account's LOGIN family in
            # its own profile. Auto-activating it as the default login too
            # would put one rotating refresh token in two config dirs (the
            # stale-copy failure class) with nobody reading the warning — and
            # its quota is already being consumed by that session anyway.
            # Judged by what the profile holds (CON-2030): a slot whose
            # sessions run on the attached year-long inference token owns no
            # family (`_bootstrap` seeds the profile with the TOKEN, the login
            # stays in backup as the quota gauge), so the login may land
            # there — on main the park's token home (slot 21, always hosting
            # bg-agent sessions) logged `return-home-wait: a 'cswap run'
            # session holds Account-21` every 30 s and the login never came
            # home. Manual switch_to refuses the shared-login case and offers
            # `--even-if-live`.
            return "skip-live-session"
        # CON-1595: the backup may be a CONSUMED generation — a `cswap run`
        # session on this slot rotated the family inside its profile and
        # nothing synced it back (CON-1579). Freshening from that backup
        # POSTs a consumed grant, the server answers invalid_grant, and an
        # alive slot got quarantined (one rotation candidate fewer, for a
        # dead-lineage verdict that was false). Same judge as `switch`: heal
        # the backup from the profile first (adopt a fresh generation without
        # a POST, refresh an expired one with the PROFILE's grant), then judge
        # expiry off the healed copy. Lazy import: refresh imports session,
        # which the switcher import chain already loads.
        from claude_swap.refresh import (
            DEFERRED,
            LIVE_SESSION,
            NO_CREDENTIALS,
            RELOGIN_REQUIRED,
            TRANSIENT_ERROR,
            heal_backup_before_activation,
        )

        report = heal_backup_before_activation(
            self.switcher, number, email,
            self.switcher.account_identity(number).get("organizationUuid", ""),
        )
        if report.outcome == LIVE_SESSION:
            # A session appeared between the check above and the heal's own.
            return "skip-live-session"
        if report.outcome == RELOGIN_REQUIRED:
            # The profile's grant — the family's newest — was rejected: the
            # lineage really is dead, quarantine is the truthful verdict.
            return "invalid_grant"
        if report.outcome in (TRANSIENT_ERROR, DEFERRED, NO_CREDENTIALS):
            return "transient"
        creds = self.switcher.read_account_credentials(number, email)
        # A pending spilled rotation (CON-849) supersedes the stored bytes:
        # the backup holds the consumed predecessor, and refreshing or
        # activating it would present a consumed grant. Reconcile first;
        # a contended reconcile defers the candidate to the next tick.
        creds = self.switcher._reconcile_spilled_rotation(number, email, creds)
        if creds is None:
            return "transient"
        if not creds:
            return "transient"
        data = oauth.extract_oauth_data(creds)
        if not data:
            return "invalid_grant"
        expires_at = data.get("expiresAt")
        now_ms = self.clock() * 1000
        near_expiry = (
            isinstance(expires_at, (int, float))
            and now_ms + FRESHEN_BUFFER_MS >= expires_at
        )
        if not near_expiry:
            return "ok"
        outcome = oauth.try_refresh_oauth_credentials(creds)
        if outcome.error is None and outcome.credentials:
            # Persist first, unconditionally: the grant consumed a generation,
            # and not writing the successor would kill the lineage regardless
            # of whose it turns out to be. A persist that only reached the
            # spill (lock contention, CON-849) preserved the pair but left
            # the *stored* credential a consumed generation — activating the
            # slot off it would hand a live claude a dead grant, so the
            # candidate defers until a reconcile pass lands the pair.
            persisted = self.switcher.persist_backup_credentials(
                number, email, outcome.credentials,
                predecessor=oauth.credential_fingerprint(creds),
            )
            if not persisted:
                return "transient"
            if self._note_token_identity(number, outcome.token_account):
                # The slot's stored credential authenticates as a *different*
                # account — activating it would put the user on the wrong
                # account with every gauge reading normal. Not a viable
                # target; the caller quarantines it (released automatically
                # once the credential is replaced by a re-add).
                return "identity-conflict"
            return "ok"
        if outcome.error in ("invalid_grant", "no_refresh_token"):
            return "invalid_grant"
        return "transient"

    def _note_token_identity(
        self, number: str, token_account: dict | None
    ) -> bool:
        """Use the token endpoint's free identity to verify/backfill a slot.

        The refresh grant just ran against the slot's own stored credential,
        so ``token_account`` (when the server includes it) names who that
        credential really is. Returns True on a *conflict*: the credential
        authenticates under a different organization than the slot records
        (org compared first, whenever both sides record one), or as a
        different account uuid. An empty slot uuid (blank-uuid records from
        older versions, add-token placeholders) is backfilled — but only
        when no org conflict exists: a wrong-org credential is evidence the
        slot holds the wrong account, and backfilling *its* uuid would
        poison the slot's identity record (backfill never rewrites a
        non-empty uuid, so that corruption would be sticky).

        ``_parse_token_account`` already enforces a strict boundary, but this
        identity is opportunistic — re-check types here so malformed data can
        never break the freshen that carried it (the successor credential is
        already persisted by the time this runs).
        """
        if not isinstance(token_account, dict):
            return False
        ta_uuid = token_account.get("uuid")
        if not isinstance(ta_uuid, str) or not ta_uuid.strip():
            return False
        ta_uuid = ta_uuid.strip()
        slot_identity = self.switcher.account_identity(number)
        ta_org = token_account.get("organizationUuid")
        slot_org = slot_identity.get("organizationUuid") or ""
        if isinstance(ta_org, str) and ta_org and slot_org and ta_org != slot_org:
            return True
        if not slot_identity.get("uuid"):
            try:
                self.switcher.backfill_account_uuid(number, ta_uuid)
            except Exception as e:  # never let bookkeeping break a freshen
                _logger.debug("uuid backfill failed for account %s: %r", number, e)
            return False
        return slot_identity["uuid"] != ta_uuid

    # -- live settings --------------------------------------------------------

    @property
    def models(self) -> tuple[str, ...]:
        """The model axes the decision binds on right now (empty = 5h/7d only).

        The one place a frontend may read them from. They move with
        settings.json under a running engine, so a display that parses the
        file itself and keeps the result goes stale the moment the user
        toggles the key — and then names a "next best" account the engine
        will not pick. Reading a tuple attribute is atomic, so the UI thread
        can ask while the engine thread is mid-tick.
        """
        return self._models

    def _settings_stamp(self) -> tuple[int, int] | None:
        """Cheap change token for settings.json (one stat), None when absent.

        Only ever compared with itself: the loop uses it to notice an edit
        mid-sleep, never to decide what the settings say.
        """
        try:
            st = os.stat(settings_path(self.switcher.backup_dir))
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _refresh_settings(self) -> None:
        """Adopt settings.json edits mid-run — no restart, no new engine.

        Freezing the settings at construction made every file-backed
        preference a restart-only preference, ``autoswitch.model`` included:
        the whole point of that key is to fold a per-model weekly quota into
        the decision, and a switch policy that needs a daemon restart to
        change is a switch policy nobody can toggle.

        Only the fields whose *file* value moved since construction are
        replayed, so a value the host passed in (and the file never carried)
        stays put, and explicit pins always win. A changed model list rebuilds
        every axis derived from it: the decision windows, the collector's
        poll-planning keys, and the one-shot model-name guard — which has
        never seen the new name and must get its shot at it.

        A read that did not answer (file deleted, half-written, unreadable)
        is not an edit: ``load_settings`` would hand back plain defaults, and
        replaying those would revert model axes, strategy, threshold and
        cooldown under a running daemon with nothing in the log to show for
        it. The last snapshot that did answer stays in force, and the engine
        says so once.
        """
        read = read_settings(self.switcher.backup_dir)
        if not read.ok:
            if self._settings_file_ok:
                self._settings_file_ok = False
                path = settings_path(self.switcher.backup_dir)
                self._emit(
                    ConfigWarningEvent(
                        message=(
                            f"could not read {path} ({read.error}); keeping the "
                            "settings already in effect"
                        )
                    )
                )
            return
        self._settings_file_ok = True
        file_now = read.settings
        changed = {
            f.name: getattr(file_now, f.name)
            for f in fields(AutoSwitchSettings)
            if getattr(file_now, f.name) != getattr(self._settings_file, f.name)
        }
        base = replace(self._settings_base, **changed) if changed else self._settings_base
        settings = with_overrides(base, self._settings_pins)
        if settings == self.settings:
            return
        self.settings = settings
        models = parse_model_names(settings.model)
        if models != self._models:
            self._models = models
            self._model_check_done = not models
        self.switcher.set_poll_policy_inputs(settings.threshold, self._models)

    # -- tick -----------------------------------------------------------------

    def tick(self) -> TickOutcome:
        """Evaluate once: poll usage, maybe switch. Never raises."""
        try:
            return self._tick_inner()
        except ClaudeSwitchError as e:
            self._emit(ErrorEvent(message=str(e), transient=True))
            return TickOutcome.ERROR
        except Exception as e:  # pragma: no cover - safety net
            self._emit(
                ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
            )
            return TickOutcome.ERROR

    def _tick_inner(self) -> TickOutcome:
        self._sleep_until_ts = None
        self._blocked_wait_long = False
        self._idle_hold_slow = False
        # Once per tick, before anything reads them: the tick then decides on
        # one coherent snapshot of the settings, exactly as it already does
        # for the threshold.
        self._refresh_settings()
        settings = self.settings
        state = self._read_state()
        if not self.dry_run:
            # Dry-run must not write anything, so recovered quarantines are
            # only released (state mutation) on real ticks.
            state = self._release_recovered_quarantines(state)
        quarantined = set(
            state.get("quarantine", {})
            if isinstance(state.get("quarantine"), dict)
            else {}
        )

        # A drain-v2 episode swapped but hasn't resumed its frozen sessions
        # yet (daemon restart, cron handover, or a pending verify retry):
        # finish it before anything else — a NO_ACTION tick must not leave
        # the park frozen. Then reconcile a signaled episode the gate can no
        # longer own (a switch of another trigger landed past it, or the
        # forcing condition went away): same law, the park must never stay
        # frozen on an episode the engine knows is dead.
        self._drain2_finish()
        self._drain2_reconcile()

        current = self.switcher.current_account_number()
        if current is None:
            self._emit(
                PollEvent(active=None, headroom={}, threshold=settings.threshold)
            )
            if self.switcher.has_live_login():
                # Live login exists but cswap doesn't manage it: never act —
                # a switch would overwrite it without a backup.
                self._emit(
                    NoSwitchEvent(
                        reason="unmanaged-active-account",
                        detail="run 'cswap --add-account' to include it in rotation",
                    )
                )
            else:
                self._emit(
                    NoSwitchEvent(
                        reason="no-active-account",
                        detail="log in and run 'cswap --add-account' first",
                    )
                )
            return TickOutcome.NO_ACTION

        current_email = self.switcher.account_email(current)
        active_ref = _ref(current, current_email) if current_email else {
            "number": int(current),
            "email": "",
        }

        if not self.dry_run:
            # A manual /login moved the live identity without a switch: the
            # record's active slot goes stale until the next switch, and its
            # consumers act on the wrong slot (CON-1581, 31-08). Reconcile
            # every real tick; dry-run must not write anything, same as the
            # quarantine release above.
            adopted, prior = self.switcher.adopt_active_account(current)
            if adopted:
                prior_ref = (
                    _ref(prior, self.switcher.account_email(prior))
                    if prior is not None
                    else None
                )
                self._emit(
                    AdoptRealLoginEvent(prior=prior_ref, to_ref=dict(active_ref))
                )
                self._emit(
                    self._adopt_snapshot_event(
                        prior_ref, dict(active_ref), current, current_email
                    )
                )

        entries, usage, headroom = self._collect_scheduled_usage(
            current, quarantined, threshold=settings.threshold
        )
        # Pool-shield axis (CON-712): account-wide 5h/7d headroom, ignoring
        # configured per-model windows. Voluntary consume-first decisions are
        # judged on this axis so the rotation can rest on (and prefer)
        # model-burned hosts instead of hoarding a model-fresh account the
        # fleet's model-pinned work is starving for; the model-aware map
        # above keeps binding the at-limit escape and escape landings.
        base_headroom = (
            _headroom_by_account(usage, ()) if self._models else headroom
        )
        self._emit(
            PollEvent(
                active=active_ref,
                headroom=headroom,
                threshold=settings.threshold,
                fetch_errors={
                    num: entry.last_error
                    for num, entry in entries.items()
                    if usage.get(num) is None and entry.last_error
                },
                windows={
                    num: pcts
                    for num, value in usage.items()
                    if (pcts := _window_pcts(
                        value if isinstance(value, dict) else None, self._models
                    ))
                },
            )
        )

        if not self._model_check_done:
            self._check_model_names(quarantined, usage)

        if (
            self.switcher.account_kind_for(current) == "api_key"
            and not settings.include_api_key_accounts
        ):
            self._emit(
                NoSwitchEvent(
                    reason="active-api-key",
                    detail="API-key accounts have no quota to watch",
                )
            )
            return TickOutcome.NO_ACTION

        active_headroom = headroom.get(current)

        # -- home pin (CON-1070) -------------------------------------------
        # The fleet seats agents per slot and refuses the *active* one (a
        # ``cswap run`` of the active slot rides the global login and would
        # be swapped from under the agent), so a login that rotates costs
        # the fleet one seat at all times. With a home slot the login rests
        # there instead: nothing voluntary moves it, a maxed account-wide
        # window holds too (the wall is the user's own to wait out), and
        # only a dead token — usage unreadable — escalates to failover
        # below. A burned scoped model window holds as well (CON-2069):
        # CON-1581 had made it an escape because the interactive terminal
        # rode the global login and went mute on a Fable-100% home (31-08);
        # since then the terminals run as ``cswap run`` sessions in the home
        # slot's own profile and the global login serves only the couriers
        # — jobs without ``cswap run`` that ride the model ladder and never
        # need the pinned model — so the escape bought the terminal nothing
        # and took a fleet seat for as long as the home stayed burned (04-09:
        # three moves in a day, one Fable slot lost for two days; the owner's
        # third "the active login is always on 32"). The only voluntary way
        # off the home slot is the user's word: ``cswap config set
        # autoswitch.homeAccount`` / ``cswap disable``. Away from home, the
        # return lands once the home slot proves alive — its scoped window
        # is not judged; while it cannot land yet (no proof of life, traffic,
        # a live session on the slot) the plain rotation keeps judging the
        # slot the login is on, so at-limit and failover there still escape
        # (review r1). An inert pin — home disabled (the user's explicit
        # hold-out wins, said once) or quarantined — leaves every tick to
        # the plain rotation.
        home = self._home_slot(settings)
        pin_active = home is not None and not self._home_inert(home, quarantined)
        if pin_active:
            if current != home:
                outcome = self._return_home(
                    home,
                    quarantined=quarantined,
                    usage=usage,
                )
                if outcome is not None:
                    return outcome
            elif active_headroom is not None:
                self._unhealthy_ticks = 0
                self._idle_hold_since = None
                burned = self._model_window_burned(
                    usage.get(current), threshold=settings.threshold
                )
                if burned is None:
                    burned_note = ""
                else:
                    name, pct = burned
                    burned_note = (
                        f"; its {name} window is at {pct_label(pct)}% "
                        f"(switch threshold {pct_label(settings.threshold)}%) "
                        "and the pin holds anyway — the couriers on the home "
                        "slot ride the model ladder (CON-2069)"
                    )
                self._emit(
                    NoSwitchEvent(
                        reason="home-pinned",
                        detail=(
                            f"the live login rests on Account-{home}; only a "
                            "dead token or 'cswap config set "
                            "autoswitch.homeAccount' moves it"
                            + burned_note
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            elif self._gauge_rate_limited(entries.get(current), self.clock()):
                # CON-2267: the usage endpoint answered http-429 (per-token
                # budget, Retry-After) for the home — a limit on the GAUGE,
                # not a dead token: the server recognised the token in order
                # to throttle it. Counting those ticks as "unhealthy" drove
                # failover off the pinned home three times on 05-09 (18:39,
                # 20:44, 23:31), each time parking a fleet slot as "active"
                # for the whole 3600 s backoff while the fleet sensor waited
                # for the same signal to clear. Hold the pin; the unhealthy
                # counter neither grows nor resets — only an auth failure
                # (401 / invalid_grant / unreadable without a cause) still
                # escalates to failover below. Only while the honoured
                # Retry-After backoff is live and the collector has no
                # sentinel verdict on the credentials themselves (review r1:
                # a stale http-429 on the row survives sentinel passes).
                self._idle_hold_since = None
                lifts = (entries[current].backoff_until or 0) - self.clock()
                self._emit(
                    NoSwitchEvent(
                        reason="home-pinned",
                        detail=(
                            f"the live login rests on Account-{home}; its "
                            "usage gauge is rate-limited (http-429, backoff "
                            f"lifts in {int(max(lifts, 0))} s) — the server "
                            "recognised the token, so it is alive and the "
                            "pin holds (CON-2267)"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION

        early = False
        if active_headroom is not None:
            self._unhealthy_ticks = 0
            self._idle_hold_since = None
            utilization = 100.0 - active_headroom
            if active_headroom <= 0:
                # A maxed *binding* window (model included) always escapes:
                # whatever is pinned to it is at the wall right now.
                trigger = "at-limit"
            else:
                if settings.strategy == "consume-first" and not pin_active:
                    # Pool-shield (CON-712): how burned this host is gets
                    # judged account-wide (5h/7d). A model-burned host with
                    # session room left is a correct resting spot for the
                    # rotation — not a threshold breach to flee, which is
                    # what re-hoarded a model-fresh account on every tick.
                    # Suspended while the home pin is active (CON-1581): the
                    # pin already guarantees the fleet a resting login, so
                    # away from home (a failover or a manual switch) the
                    # login is coming home anyway and a model-aware landing
                    # is the cheaper wait (CON-2069: at home the pin holds
                    # regardless of the model window — this branch only runs
                    # away from home).
                    base_h = base_headroom.get(current)
                    if base_h is not None:
                        utilization = 100.0 - base_h
                if utilization < settings.threshold:
                    early = self._early_swap_fires(utilization, settings, state)
                    if early:
                        # The park is small enough that leaving NOW is cheaper
                        # than the same move at the threshold under a full park
                        # (CON-582). Rides the proactive trigger end to end —
                        # ranking, gates, drain2 — only the entry condition and
                        # the event label differ.
                        trigger = "proactive"
                    elif settings.strategy != "consume-first":
                        self._emit(
                            NoSwitchEvent(
                                reason="below-threshold",
                                # Both sides through pct_label: .0f utilization
                                # could display an impossible "100% < 99.9%".
                                detail=(
                                    f"{pct_label(utilization)}% < "
                                    f"{pct_label(settings.threshold)}%"
                                ),
                            )
                        )
                        return TickOutcome.NO_ACTION
                    else:
                        # consume-first: below the threshold we still
                        # proactively move to whichever account's weekly
                        # window resets soonest, to burn the most-perishable
                        # quota first. Candidate selection decides whether a
                        # sooner-resetting account with room actually exists.
                        trigger = "consume-first"
                else:
                    trigger = "proactive"
        else:
            if usage.get(current) == USAGE_TOKEN_EXPIRED:
                # Expired and the refresh could not complete this pass (lock
                # contention, unattributable lineage, failed persist, or the
                # row's failure backoff gating the fetch). The locked-refresh
                # path retries on later passes — no quota burn, nothing to
                # switch for yet; crawl slowly instead of burning failover
                # ticks (Finding 2 of the usage-lapse investigation).
                now = self.clock()
                if self._idle_hold_since is None:
                    self._idle_hold_since = now
                if now - self._idle_hold_since <= IDLE_HOLD_MAX_S:
                    self._unhealthy_ticks = 0
                    self._idle_hold_slow = True
                    self._emit(
                        NoSwitchEvent(
                            reason="active-idle",
                            detail=(
                                "token expired while Claude Code is idle; "
                                "resumes on next use"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Held far longer than any idle nap should need — likely a
                # dead refresh token with an *active* user. Fall through to
                # normal unhealthy counting so failover can still happen.
                _logger.warning(
                    "Active token expired and owned for over %.0f minutes; "
                    "resuming unhealthy counting (dead refresh token?)",
                    IDLE_HOLD_MAX_S / 60,
                )
            else:
                self._idle_hold_since = None
            self._unhealthy_ticks += 1
            if self._unhealthy_ticks < settings.unhealthy_ticks:
                self._emit(
                    NoSwitchEvent(
                        reason="active-usage-unknown",
                        detail=(
                            f"{self._unhealthy_ticks}/{settings.unhealthy_ticks} "
                            "before failover"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            trigger = "failover"

        # Cooldown only debounces the voluntary consume-first rotation
        # (reset-order churn). Proactive means the threshold is already
        # crossed — "time to leave", not an optimization — so holding it
        # here just rides the account into the wall (2026-07-31 incident:
        # eight minutes of no-switch cooldown at 98%, then a forced
        # at-limit escape).
        if trigger == "consume-first" and self._in_cooldown(state):
            self._emit(NoSwitchEvent(reason="cooldown"))
            return TickOutcome.NO_ACTION

        drain: dict | None = None
        drain2: dict | None = None
        if trigger in self._gated_triggers():
            quiet, detail = self._session_quiet()
            if not quiet:
                self._emit(NoSwitchEvent(reason="sessions-active", detail=detail))
                return TickOutcome.NO_ACTION

        # -- candidate selection ------------------------------------------
        # Judged BEFORE any drain gate (CON-572 class A): the stop wave and
        # the passive wait both belong to a switch that can actually
        # happen. The 15-08 episode paused six working sessions twice in 13
        # minutes only to discover there was nobody to switch to.
        candidates = [
            num
            for num in self.switcher.switchable_account_numbers()
            if num != current and num not in quarantined
        ]
        oauth_candidates = [
            n for n in candidates if self.switcher.account_kind_for(n) != "api_key"
        ]
        api_key_candidates = (
            [n for n in candidates if self.switcher.account_kind_for(n) == "api_key"]
            if settings.include_api_key_accounts
            else []
        )
        if (
            trigger == "consume-first"
            and not oauth_candidates
            and active_headroom is not None
        ):
            # Healthy below-threshold account with no OAuth peer to compare
            # against — the same state `best` reports as below-threshold
            # NO_ACTION before ever reaching candidate selection. API-key
            # candidates don't change the outcome: they have no weekly window
            # to consume, so a consume-first nudge never targets them. Keep
            # the exit-code contract identical across strategies: cron
            # wrappers keying on BLOCKED must not see false "blocked" from
            # the flag alone.
            self._emit(
                NoSwitchEvent(
                    reason="below-threshold",
                    # `utilization` and not raw active_headroom: under the
                    # pool-shield the judged axis is account-wide, and the
                    # model-aware percent here would contradict the reason
                    # on a model-burned host ("95% < 90%", review r1 nit).
                    detail=(
                        f"{pct_label(utilization)}% < "
                        f"{pct_label(settings.threshold)}%"
                    ),
                )
            )
            return TickOutcome.NO_ACTION
        if not oauth_candidates and not api_key_candidates:
            if early:
                # A voluntary below-threshold tick: on main this very tick
                # was a plain below-threshold NO_ACTION, so the early
                # trigger must not add the last-account alert or the long
                # blocked wait on top of it (review r1 finding 2). Any
                # signaled early episode still releases — the park must
                # not stay paused for a swap that cannot happen.
                self._emit(NoSwitchEvent(reason="no-candidates"))
                self._abandon_switch_intent(
                    trigger, "no accounts to switch to", alert=False
                )
                return TickOutcome.NO_ACTION
            # Won't change until the user adds/recovers an account — no point
            # re-polling at full cadence.
            self._blocked_wait_long = True
            self._emit(NoSwitchEvent(reason="no-candidates"))
            self._abandon_switch_intent(trigger, "no accounts to switch to")
            return TickOutcome.BLOCKED

        consume_first = settings.strategy == "consume-first"
        ordered, any_known, active_reset_ts = self._rank_candidates(
            trigger=trigger,
            consume_first=consume_first,
            oauth_candidates=oauth_candidates,
            usage=usage,
            headroom=headroom,
            base_headroom=base_headroom,
            current=current,
            active_headroom=active_headroom,
            settings=settings,
            now=self.clock(),
            early=early,
            pin_active=pin_active,
        )

        if (trigger == "consume-first" or early) and ordered:
            # Two-phase commit: the provisional pick may have ridden a
            # snapshot up to CANDIDATE_MAX_INTERVAL_S stale — consume-first
            # and the early swap both decide below the threshold, where the
            # collector only escalates inside the ESCALATION_MARGIN_PCT band
            # (flat-traffic invariant). A switch is imminent, so spend the
            # fetches now and re-decide on fresh data.
            # reserve() serves just-fetched accounts from the store, so this
            # is cheap in-tick and plan-bounded across ticks. The trigger is
            # deliberately NOT re-classified if the fresh active crossed the
            # threshold: a still-qualifying sooner target switches anyway,
            # and otherwise the next tick escalates normally and escapes.
            entries = self.switcher.usage_entries_by_account(
                fetch={current, *candidates}
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}
            headroom = _headroom_by_account(usage, self._models)
            base_headroom = (
                _headroom_by_account(usage, ()) if self._models else headroom
            )
            active_headroom = headroom.get(current)
            ordered, any_known, active_reset_ts = self._rank_candidates(
                trigger=trigger,
                consume_first=consume_first,
                oauth_candidates=oauth_candidates,
                usage=usage,
                headroom=headroom,
                base_headroom=base_headroom,
                current=current,
                active_headroom=active_headroom,
                settings=settings,
                now=self.clock(),
                early=early,
                pin_active=pin_active,
            )

        if (
            not ordered
            and api_key_candidates
            and trigger != "consume-first"
            and not early
        ):
            # Last resort when we must move: metered API-key accounts
            # (unmeasurable headroom). Never for a below-threshold voluntary
            # tick — a consume-first nudge has no weekly window to consume
            # there, and an early swap that took it would freeze the park in
            # a signaled episode whose swap can never pass the stale-usage
            # gate (api-key rows carry no usage entry) — a below-threshold
            # livelock (review r1 finding 1).
            ordered = api_key_candidates

        if not ordered:
            if not any_known:
                # No candidate readable this tick — true for every strategy,
                # and must not be dressed up as a consume-first hold.
                self._emit(
                    NoSwitchEvent(
                        reason="no-comparison",
                        detail=(
                            "no candidate has provable headroom (usage "
                            "unreadable, or a configured model window "
                            "unreported)"
                        ),
                    )
                )
                # A signaled drain2 episode SURVIVES this tick: unreadable
                # usage is transient, not a dead intent — same law as the
                # transient freshen failure below, which deliberately keeps
                # the episode so an orderly pause isn't thrown away on a
                # network blip (review r1 of CON-572: releasing here
                # re-froze the park with a fresh wave one tick later).
                # A SUSTAINED unreadable stretch is bounded by
                # ``_drain2_reconcile``: the gate stops refreshing the
                # record, so it goes stale and is closed with a resume.
                return TickOutcome.BLOCKED
            if early:
                # Early opportunism that finds nothing better simply stays
                # put: none of the must-move artifacts — the last-account
                # alert, the all-exhausted reset sleep — may fire off a
                # below-threshold tick (the account still has headroom;
                # nothing is forced). A signaled early episode still
                # releases: the park must not stay paused for a swap that
                # cannot happen.
                self._emit(
                    NoSwitchEvent(
                        reason="no-qualifying-candidate",
                        detail=(
                            "no candidate beats the active account by the "
                            "hysteresis margin; the early swap stays put"
                        ),
                    )
                )
                self._abandon_switch_intent(
                    trigger,
                    "no qualifying candidate for the early swap",
                    alert=False,
                )
                return TickOutcome.NO_ACTION
            if trigger == "consume-first":
                # Below the threshold and healthy: staying put is a correct
                # outcome, never a block. Distinguish *why* nothing qualified
                # so an opted-in user can see the strategy working (or inert).
                if active_reset_ts is None:
                    # The strictly-sooner filter skips every candidate when the
                    # active account's weekly reset is unknown — without this
                    # reason the strategy would look enabled while doing
                    # nothing, with no way to tell.
                    self._emit(
                        NoSwitchEvent(
                            reason="reset-unknown",
                            detail=(
                                "active account's weekly reset time is "
                                "unknown; consume-first is idle until it "
                                "is reported"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
                # Covers both "everyone resets later" and "sooner ones have no
                # room" — don't claim the active account resets first when the
                # real story may be exhausted candidates.
                self._emit(
                    NoSwitchEvent(
                        reason="already-consuming-soonest",
                        # Also covers the pool-shield's reverse-trade guard:
                        # a model-fresh candidate with room may exist and
                        # still not be a voluntary target from a burned host.
                        detail=(
                            "no eligible sooner-resetting account "
                            "(pool-shield may hold model-fresh hosts back)"
                        ),
                    )
                )
                return TickOutcome.NO_ACTION
            # "All exhausted" (and its bounded reset-aware sleep) only when it's
            # literally true: every candidate's usage is known and at its
            # limit. A candidate that merely failed the proactive hysteresis
            # gate, or one whose usage is unreadable this tick, can become
            # viable at any moment — and the active account can hit 100% and
            # need the at-limit escape — so those keep the normal cadence.
            candidate_headrooms = [headroom.get(n) for n in oauth_candidates]
            truly_exhausted = all(
                h is not None and h <= 0 for h in candidate_headrooms
            )
            if not truly_exhausted:
                self._emit(
                    NoSwitchEvent(
                        reason="no-qualifying-candidate",
                        detail=(
                            "no candidate is below the threshold and better "
                            "than the active account by the hysteresis "
                            "margin, or usage is unreadable this tick"
                        ),
                    )
                )
                self._abandon_switch_intent(trigger, "no qualifying candidate")
                return TickOutcome.BLOCKED
            self._blocked_wait_long = True
            earliest = self._earliest_recovery(usage)
            if earliest is not None:
                self._sleep_until_ts = earliest.timestamp() + RESET_SLACK_S
            self._emit(
                AllExhaustedEvent(
                    earliest_reset_at=(
                        earliest.isoformat().replace("+00:00", "Z")
                        if earliest
                        else None
                    )
                )
            )
            self._abandon_switch_intent(trigger, "every candidate exhausted")
            return TickOutcome.BLOCKED

        # -- live login slots (CON-2052) -----------------------------------
        # A candidate whose live `cswap run` session runs on its own login
        # family is not a target for ANY trigger: the default login landing
        # there puts one rotating refresh token in two stores with a writer
        # on each side (the CON-2030 class). Judged here — before the drain
        # gates and before dry-run's early exit: `_freshen_target` skipped
        # such a slot too, but only after the park had been paused for a
        # swap that could not happen, and dry-run never reached it at all.
        # Live incident 2026-09-04 (09:00Z, 09:47Z, 12:39Z): the home's
        # Fable window burned, the orchestrator's slot ranked first, and the
        # login landed on it three times in a day.
        ordered, skipped_live = self._drop_live_login_slots(ordered, trigger)
        if (
            not ordered
            and skipped_live
            and api_key_candidates
            and trigger != "consume-first"
            and not early
        ):
            # The metered API-key last resort (above) applies to a target
            # list the live-login filter emptied as well: an API-key slot
            # has no OAuth family to fork, so the filter never drops one.
            ordered = api_key_candidates
        if not ordered:
            self._emit(
                NoSwitchEvent(
                    reason="no-viable-target",
                    detail=self._live_login_detail(skipped_live),
                )
            )
            # Same law as the no-qualifying-candidate exits above: only a
            # must-move tick is down to its last account. A voluntary tick
            # (the early swap, consume-first) that found nothing takeable
            # stays put by choice — no last-account cry, NO_ACTION (review
            # r.1 of CON-2052: the alert fired on a 75%-healthy park).
            if early:
                self._abandon_switch_intent(
                    trigger,
                    "every qualifying candidate for the early swap hosts a "
                    "live login-family session",
                    alert=False,
                )
                return TickOutcome.NO_ACTION
            if trigger == "consume-first":
                return TickOutcome.NO_ACTION
            self._abandon_switch_intent(
                trigger,
                "every qualifying candidate hosts a live login-family session",
            )
            return TickOutcome.BLOCKED

        # A qualifying candidate exists: the switch intent is real again —
        # re-arm the last-account alert for the next drought.
        self._clear_last_account_alert()

        # -- drain gates ---------------------------------------------------
        # Run only now that a qualifying candidate exists (CON-572): a
        # pause signaled first would stop the park for a switch that may
        # not be possible at all.
        if trigger not in self._gated_triggers():
            if self._drain2_active_for(trigger):
                # Drain v2: create the park pause instead of waiting for
                # one — signal every mid-turn session to checkpoint,
                # confirm fixation from the roster, then swap. Channel
                # failure falls back to the passive v1 drain below.
                mode, drain2 = self._drain2_gate(trigger, early=early)
                if mode == "hold":
                    return TickOutcome.NO_ACTION
                if mode == "fallback":
                    drain2 = None
                    proceed, drain = self._drain_gate(trigger, active_headroom)
                    if not proceed:
                        return TickOutcome.NO_ACTION
            else:
                # Forced switches drain: a bounded wait for the same
                # silence, instead of landing at the first busy tick (every
                # live session on the account being left full-misses its
                # prompt cache). An at-limit switch off a window already at
                # 100% skips the wait inside the gate — a dead account has
                # no cache left to drain.
                proceed, drain = self._drain_gate(trigger, active_headroom)
                if not proceed:
                    return TickOutcome.NO_ACTION

        # -- freshen + switch ----------------------------------------------
        transient_failure = False
        for num in ordered:
            email = self.switcher.account_email(num)
            if trigger == "consume-first" or early:
                # The phase-2 refetch is best-effort: the collector refuses
                # accounts in failure backoff or claimed by a concurrent
                # poller, which then serve their stored entries. Consume-first
                # and the early swap are opportunistic, not escapes — never
                # act on stale data or slide to a worse-ranked target; hold
                # and retry next tick.
                entry = entries.get(num)
                if entry is None or not entry.fresh(self.clock()):
                    self._emit(
                        NoSwitchEvent(
                            reason="stale-usage",
                            detail=(
                                f"account {num} usage could not be refreshed "
                                "this tick (backoff or a concurrent poller); "
                                "retrying"
                            ),
                        )
                    )
                    return TickOutcome.NO_ACTION
            if self.dry_run:
                # Dry-run stops at the decision: no token refresh, no
                # quarantine writes — freshening is a mutation.
                return self._perform_with_drain2(
                    num, email, trigger, drain, drain2, early=early
                )
            status = self._freshen_target(num, email)
            if status == "identity-conflict":
                # The slot's credential is alive but belongs to a different
                # account — switching onto it would silently run the wrong
                # account. Quarantine (auto-released once a re-add replaces
                # the credential).
                self._quarantine(num, email, "identity-conflict")
                continue
            if status == "invalid_grant":
                self._quarantine(num, email, "invalid_grant")
                continue
            if status == "transient":
                transient_failure = True
                continue
            if status == "skip-live-session":
                continue
            return self._perform_with_drain2(
                num, email, trigger, drain, drain2, early=early
            )

        if transient_failure:
            # Deliberately NO release: the episode survives and the next
            # tick retries the swap — an orderly pause must not be thrown
            # away on a network blip.
            self._emit(
                ErrorEvent(
                    message="could not freshen any candidate (network?)",
                    transient=True,
                )
            )
            return TickOutcome.ERROR
        live_detail = self._live_login_detail(skipped_live)
        self._emit(
            NoSwitchEvent(
                reason="no-viable-target",
                detail=(
                    f"every ranked target failed to freshen; {live_detail}"
                    if live_detail
                    else ""
                ),
            )
        )
        self._drain2_release(drain2, "every ranked target failed to freshen")
        return TickOutcome.BLOCKED

    def _rank_candidates(
        self,
        *,
        trigger: str,
        consume_first: bool,
        oauth_candidates: list[str],
        usage: dict[str, dict | str | None],
        headroom: dict[str, float | None],
        base_headroom: dict[str, float | None],
        current: str,
        active_headroom: float | None,
        settings: AutoSwitchSettings,
        now: float,
        early: bool = False,
        pin_active: bool = False,
    ) -> tuple[list[str], bool, float | None]:
        """Filter and rank OAuth candidates for this tick's trigger.

        Returns ``(ordered, any_known, active_reset_ts)``. Pure — no emits,
        no state writes — so the consume-first two-phase commit can run it
        twice per tick: on the stored snapshot to decide provisionally, then
        on the escalated refetch to re-verify before switching.

        ``pin_active`` (CON-1581) stands the pool-shield down: with an
        active home pin the login is judged model-aware everywhere — the
        pin already guarantees the fleet a resting login, and a voluntary
        landing on a model-burned host would park the pinned login's own
        model-bound work at a wall (and, on the home itself, ping-pong
        against the model-window escape).
        """
        # consume-first ranks by soonest weekly reset; a proactive (below-
        # threshold) target must reset strictly sooner than where we are.
        active_reset_ts = (
            _seven_day_reset_ts(usage.get(current), now) if consume_first else None
        )
        # Pool-shield (CON-712): with a configured model, voluntary landings
        # under consume-first are judged on the account-wide 5h/7d axis and
        # PREFER hosts whose per-model window is already burned past the
        # threshold — the rotation parks where model-pinned fleet work has
        # nothing left to lose, and model-fresh accounts stay free for it.
        # The model window still binds everywhere else: a maxed binding
        # window (h <= 0) is never a target, and escape landings keep
        # ranking model-healthy accounts first.
        shield = consume_first and bool(self._models) and not pin_active
        voluntary = trigger in ("proactive", "consume-first")
        active_burned = False
        # The active account's headroom on the axis voluntary moves are
        # judged by: account-wide under the shield, model-aware otherwise.
        # Keeps the early-swap hysteresis comparison on ONE axis (review
        # r1 nit: the trigger judged base while the margin judged model).
        active_voluntary_h = active_headroom
        if shield and active_headroom is not None and active_headroom > 0:
            active_base = base_headroom.get(current)
            if active_base is not None:
                active_voluntary_h = active_base
            active_burned = (
                active_base is not None
                and (100.0 - active_headroom) >= settings.threshold
                and (100.0 - active_base) < settings.threshold
            )
        qualifying: list[tuple[tuple, str]] = []
        any_known = False
        for num in oauth_candidates:
            h = headroom.get(num)
            if h is None:
                continue
            any_known = True
            if h <= 0:
                continue  # itself at its limit — never a target
            reset_ts = (
                _seven_day_reset_ts(usage.get(num), now) if consume_first else None
            )
            # An account at/over the threshold re-triggers on the very next
            # tick — voluntary triggers refuse it outright; escapes keep it
            # as a last resort but rank every healthy landing first (the
            # unhealthy flag leads the sort key below). 2026-07-31 incident:
            # an at-limit escape under consume-first ordering landed on a
            # 1%-headroom account while 38%/60% accounts sat idle.
            unhealthy = (100.0 - h) >= settings.threshold
            burned = False
            # The candidate's headroom on the voluntary axis (account-wide
            # under the shield) — pairs with active_voluntary_h in the
            # early-swap hysteresis below.
            judge_h = h
            if shield and voluntary:
                # Health for a voluntary landing is account-wide; "burned"
                # marks the preferred class: model window past the
                # threshold while the account itself still has room.
                base_h = base_headroom.get(num)
                if base_h is not None:
                    judge_h = base_h
                    burned = unhealthy and (100.0 - base_h) < settings.threshold
                    unhealthy = (100.0 - base_h) >= settings.threshold
            if voluntary:
                # Landing must be healthy. At-limit and failover are escapes
                # that skip this whole block — any account with real headroom
                # beats a blocked or dead one.
                if unhealthy:
                    continue
                if consume_first:
                    # A rescue move frees a model-fresh active for the fleet
                    # by parking on a burned host — the shield's whole point,
                    # so it outranks reset ordering and the early-swap
                    # hysteresis. No ping-pong is possible: once the active
                    # host is burned, rescue never fires again, and BOTH
                    # below-threshold voluntary paths — the nudge and the
                    # early swap — refuse the reverse trade right below.
                    rescue = burned and not active_burned
                    if (
                        (trigger == "consume-first" or early)
                        and not burned
                        and active_burned
                    ):
                        # Never trade a burned host for a model-fresh one
                        # below the threshold — that is the hoarding this
                        # shield exists to stop. The early swap rides the
                        # proactive trigger but decides below the threshold
                        # too, so it obeys the same law (review r1: without
                        # this the early band cycled burned -> fresh ->
                        # rescue -> burned on the cooldown cadence).
                        continue
                    if trigger == "consume-first":
                        # Purely proactive on reset ordering: below the
                        # threshold, only move to accounts whose weekly
                        # window resets sooner than the active one (above
                        # the threshold we must move, so any healthy
                        # account qualifies and the sort picks soonest).
                        if not rescue and (
                            reset_ts is None
                            or active_reset_ts is None
                            or reset_ts >= active_reset_ts
                        ):
                            continue
                    # The early swap is voluntary under consume-first too:
                    # unlike the at-threshold proactive it must clear the
                    # same hysteresis margin `best` applies — a 2% win must
                    # never freeze and move the park, or accounts hovering
                    # together in the early band ping-pong every cooldown
                    # at full park price (review r2). Both sides compare on
                    # the voluntary axis (account-wide under the shield).
                    if (
                        early
                        and not rescue
                        and active_voluntary_h is not None
                        and judge_h - active_voluntary_h
                        < settings.hysteresis_pct
                    ):
                        continue
                elif active_headroom is not None:
                    # best: the candidate must beat the active account by the
                    # full hysteresis margin (a one-way move like 99%→89%
                    # qualifies; near-line pairs can't flap back).
                    if h - active_headroom < settings.hysteresis_pct:
                        continue
            # Healthy landings before unhealthy ones (only escapes ever keep
            # unhealthy candidates); within each group the strategy's own
            # order applies.
            if consume_first:
                # Burned hosts first (pool-shield; escapes never set the
                # flag), then soonest weekly reset (unknown resets sort
                # last), most headroom breaks ties, then sequence order.
                key: tuple = (
                    unhealthy,
                    not burned,
                    reset_ts if reset_ts is not None else float("inf"),
                    -h,
                )
            else:
                key = (unhealthy, -h)
            qualifying.append((key, num))
        # Ascending by the strategy's key; list order (sequence order) breaks ties.
        qualifying.sort(key=lambda t: t[0])
        return [num for _, num in qualifying], any_known, active_reset_ts

    def _early_swap_fires(
        self, utilization: float, settings: AutoSwitchSettings, state: dict
    ) -> bool:
        """Whether the below-threshold early swap fires this tick (CON-582).

        The migration price of a swap is the sum of the live contexts on
        the account being left — it grows with the park — so at high
        utilization with only a few sessions mid-turn, leaving NOW is
        strictly cheaper than the same forced move at the threshold under
        a full park (the 15-08 episode: 10.2M cache-creation tokens for 12
        sessions at once). Voluntary economics, never an escape: cooldown
        and the quiet gate hold it, an unreadable roster declines it (a
        park that can't be measured can't be called small), and a park
        bigger than ``earlySwapMaxBusy`` waits for the threshold.
        Interactive busy sessions count toward the size — they pay the
        migration like everyone else.

        One decision per episode: a live signaled drain-v2 episode this
        trigger started keeps firing (the pause is already bought;
        abandoning it because one more session woke up would pay the pause
        twice, and the same park would be re-frozen at the threshold
        anyway). The stickiness ends with the episode — or when
        utilization leaves the early band, after which the record goes
        stale and ``_drain2_reconcile`` releases it (same law as a window
        reset under a threshold episode).
        """
        early_threshold = settings.early_swap_threshold
        if early_threshold <= 0 or utilization < early_threshold:
            return False
        record = self._read_drain2()
        if (
            record is not None
            and record.get("phase") == "signaled"
            and record.get("early")
        ):
            updated = record.get("updatedAt")
            if (
                isinstance(updated, (int, float))
                and self.clock() - updated <= DRAIN_STALE_GAP_S
            ):
                return True
        if self._in_cooldown(state):
            return False
        if "proactive" in self._gated_triggers():
            quiet, _ = self._session_quiet()
            if not quiet:
                # The quiet gate would hold the switch anyway — don't spend
                # a roster subprocess proving a park size nobody can use.
                return False
        roster = self._park_roster()
        if roster is None:
            return False
        targets, interactive = self._drain2_targets(roster)
        busy = len(targets) + interactive
        if busy > settings.early_swap_max_busy:
            return False
        self._emit(
            EarlySwapEvent(
                utilization_pct=utilization,
                early_threshold=early_threshold,
                busy_sessions=busy,
                max_busy=settings.early_swap_max_busy,
            )
        )
        return True

    def _move_cost(
        self, sessions: list[tuple[str, str]]
    ) -> context_cost.MoveCost:
        """Estimate the migration price of ``sessions`` (name, session_id)
        from their transcripts; the estimator must never break a tick."""
        try:
            return context_cost.estimate_move_cost(
                self.claude_projects_dir, sessions
            )
        except Exception as e:  # pragma: no cover - defensive boundary
            _logger.debug("move-cost estimate raised: %r", e)
            return context_cost.MoveCost(
                per_session={name: None for name, _ in sessions}
            )

    # -- adaptive usage scheduling ---------------------------------------------

    def _collect_scheduled_usage(
        self,
        current: str,
        quarantined: set[str] = frozenset(),
        *,
        threshold: float | None = None,
    ) -> tuple[dict, dict[str, dict | str | None], dict[str, float | None]]:
        """Two-phase usage collection with an O(1) baseline.

        Phase A fetches the active account (when its persisted poll plan says
        it is due — poll_policy's urgent mode is what tightens that cadence
        near the band) plus ONE due candidate (the one with the stalest data
        — never-fetched first, then oldest fetch); everyone else is served
        from the usage store. Phase B refetches ALL candidates and recomputes
        before any switch decision when a switch could be near: active
        utilization within ``ESCALATION_MARGIN_PCT`` of the threshold, or
        active usage unknown (failover must not run on stale candidate data).
        At-limit, proactive, and ordinary unknown-usage failover selection
        never runs on the pre-escalation snapshot — those triggers imply the
        escalation condition (the deliberate exception: an owned-and-expired
        active is excluded above, so a post-idle-hold failover can run
        without escalating). The consume-first trigger can fire outside the
        escalation band, so it instead decides *provisionally* on the stored
        snapshot and, only when a switch would fire, re-runs an escalated
        collection and re-verifies the choice in ``_tick_inner`` (two-phase
        commit), plus a per-target ``UsageEntry.fresh`` gate before
        performing.

        Stalest-first needs no rotation cursor: it reads the persisted store,
        so the loop and cron-driven ``--once`` runs schedule identically.
        Backoff (``backoffUntil``) is enforced by the collector even for the
        active account — a Retry-After must never be defeated — and during an
        idle-hold no candidate is polled at all (slow crawl for everything).
        Adapted cadences are persisted by the collector itself after each
        fetch (shared with every other surface), not by the engine.

        Returns ``(entries, usage, headroom)`` where ``usage`` carries
        decision values and ``headroom`` the derived headroom per account.
        """
        now = self.clock()
        # Quarantined accounts can never be switch targets, so spending the
        # single alternate poll slot (or an escalation fetch) on one is wasted.
        candidates = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n != current and n not in quarantined
        ]

        pre = self.switcher.usage_entries_by_account(fetch=set())
        plan: set[str] = set()
        active_pre = pre.get(current)
        # The active account is nominated when never fetched, poll-due per its
        # persisted plan, or (no plan yet) past the normal cadence floor. The
        # collector's reserve() honors due-ness even inside the serve TTL, so
        # an urgent plan (60s while burning near the band) actually fetches.
        # A candidate-style plan (slower than any active plan can be) left
        # over from a role change the switcher never saw (e.g. a manual
        # login) is overridden past the active age cap. Exhausted accounts
        # carry their own bounded plan and become due normally.
        stale_candidate_plan = (
            active_pre is not None
            and active_pre.age_s is not None
            and active_pre.age_s >= poll_policy.ACTIVE_MAX_INTERVAL_S
            and (active_pre.poll_interval_s or 0.0)
            > poll_policy.ACTIVE_MAX_INTERVAL_S
            and (binding_pct(active_pre.last_good, self._models) or 0.0) < 100.0
        )
        overslept_plan = (
            active_pre is not None
            and plan_oversleeps_interval(active_pre, now)
        )
        if (
            active_pre is None
            or active_pre.age_s is None
            or stale_candidate_plan
            or overslept_plan
            or (
                active_pre.next_poll_at is not None
                and now >= active_pre.next_poll_at
            )
            or (
                active_pre.next_poll_at is None
                and active_pre.age_s >= poll_policy.MIN_INTERVAL_S
            )
        ):
            plan.add(current)
        if self._idle_hold_since is None:
            pick = due_candidate(candidates, pre, now)
            if pick is not None:
                plan.add(pick)
        entries = self.switcher.usage_entries_by_account(
            fetch=plan,
            # A candidate-style plan on the active slot is deliberately
            # overridden after the active age cap; every other baseline
            # nomination preserves a valid future plan under the store lock.
            scheduled=not stale_candidate_plan,
        )
        usage = {num: entry.decision_value() for num, entry in entries.items()}

        active_value = usage.get(current)
        active_headroom = oauth.account_headroom(
            active_value if isinstance(active_value, dict) else None, self._models
        )
        # The caller's tick-snapshotted threshold, so one tick fetches and
        # decides on the same value even if apply_threshold() lands mid-tick.
        if threshold is None:
            threshold = self.settings.threshold
        escalate = bool(candidates) and (
            (active_headroom is None and active_value != USAGE_TOKEN_EXPIRED)
            or (
                active_headroom is not None
                and 100.0 - active_headroom >= threshold - ESCALATION_MARGIN_PCT
            )
        )
        if escalate:
            escalation_fetch = {current, *candidates}
            # Escalation may beat ordinary candidate plans to obtain a fresh
            # switch decision, but a decision-trusted exhausted row cannot be
            # a target. Preserve any wider post-429 plan instead of refetching
            # that token at the bounded all-exhausted wake cadence.
            for num in tuple(escalation_fetch):
                entry = entries.get(num)
                value = usage.get(num)
                planned_headroom = oauth.account_headroom(
                    value if isinstance(value, dict) else None, self._models
                )
                if (
                    entry is not None
                    and entry.next_poll_at is not None
                    and now < entry.next_poll_at
                    and (entry.poll_interval_s or 0.0)
                    > poll_policy.EXHAUSTED_INTERVAL_S
                    and planned_headroom is not None
                    and planned_headroom <= 0
                ):
                    escalation_fetch.remove(num)
            entries = self.switcher.usage_entries_by_account(
                fetch=escalation_fetch
            )
            usage = {num: entry.decision_value() for num, entry in entries.items()}

        headroom = _headroom_by_account(usage, self._models)
        return entries, usage, headroom

    def _perform_with_drain2(
        self,
        number: str,
        email: str,
        trigger: str,
        drain: dict | None,
        drain2: dict | None,
        early: bool = False,
    ) -> TickOutcome:
        """Perform the switch, then complete any drain-v2 episode behind it
        (mark swapped → verify the new account → resume wave). A failed
        switch leaves the episode in place, so the retry keeps its label
        and its fixation bookkeeping (acked/soft/streaks)."""
        outcome = self._perform(
            number, email, trigger, drain=drain, drain2=drain2, early=early
        )
        if outcome is TickOutcome.SWITCHED and drain2 is not None:
            self._drain2_mark_swapped(number)
            self._drain2_finish()
        return outcome

    def _perform(
        self,
        number: str,
        email: str,
        trigger: str,
        drain: dict | None = None,
        drain2: dict | None = None,
        early: bool = False,
    ) -> TickOutcome:
        if self.dry_run:
            current = self.switcher.current_account_number()
            current_email = self.switcher.account_email(current) if current else ""
            quiet, _ = self._session_quiet()
            self._emit(
                SwitchEvent(
                    trigger=trigger,
                    from_ref=_ref(current, current_email) if current else None,
                    to_ref=_ref(number, email),
                    dry_run=True,
                    gate="quiet" if quiet else "forced",
                    drain=drain,
                    drain2=drain2,
                    early=early,
                )
            )
            return TickOutcome.SWITCHED

        # Hold the state lock across the whole recheck -> switch -> record
        # sequence so two concurrent engines (loop + cron --once) make one
        # serialized decision: the loser re-reads the winner's lastSwitchAt
        # and backs off instead of double-switching. No deadlock cycle: the
        # switch path (cswap FileLock + Claude Code locks) never takes the
        # state lock.
        with self._state_lock():
            state = self._read_state()
            # Same law as the tick-top gate: cooldown holds only the
            # voluntary consume-first rotation; proactive is already past
            # the threshold and must go.
            if trigger == "consume-first" and self._in_cooldown(state):
                self._emit(NoSwitchEvent(reason="cooldown"))
                return TickOutcome.NO_ACTION

            # Re-measure traffic right before the swap: candidate freshening
            # (a network round-trip) sits between the tick-top gate and here,
            # and a session waking up in that window must still block a
            # voluntary switch. The same measurement labels the event, so
            # every logged switch carries the traffic state it landed in.
            quiet, detail = self._session_quiet()
            if trigger in self._gated_triggers() and not quiet:
                self._emit(NoSwitchEvent(reason="sessions-active", detail=detail))
                return TickOutcome.NO_ACTION

            result = self.switcher.switch_to(number, json_output=True)
            if not result or not result.get("switched"):
                self._emit(
                    NoSwitchEvent(
                        reason="already-active",
                        detail=(result or {}).get("reason", ""),
                    )
                )
                return TickOutcome.NO_ACTION

            state["schemaVersion"] = STATE_SCHEMA_VERSION
            state["lastSwitchAt"] = self.clock()
            state["lastSwitchTo"] = number
            # Why the daemon switched, for human-facing consumers (the Quota
            # panel). Additive fields — same contract as SwitchEvent payloads:
            # no schema bump, readers ignore unknown keys.
            state["lastSwitchTrigger"] = trigger
            state["lastSwitchGate"] = "quiet" if quiet else "forced"
            # A landed switch closes any drain episode — the traffic it was
            # waiting out belongs to the account we just left.
            state.pop("drain", None)
            atomic_write_json(self.state_path, state)
            # Stamp the agent-facing swap marker the drain2 STOP text tells
            # sessions to watch (`[ "$(cat marker)" -ge signal_epoch ]`).
            # Every real switch counts — a v1 swap ends a pause condition
            # just the same. Best-effort: never breaks a landed switch.
            try:
                self._drain2_marker_path().write_text(
                    f"{int(self.clock())}\n", encoding="utf-8"
                )
            except OSError as e:
                _logger.debug("drain2 switch marker write failed: %r", e)

        self._emit(
            SwitchEvent(
                trigger=trigger,
                from_ref=result.get("from"),
                to_ref=result.get("to"),
                warnings=result.get("warnings", []),
                gate="quiet" if quiet else "forced",
                drain=drain,
                drain2=drain2,
                early=early,
            )
        )
        return TickOutcome.SWITCHED

    # -- helpers --------------------------------------------------------------

    def _in_cooldown(self, state: dict) -> bool:
        last = state.get("lastSwitchAt")
        if not isinstance(last, (int, float)):
            return False
        return (self.clock() - last) < self.settings.cooldown_seconds

    def _gated_triggers(self) -> tuple[str, ...]:
        """Which triggers wait for transcript silence.

        ``autoswitch.switchUnderLoad`` releases only ``proactive`` — the
        threshold is already crossed there, so the choice is "swap now and
        lose prompt caches" against "ride into the wall and lose in-flight
        agents". ``consume-first`` is a below-threshold optimization with
        nothing to escape, so it stays gated under every setting — and so
        is ``return-home`` (CON-1070): the login is working where it is,
        the return is a correction with nothing burning behind it.
        """
        if self.settings.switch_under_load:
            return ("consume-first", "return-home")
        return ("proactive", "consume-first", "return-home")

    # -- home pin (CON-1070) ----------------------------------------------

    def _model_window_burned(
        self, value: dict | str | None, *, threshold: float
    ) -> tuple[str, float] | None:
        """The configured scoped model window at/over the threshold, if any.

        The home pin's model judgment (CON-1581): judged with the same
        ``threshold`` as the proactive escape, so "burned" means the same
        thing everywhere — the caller passes its tick-snapshotted value
        (``apply_threshold()`` can land mid-tick from the TUI thread, and
        one tick must judge every window on one number). Returns the worst
        offending ``(window name, pct)``, or ``None`` when every
        configured scoped window is below the threshold (or none is
        configured/reported). The account-wide 5h/7d windows are
        deliberately not judged here — at home they are the user's own
        wall to wait out (CON-1070).
        """
        if not self._models or not isinstance(value, dict):
            return None
        worst: tuple[str, float] | None = None
        for label, pct, _ in oauth.relevant_windows(value, self._models):
            if label in ("5h", "7d"):
                continue
            if pct >= threshold and (worst is None or pct > worst[1]):
                worst = (label, pct)
        return worst

    def _home_slot(self, settings: AutoSwitchSettings) -> str | None:
        """Resolve ``autoswitch.homeAccount`` to a managed slot, or None.

        A value that names no managed account leaves the pin inert and says
        so once — a pin that looks set while gating nothing is the
        ``autoswitch.model`` typo class all over again.
        """
        ident = settings.home_account
        if not ident:
            self._home_warned = None
            return None
        try:
            number, _email, _org = self.switcher.resolve_account(ident)
        except ClaudeSwitchError as e:
            self._warn_home_once(
                f"unknown:{ident}",
                f"autoswitch.homeAccount: {e} — the pin is inert until it "
                "names a managed account",
            )
            return None
        if self._home_warned and self._home_warned.startswith("unknown:"):
            self._home_warned = None
        return number

    def _warn_home_once(self, key: str, message: str) -> None:
        if self._home_warned == key:
            return
        self._home_warned = key
        self._emit(ConfigWarningEvent(message=message))

    @staticmethod
    def _gauge_rate_limited(entry, now: float) -> bool:
        """Whether a slot's usage is unknown only because the usage endpoint
        throttled the token (``http-429`` with Retry-After, CON-2267).

        True only while the honoured 429 backoff is live: the store writes
        ``lastError == "http-429"`` together with ``backoffUntil`` on the 429
        and clears both on the next success; any later failure rewrites
        ``lastError`` with its own cause. A sentinel on the entry (no
        credentials, token expired, relogin required) is the collector's
        verdict on the credentials themselves and wins — the stale 429 on
        the row is not a proof of life for a login whose tokens are gone
        (review r1). A 429 is the server saying "slow down", which it can
        only say to a token it recognised — alive by construction, unlike
        401 / invalid_grant or a network cause, where nothing proves life.
        """
        if entry is None or getattr(entry, "sentinel", None) is not None:
            return False
        if getattr(entry, "last_error", None) != "http-429":
            return False
        until = getattr(entry, "backoff_until", None)
        return until is not None and now < until

    def _home_inert(self, home: str, quarantined: set[str]) -> bool:
        """Whether the pin is switched off this tick, wherever the login is.

        A disabled home is the user's explicit hold-out and wins over the
        pin (said once, not every tick); a quarantined home already failed
        a landing. Either way the plain rotation judges the tick — also
        when the login currently sits on the home slot (review r1: a
        disabled active home must leave by the threshold, as without a pin).
        """
        if self.switcher.is_account_disabled(home):
            self._warn_home_once(
                f"disabled:{home}",
                f"autoswitch.homeAccount: Account-{home} is disabled — the "
                f"pin is inert until 'cswap enable {home}'",
            )
            return True
        if self._home_warned and self._home_warned.startswith("disabled:"):
            self._home_warned = None
        return home in quarantined

    def _return_home(
        self,
        home: str,
        *,
        quarantined: set[str],
        usage: dict[str, dict | str | None],
    ) -> TickOutcome | None:
        """Bring the live login back to the pinned slot once it proves alive.

        Returns the tick's outcome when the return landed (or was refused by
        the switch itself), and ``None`` whenever it cannot land this tick —
        no readable usage (no proof of life: a stale backup is exactly what
        a hand-made switch onto the home slot died on), session traffic, a
        ``cswap run`` session holding the slot, a network blip, or a dead
        lineage just quarantined. ``None`` hands the tick to the plain
        rotation, which keeps judging the slot the login is on: its at-limit
        and failover escapes must not be silenced by a return that is
        merely waiting (review r1). A wait says why in one
        ``return-home-wait`` no-switch; a quarantine joins ``quarantined``
        in place so the same tick's rotation never lands on the slot the
        return just proved dead.

        Proof of life is positive and live: readable usage now, then the
        same freshen the daemon gives every target (a live refresh with
        quarantine on a dead lineage) — never a timer or a cooldown
        expiring, which is how failback flaps. The home's scoped model
        window is deliberately not judged (CON-2069): the login belongs
        home whatever that window reads — the couriers there ride the
        model ladder, and the slot the login squats on meanwhile is a
        fleet seat (the CON-1581 ``home-model-burned`` wait is gone).
        """
        value = usage.get(home)
        if value is None:
            # Not served this tick (adaptive cadence), and not a dead-token
            # sentinel: one targeted fetch. The collector's backoff and
            # claims still apply, so a dead home is never hammered.
            entry = self.switcher.usage_entries_by_account(fetch={home}).get(home)
            value = entry.decision_value() if entry is not None else None
        if _headroom_by_account({home: value}, self._models).get(home) is None:
            return None

        def wait(detail: str) -> None:
            self._emit(
                NoSwitchEvent(
                    reason="return-home-wait",
                    detail=f"the return to Account-{home} waits: {detail}",
                )
            )

        quiet, detail = self._session_quiet()
        if not quiet:
            wait(detail)
            return None
        email = self.switcher.account_email(home)
        if self.dry_run:
            return self._perform_with_drain2(home, email, "return-home", None, None)
        status = self._freshen_target(home, email)
        if status in ("identity-conflict", "invalid_grant"):
            self._quarantine(home, email, status)
            quarantined.add(home)
            return None
        if status == "transient":
            wait(
                "its stored login could not be freshened or healed this tick "
                "(network, credential locks held elsewhere, or an unreadable "
                "keychain entry)"
            )
            return None
        if status == "skip-live-session":
            # A ``cswap run`` session owns this slot's token in its own
            # profile; making it the default login too would fork the
            # lineage (the stale-copy class).
            wait(f"a 'cswap run' session holds Account-{home}")
            return None
        return self._perform_with_drain2(home, email, "return-home", None, None)

    def _drain_gate(
        self, trigger: str, active_headroom: float | None
    ) -> tuple[bool, dict | None]:
        """Bounded quiet-wait ("drain") before a forced switch lands.

        Forced triggers — at-limit, failover, and proactive under
        ``switchUnderLoad`` — bypass the voluntary quiet gate, so without a
        drain they swap at the first tick and full-miss the prompt cache of
        every session running on the account being left. With
        ``drain_timeout_seconds`` set, the engine instead holds the swap and
        re-checks each tick (the poll step) for QUIET_WINDOW_S of transcript
        silence. The wait is bounded: an account pinned at its limit breaks
        live agents harder than a cache miss does, so at the ceiling the
        switch proceeds anyway, after a one-per-episode DrainTimeoutEvent.

        ``active_headroom`` is the binding-window headroom of the account
        being left (None = unknown). At or under 0 the wait is skipped
        outright: the window is at 100%, every call on the account is
        already failing, so transcript silence measures how long the dying
        takes — not a cache worth protecting (CON-486: 417s of drain-wait
        on a dead account while a background reviewer burned itself to the
        session limit). Agents cut mid-call recover via the wake heal.

        Returns ``(proceed, drain_info)``. ``proceed=False`` means hold this
        tick (a ``drain-wait`` line was emitted). ``drain_info`` is the
        additive SwitchEvent payload when an episode preceded the swap:
        ``{"outcome": "go"|"timeout", "waitedSeconds": int}``; None when the
        swap needed no wait. The episode lives in the state file (shared
        with cron ``--once`` ticks) — in memory under dry-run, which must
        not write state.
        """
        max_wait = self.settings.drain_timeout_seconds
        if max_wait <= 0:
            return True, None
        if active_headroom is not None and active_headroom <= 0.0:
            _logger.info(
                "Binding window of the active account is at 100%%; "
                "skipping the drain wait — calls there are already failing"
            )
            return True, None
        now = self.clock()
        latest = latest_session_activity_ts(self.claude_projects_dir)
        record = self._read_drain()
        if record is not None:
            started = record.get("startedAt")
            updated = record.get("updatedAt")
            if (
                not isinstance(started, (int, float))
                or not isinstance(updated, (int, float))
                or now - updated > DRAIN_STALE_GAP_S
            ):
                record = None  # a previous episode's leftovers
        if latest is None or now - latest >= QUIET_WINDOW_S:
            if record is None:
                return True, None
            # Keep the record: the swap can still fail past this gate
            # (freshen hiccup, no viable target), and the episode must not
            # restart from zero nor lose its drain label on the retry. The
            # switch that lands clears it (_perform); an episode that ends
            # without one ages out via DRAIN_STALE_GAP_S.
            self._write_drain({**record, "updatedAt": now})
            return True, {
                "outcome": "go",
                "waitedSeconds": int(now - record["startedAt"]),
            }
        busy = f"last session write {now - latest:.0f}s ago"
        if record is None:
            self._write_drain(
                {"startedAt": now, "updatedAt": now, "trigger": trigger}
            )
            self._emit(
                NoSwitchEvent(
                    reason="drain-wait",
                    detail=(
                        f"{busy}; {trigger} switch drains up to "
                        f"{max_wait:.0f}s for "
                        f"{QUIET_WINDOW_S / 60:.0f}m of silence"
                    ),
                )
            )
            return False, None
        waited = now - record["startedAt"]
        if waited < max_wait:
            self._write_drain({**record, "updatedAt": now, "trigger": trigger})
            self._emit(
                NoSwitchEvent(
                    reason="drain-wait",
                    detail=f"{busy}; drained {waited:.0f}s of {max_wait:.0f}s",
                )
            )
            return False, None
        # Ceiling. Warn once per episode — a second engine racing this
        # check-then-act can double-emit the WARN (benign: an extra log
        # line; the switch itself is serialized in _perform). The record
        # survives until a switch lands (_perform clears it), so a blocked
        # or transiently-failed attempt retries next tick without
        # re-waiting a full ceiling; an episode that ends without a switch
        # ages out via DRAIN_STALE_GAP_S.
        if not record.get("timeoutWarned"):
            self._emit(
                DrainTimeoutEvent(
                    trigger=trigger,
                    waited_seconds=int(waited),
                    max_wait_seconds=int(max_wait),
                    detail=busy,
                )
            )
        self._write_drain({**record, "updatedAt": now, "timeoutWarned": True})
        return True, {"outcome": "timeout", "waitedSeconds": int(waited)}

    def _read_drain(self) -> dict | None:
        if self.dry_run:
            return self._drain_mem
        value = self._read_state().get("drain")
        return value if isinstance(value, dict) else None

    def _write_drain(self, record: dict | None) -> None:
        if self.dry_run:
            self._drain_mem = record
            return

        def set_drain(state: dict) -> None:
            if record is None:
                state.pop("drain", None)
            else:
                state["drain"] = record

        self._mutate_state(set_drain)

    # -- drain v2 (active checkpoint) --------------------------------------

    def _drain2_active_for(self, trigger: str) -> bool:
        """Whether this forced trigger goes through the active checkpoint.

        Proactive only: it is a forewarning below the hard limit, worth a
        couple of minutes of orderly pause. At-limit and failover mean calls
        are already failing (or the account is dead) — orchestrating a pause
        spends time the park doesn't have, so failover keeps the passive
        drain and at-limit skips even that (CON-486: its binding window is
        at 100%, so no wait has a cache left to protect).
        A recent channel failure stands v2 down (see DRAIN2_BACKOFF_S), and
        so does a recent no-swap release (CON-461): pausing the park again
        while there is still nobody to switch to would thrash it.
        """
        now = self.clock()
        return (
            trigger == "proactive"
            and self.settings.drain2_wait_seconds > 0
            and now >= self._park_backoff_until
            and not self._drain2_release_backoff_active(now)
        )

    def _drain2_release_backoff_active(self, now: float) -> bool:
        """Whether a no-swap release recently stood v2 down.

        The truth is in the state file: the episode record itself lives
        there precisely so cron ``--once`` ticks and daemon restarts share
        it — a backoff that died with the process would let every fresh
        process re-signal a pause into the just-released park and thrash
        it in 180s-pause cycles for as long as no candidate exists
        (review r1 finding 1). The in-memory mirror answers for dry-run
        (which never touches state) and skips the file read once this
        process has seen the value.
        """
        if now < self._drain2_release_until:
            return True
        if self.dry_run:
            return False
        value = self._read_state().get("drain2ReleaseUntil")
        if isinstance(value, (int, float)) and now < value:
            self._drain2_release_until = value
            return True
        return False

    def _park_channel(self) -> ParkChannel:
        if self._park is None:
            self._park = ParkChannel(
                # Working directory only — the roster name is
                # park.HERALD_NAME ("Jerry"), set on the herald's argv.
                # Renaming the dir would orphan the live one under
                # backup_dir for no visible gain.
                herald_cwd=self.switcher.backup_dir / "drain2-herald"
            )
        return self._park

    def _park_roster(self) -> list[ParkSession] | None:
        try:
            return self._park_channel().roster()
        except Exception as e:  # the channel must never break a tick
            _logger.debug("park roster raised: %r", e)
            return None

    def _drain2_wave(self, targets: list[str], message: str) -> WaveResult:
        """One herald wave; dry-run pretends success without spawning."""
        if self.dry_run:
            return WaveResult(ok=True, delivered=[], detail="dry-run")
        try:
            return self._park_channel().send_wave(targets, message)
        except Exception as e:  # the channel must never break a tick
            return WaveResult(ok=False, detail=f"{type(e).__name__}: {e}")

    def _drain2_marker_path(self) -> Path:
        return self.switcher.backup_dir / DRAIN2_SWITCH_MARKER_NAME

    def _drain2_ack_dir(self) -> Path:
        return self.switcher.backup_dir / DRAIN2_ACK_DIR_NAME

    def _drain2_stop_message(self) -> str:
        """The STOP wave text with this wave's paths and epoch baked in.

        The epoch is the guard against the late-freezer trap: an agent that
        processes the wave after the swap compares the marker (stamped at
        switch time) against the wave's own epoch and continues instead of
        freezing into a closed episode.
        """
        return DRAIN2_STOP_MESSAGE.format(
            marker=self._drain2_marker_path(),
            ack_dir=self._drain2_ack_dir(),
            signal_epoch=int(self.clock()),
        )

    @staticmethod
    def _drain2_ack_name_ok(name: str) -> bool:
        """Session names come from the roster; only plain filenames may
        touch the ack dir (no separators, no dot-prefixed entries, no NUL —
        a NUL survives the ``Path(name).name`` check and then raises
        ValueError, not OSError, from stat/unlink)."""
        return (
            bool(name)
            and "\x00" not in name
            and Path(name).name == name
            and not name.startswith(".")
        )

    def _drain2_acked(self, name: str, since: float) -> bool:
        """Whether a checkpoint receipt fresh for this episode exists."""
        if not self._drain2_ack_name_ok(name):
            return False
        try:
            st = (self._drain2_ack_dir() / name).stat()
        except (OSError, ValueError):
            return False
        return st.st_mtime >= since

    def _drain2_clear_acks(self, names: list[str]) -> None:
        """Drop receipts left over from previous episodes (best-effort;
        the mtime-vs-startedAt judge stays correct even when this fails)."""
        for name in names:
            if not self._drain2_ack_name_ok(name):
                continue
            try:
                (self._drain2_ack_dir() / name).unlink()
            except (OSError, ValueError):
                pass

    def _read_drain2(self) -> dict | None:
        if self.dry_run:
            return self._drain2_mem
        value = self._read_state().get("drain2")
        return value if isinstance(value, dict) else None

    def _write_drain2(self, record: dict | None) -> None:
        if self.dry_run:
            self._drain2_mem = record
            return

        def set_record(state: dict) -> None:
            if record is None:
                state.pop("drain2", None)
            else:
                state["drain2"] = record

        self._mutate_state(set_record)

    def _drain2_go_unavailable(self, reason: str) -> None:
        """Channel failure: log once, stand v2 down, hand off to v1.

        Any signaled record is dropped — the passive drain owns the episode
        from here, and already-signaled sessions resume on their own via the
        wave text's self-rescue clause.
        """
        self._emit(Drain2UnavailableEvent(reason=reason))
        self._park_backoff_until = self.clock() + DRAIN2_BACKOFF_S
        record = self._read_drain2()
        if record is not None and record.get("phase") == "signaled":
            self._write_drain2(None)

    def _drain2_targets(
        self, roster: list[ParkSession]
    ) -> tuple[list[ParkSession], int]:
        """Mid-turn background sessions to signal, plus the count of mid-turn
        interactive ones skipped (a human's hands: they can't be told to
        freeze and their fixation can't be confirmed from the roster).

        Sessions running against per-terminal ``cswap run`` profiles are
        excluded by pid: the global swap doesn't touch their credentials, so
        freezing them (the orchestrator included) would idle the one part of
        the park the swap can't hurt. When the profile-membership probe is
        unavailable the exclusion degrades to "signal them too" — a wasted
        pause, never a missed one.
        """
        busy = [s for s in roster if s.executing]
        interactive = sum(1 for s in busy if s.kind != "background")
        background = [s for s in busy if s.kind == "background"]
        excluded = self._drain2_excluded_pids(
            [s.pid for s in background if s.pid is not None]
        )
        targets = [
            s for s in background if s.pid is None or s.pid not in excluded
        ]
        return targets, interactive

    def _drain2_excluded_pids(self, pids: list[int]) -> set[int]:
        excluded: set[int] = set()
        if not pids:
            return excluded
        try:
            numbers = self.switcher.switchable_account_numbers()
        except Exception:
            return excluded
        for num in numbers:
            email = self.switcher.account_email(num)
            if not email:
                continue
            try:
                profile = session_dir_for(self.switcher.backup_dir, num, email)
                if not profile.exists():
                    continue
                owned = pids_with_config_dir(pids, profile)
            except Exception:
                continue
            if owned:
                excluded |= owned
        return excluded

    def _drain2_split_small(
        self, sessions: list[ParkSession]
    ) -> tuple[context_cost.MoveCost, list[str]]:
        """Price the sessions and name the ones too small to checkpoint.

        Returns ``(cost, small)``: the transcript-based migration estimate
        for every session (CON-582 telemetry, taken BEFORE any wave), and
        the sorted names whose known context sits at/below
        ``drain2SmallContextTokens`` — their post-swap cache re-create is
        pocket change next to the checkpoint ceremony, so they ride through
        the swap unfrozen. Unknown is not small: a transcript the engine
        can't read could be a 900k context, so it gets checkpointed.
        """
        cost = self._move_cost([(s.name, s.session_id) for s in sessions])
        small_max = self.settings.drain2_small_context_tokens
        small = sorted(
            s.name
            for s in sessions
            if small_max > 0
            and (tokens := cost.per_session.get(s.name)) is not None
            and tokens <= small_max
        )
        return cost, small

    @staticmethod
    def _drain2_cost_fields(record: dict) -> dict:
        """The additive telemetry a drain2 record carries into its
        SwitchEvent payload: the pre-wave migration estimate and how many
        sessions rode through unfrozen. Keys appear only when known, so
        pre-CON-582 records (and unpriceable parks) keep the old shape."""
        fields: dict = {}
        est = record.get("estMoveTokens")
        if isinstance(est, (int, float)) and not isinstance(est, bool):
            fields["estMoveTokens"] = int(est)
        small = record.get("small")
        if isinstance(small, list) and small:
            fields["skippedSmall"] = len(small)
        return fields

    def _drain2_gate(
        self, trigger: str, *, early: bool = False
    ) -> tuple[str, dict | None]:
        """Active-checkpoint gate for the forced proactive switch.

        Returns ``(mode, drain2_info)``: ``("hold", None)`` — episode in
        progress, a ``drain2-wait`` line was emitted; ``("proceed", info)``
        — swap now, ``info`` is the additive SwitchEvent payload with the
        honest waited/fixed/forced count; ``("fallback", None)`` — the park
        channel failed (event emitted, channel backed off in-process), the
        caller runs the passive v1 drain instead.

        The episode lives in the state file under ``drain2`` (in memory for
        dry-run) so cron ``--once`` ticks and daemon restarts share it.
        Fixation is proof, not a timer and not one glance (CON-461): a
        signaled session is fixed when it left a checkpoint receipt for
        THIS episode (primary — the agent's own word, visible through the
        ``busy`` its background watch causes), or SOFTLY when the roster
        shows it not ``busy`` for DRAIN2_SOFT_FIX_POLLS consecutive polls
        (the fallback for sessions that never acked; a single poll is
        routinely a turn-boundary blip of a session that never paused).
        The record's ``acked`` list is who the post-swap resume wave may
        wake — sessions that provably parked; receiptless sessions are
        never re-woken.
        """
        now = self.clock()
        max_wait = self.settings.drain2_wait_seconds
        record = self._read_drain2()
        if record is not None and record.get("phase") == "swapped":
            # A swapped episode that still owes its resume survived this
            # tick's ``_drain2_finish`` (a herald retry is pending). A fresh
            # episode written over it would destroy the ``resumed``
            # bookkeeping and orphan the pending sessions (review r1) —
            # hold until the finish closes it (retry success or the
            # self-rescue cap). At-limit/failover still escape through the
            # passive v1 path, so a dying account never waits on this.
            self._emit(
                NoSwitchEvent(
                    reason="drain2-wait",
                    detail=(
                        "previous episode's resume still pending; a new "
                        "episode waits for it to close"
                    ),
                )
            )
            return "hold", None
        if record is not None:
            if record.get("phase") != "signaled":
                record = None  # another phase's leftovers are not ours
            else:
                started = record.get("startedAt")
                updated = record.get("updatedAt")
                if (
                    not isinstance(started, (int, float))
                    or not isinstance(updated, (int, float))
                    or now - updated > DRAIN_STALE_GAP_S
                ):
                    record = None  # a previous episode's leftovers

        roster = self._park_roster()
        if roster is None:
            self._drain2_go_unavailable(
                "park roster unreadable (`claude agents --json`)"
            )
            return "fallback", None
        by_name = {s.name: s for s in roster}

        if record is None:
            busy, skipped_interactive = self._drain2_targets(roster)
            if not busy:
                # Nobody is mid-turn: the pause already exists.
                return "proceed", {
                    "outcome": "ready",
                    "waitedSeconds": 0,
                    "fixed": 0,
                    "forced": 0,
                    "ackFixed": 0,
                    "softFixed": 0,
                }
            # Price the move BEFORE any wave (CON-582): the estimate covers
            # every mid-turn session — the small ones pay too, just without
            # the ceremony — so future thresholds are judged by the number.
            cost, small = self._drain2_split_small(busy)
            targets = [s for s in busy if s.name not in small]
            if not targets:
                # Everyone mid-turn is pocket change: swap without a pause
                # — freezing them would cost more than their re-creates.
                info = {
                    "outcome": "ready",
                    "waitedSeconds": 0,
                    "fixed": 0,
                    "forced": 0,
                    "ackFixed": 0,
                    "softFixed": 0,
                    "skippedSmall": len(small),
                }
                if cost.total is not None:
                    info["estMoveTokens"] = cost.total
                self._emit(
                    Drain2SignalEvent(
                        trigger=trigger,
                        targets=[],
                        delivered=[],
                        skipped_interactive=skipped_interactive,
                        dry_run=self.dry_run,
                        est_move_tokens=cost.total,
                        est_session_tokens=cost.per_session,
                        skipped_small=small,
                    )
                )
                return "proceed", info
            names = sorted(s.name for s in targets)
            if not self.dry_run:
                # Receipts from previous episodes must not pre-fix anyone.
                self._drain2_clear_acks(names)
            wave = self._drain2_wave(names, self._drain2_stop_message())
            if not wave.ok:
                self._drain2_go_unavailable(f"stop wave failed: {wave.detail}")
                return "fallback", None
            new_record = {
                "phase": "signaled",
                "trigger": trigger,
                "startedAt": now,
                "updatedAt": now,
                "signaled": {
                    s.name: {"sessionId": s.session_id} for s in targets
                },
            }
            if early:
                # Marks the episode as started by the small-park early
                # trigger, so `_early_swap_fires` keeps feeding it without
                # re-judging the park size every tick.
                new_record["early"] = True
            if small:
                new_record["small"] = small
            if cost.total is not None:
                new_record["estMoveTokens"] = cost.total
            self._write_drain2(new_record)
            self._emit(
                Drain2SignalEvent(
                    trigger=trigger,
                    targets=names,
                    delivered=wave.delivered,
                    skipped_interactive=skipped_interactive,
                    dry_run=self.dry_run,
                    est_move_tokens=cost.total,
                    est_session_tokens=cost.per_session,
                    skipped_small=small,
                )
            )
            self._emit(
                NoSwitchEvent(
                    reason="drain2-wait",
                    detail=(
                        f"signaled {len(names)} session(s); waiting for "
                        f"checkpoint 0s of {max_wait:.0f}s"
                    ),
                )
            )
            return "hold", None

        # Episode in progress: top up newcomers, then judge fixation.
        signaled = dict(record.get("signaled") or {})
        known_small = sorted(
            n for n in (record.get("small") or []) if isinstance(n, str)
        )
        targets, skipped_interactive = self._drain2_targets(roster)
        newcomers = [
            s
            for s in targets
            if s.name not in signaled and s.name not in known_small
        ]
        if newcomers:
            # Same law as the initial wave: price first, then leave the
            # pocket-change contexts running. A session judged small stays
            # small for the episode (recorded), so it is neither re-priced
            # nor re-announced every tick.
            cost, fresh_small = self._drain2_split_small(newcomers)
            fresh = sorted(
                s.name for s in newcomers if s.name not in fresh_small
            )
            if fresh_small:
                known_small = sorted({*known_small, *fresh_small})
                record = {**record, "small": known_small}
            if cost.total is not None:
                prev_est = record.get("estMoveTokens")
                prev_est = (
                    int(prev_est)
                    if isinstance(prev_est, (int, float))
                    and not isinstance(prev_est, bool)
                    else 0
                )
                record = {**record, "estMoveTokens": prev_est + cost.total}
            wave = None
            if fresh:
                if not self.dry_run:
                    self._drain2_clear_acks(fresh)
                wave = self._drain2_wave(fresh, self._drain2_stop_message())
                # Track them regardless of wave health: the count must cover
                # every session the swap can tear; an unsignaled newcomer is
                # simply forced at the cap, honestly counted.
                for s in newcomers:
                    if s.name in fresh:
                        signaled[s.name] = {"sessionId": s.session_id}
            self._emit(
                Drain2SignalEvent(
                    trigger=trigger,
                    targets=fresh,
                    # None = unconfirmed (the wave failed or its report was
                    # unparseable) — never a confirmed zero. No wave at all
                    # (every newcomer was small) is a confirmed zero.
                    delivered=(
                        (wave.delivered if wave.ok else None)
                        if wave is not None
                        else []
                    ),
                    skipped_interactive=skipped_interactive,
                    top_up=True,
                    dry_run=self.dry_run,
                    est_move_tokens=cost.total,
                    est_session_tokens=cost.per_session,
                    skipped_small=fresh_small,
                )
            )

        started = record["startedAt"]
        # Fixation proof (CON-461). The receipt is primary: the agent's own
        # word that it checkpointed, visible through any ``busy`` its
        # background watch causes (CON-451). The roster alone counts only
        # as a sustained not-busy STREAK — one poll is routinely the
        # sub-second gap between two tool calls of a session that never
        # paused, and the 14-08 episode swapped under fix-age-267's live
        # turn on exactly that misreading. A busy observation resets the
        # streak; the counters persist in the record so cron ``--once``
        # ticks share them.
        prev_streaks = record.get("notBusy")
        prev_streaks = prev_streaks if isinstance(prev_streaks, dict) else {}
        # Only an observation far enough from this episode's previous gate
        # poll counts toward the streak: rapid ticks (TUI wake, settings
        # edits slicing the sleep) would otherwise collect 3 "consecutive"
        # not-busy glances inside ONE stretched turn boundary (review r1
        # finding 2). Too-soon not-busy keeps the streak as is; busy
        # resets at any spacing — work is proof at any distance.
        spaced = now - record["updatedAt"] >= DRAIN2_SOFT_FIX_MIN_GAP_S
        streaks: dict[str, int] = {}
        acked: list[str] = []
        soft: list[str] = []
        unfixed: list[str] = []
        for name in sorted(signaled):
            if self._drain2_acked(name, since=started):
                acked.append(name)
                continue
            row = by_name.get(name)
            if row is not None and row.executing:
                streaks[name] = 0
                unfixed.append(name)
                continue
            prev = prev_streaks.get(name)
            prev = prev if isinstance(prev, int) and prev >= 0 else 0
            streak = prev + 1 if spaced else prev
            streaks[name] = streak
            if streak >= DRAIN2_SOFT_FIX_POLLS:
                if spaced and streak == DRAIN2_SOFT_FIX_POLLS:
                    _logger.info(
                        "drain2: %s fixed softly — no receipt, roster "
                        "not-busy %d polls in a row",
                        name,
                        streak,
                    )
                soft.append(name)
            else:
                unfixed.append(name)
        fixed = sorted(acked + soft)
        waited = now - record["startedAt"]
        if not unfixed:
            self._write_drain2({
                **record,
                "signaled": signaled,
                "updatedAt": now,
                "notBusy": streaks,
                "acked": acked,
                "soft": soft,
            })
            return "proceed", {
                "outcome": "ready",
                "waitedSeconds": int(waited),
                "fixed": len(fixed),
                "forced": 0,
                "ackFixed": len(acked),
                "softFixed": len(soft),
                **self._drain2_cost_fields(record),
            }
        if waited < max_wait:
            self._write_drain2({
                **record,
                "signaled": signaled,
                "updatedAt": now,
                "notBusy": streaks,
            })
            self._emit(
                NoSwitchEvent(
                    reason="drain2-wait",
                    detail=(
                        f"checkpointed {len(fixed)}/{len(signaled)} "
                        f"(receipt {len(acked)}, soft {len(soft)}); waited "
                        f"{waited:.0f}s of {max_wait:.0f}s"
                    ),
                )
            )
            return "hold", None
        # Fixation cap. Warn once per episode; like v1, the record survives
        # until a switch lands, so a blocked or failed attempt retries
        # without re-waiting and without repeating the WARN.
        if not record.get("timeoutWarned"):
            self._emit(
                Drain2TimeoutEvent(
                    trigger=trigger,
                    waited_seconds=int(waited),
                    max_wait_seconds=int(max_wait),
                    fixed=fixed,
                    forced=unfixed,
                    acked=acked,
                    soft=soft,
                )
            )
        self._write_drain2({
            **record,
            "signaled": signaled,
            "updatedAt": now,
            "notBusy": streaks,
            "acked": acked,
            "soft": soft,
            "timeoutWarned": True,
        })
        return "proceed", {
            "outcome": "timeout",
            "waitedSeconds": int(waited),
            "fixed": len(fixed),
            "forced": len(unfixed),
            "ackFixed": len(acked),
            "softFixed": len(soft),
            **self._drain2_cost_fields(record),
        }

    def _drain2_mark_swapped(self, number: str) -> None:
        """A switch just landed inside a drain-v2 episode: record the phase
        flip so the resume wave survives a crash between swap and resume.

        Only a live episode flips: a rotten signaled record (another
        episode's leftovers a concurrent ``--once`` may have raced past the
        tick-top reconcile) must not be adopted — its verify/resume would
        narrate sessions long gone. It is dropped instead.
        """
        record = self._read_drain2()
        if record is None or record.get("phase") != "signaled":
            return
        now = self.clock()
        updated = record.get("updatedAt")
        if (
            not isinstance(updated, (int, float))
            or now - updated > DRAIN_STALE_GAP_S
        ):
            self._write_drain2(None)
            return
        self._write_drain2({
            **record,
            "phase": "swapped",
            "swappedAt": now,
            "updatedAt": now,
            "to": str(number),
            "verifyAttempts": 0,
        })

    def _drain2_reconcile(self) -> None:
        """Close a signaled episode the gate can no longer own.

        Two machine-detectable ways an episode dies without its own swap:
        a switch of another trigger lands past it (an unsignaled interactive
        session rides the account into the at-limit escape), or the forcing
        condition goes away (a window reset drops utilization below the
        threshold) and the gate stops observing, so the record goes stale.
        In both, sessions frozen by the STOP wave would otherwise hang on
        the wave text's self-rescue prose alone — this is the machine
        guarantee behind that promise: the engine knows the episode is dead
        and wakes them itself. Runs each tick top, after ``_drain2_finish``.
        """
        record = self._read_drain2()
        if record is None or record.get("phase") != "signaled":
            return
        now = self.clock()
        state = self._read_state() if not self.dry_run else {}
        last_switch = state.get("lastSwitchAt")
        started = record.get("startedAt")
        updated = record.get("updatedAt")
        switched_past = (
            isinstance(last_switch, (int, float))
            and isinstance(started, (int, float))
            and last_switch > started
        )
        stale = (
            not isinstance(updated, (int, float))
            or now - updated > DRAIN_STALE_GAP_S
        )
        if not switched_past and not stale:
            return
        reason = (
            "abandoned: a switch landed past the episode"
            if switched_past
            else "abandoned: episode went stale (forcing condition gone)"
        )
        self._drain2_resume_wave(record, reason=reason)
        self._write_drain2(None)

    def _drain2_acked_set(self, record: dict) -> set[str]:
        """Names that provably parked for this episode: a live receipt
        (mtime after the episode start — also catches agents that acked
        after the swap landed), plus the ``acked`` list judged at proceed
        time (survives receipt files vanishing afterwards). A record
        written by a pre-CON-461 engine carries no ``acked`` key; its
        closest equivalent is the legacy ``frozen`` snapshot, honored so a
        deploy never strands an episode already in flight."""
        signaled = {
            n for n in (record.get("signaled") or {}) if isinstance(n, str)
        }
        started = record.get("startedAt")
        since = started if isinstance(started, (int, float)) else 0.0
        acked = {n for n in signaled if self._drain2_acked(n, since=since)}
        acked |= {
            n
            for n in (record.get("acked") or [])
            if isinstance(n, str) and n in signaled
        }
        if "acked" not in record:
            acked |= {
                n
                for n in (record.get("frozen") or [])
                if isinstance(n, str) and n in signaled
            }
        return acked

    def _drain2_resume_targets(
        self, record: dict
    ) -> tuple[list[str], list[str]] | None:
        """Live resume targets, and who stays out of the wave on purpose.

        Targets: sessions that provably parked — checkpoint receipt for
        this episode (``_drain2_acked_set``) — and still hold an open task
        (``state=working``), busy and idle alike. The first live episode
        (CON-451) buried the idle-only filter: a session frozen behind its
        own background task (the wave text's swap watch, a local test
        suite) reads ``busy`` in the roster forever, and a genuinely
        mid-turn session just queues the message until its next turn
        boundary. Sessions that finished (done/failed/stopped, or gone
        from the roster) are never re-woken: a message to a completed
        background session would respawn it.

        Signaled sessions WITHOUT a receipt are not woken (CON-461): they
        either never paused — the 14-08 episode told fix-age-267 «пауза
        кончилась» while it had worked through the whole episode — or
        parked without acking, and those self-wake via their marker watch
        or the wave text's self-rescue clause. The second list returned is
        exactly those left-out open-task sessions, for the event's
        honesty. Returns None when the roster is unreadable.
        """
        signaled = sorted(
            n for n in (record.get("signaled") or {}) if isinstance(n, str)
        )
        acked = self._drain2_acked_set(record)
        roster = self._park_roster()
        if roster is None:
            return None
        by_name = {s.name: s for s in roster}
        open_task = [
            n
            for n in signaled
            if (row := by_name.get(n)) is not None
            and row.state in (None, "working")
        ]
        return (
            [n for n in open_task if n in acked],
            [n for n in open_task if n not in acked],
        )

    def _drain2_resume_wave(
        self,
        record: dict,
        *,
        reason: str = "",
        message: str = DRAIN2_RESUME_MESSAGE,
    ) -> tuple[list[str], WaveResult | None]:
        """Send the resume wave for an episode and emit the event.

        Targets are derived live (``_drain2_resume_targets``), minus names a
        previous wave of this episode already confirmed (``resumed``). When
        the roster can't answer, every session that provably parked (the
        acked set — readable without a roster) is woken rather than none
        (a message to a busy session just queues; a frozen one left behind
        stays frozen). Returns ``(targets, wave)`` for the caller's retry
        bookkeeping; ``wave=None`` means nothing was sent (dry-run, or
        nobody left to wake).
        """
        already = {n for n in (record.get("resumed") or []) if isinstance(n, str)}
        live = self._drain2_resume_targets(record)
        if live is None:
            # Roster unreadable: who is still open-task can't be judged,
            # but "signaled and never acked" is state-file knowledge —
            # report it rather than a false "nobody left out" (review r1
            # nit).
            acked_set = self._drain2_acked_set(record)
            targets = sorted(acked_set)
            unacked = sorted(
                {
                    n
                    for n in (record.get("signaled") or {})
                    if isinstance(n, str)
                }
                - acked_set
            )
        else:
            targets, unacked = live
        targets = [n for n in targets if n not in already]
        if self.dry_run:
            self._emit(
                Drain2ResumeEvent(
                    targets=targets, delivered=[], skipped="dry-run",
                    reason=reason, unacked=unacked,
                )
            )
            return targets, None
        if not targets:
            self._emit(
                Drain2ResumeEvent(
                    targets=[], delivered=[],
                    skipped=(
                        "already resumed"
                        if already
                        else "nobody acked a checkpoint"
                    ),
                    reason=reason, unacked=unacked,
                )
            )
            return [], None
        wave = self._drain2_wave(targets, message)
        self._emit(
            Drain2ResumeEvent(
                targets=targets,
                delivered=wave.delivered if wave.ok else None,
                skipped="" if wave.ok else f"resume wave failed: {wave.detail}",
                reason=reason, unacked=unacked,
            )
        )
        return targets, wave

    def _drain2_release(self, drain2: dict | None, reason: str) -> None:
        """The gate said "swap now" but the swap cannot happen for a
        non-transient reason: release the park and close the episode
        instead of holding it open (live hole 14-08 17:10–17:13Z, CON-461
        add-on: the record stayed open and the park stood parked until a
        manual wake).

        Since CON-572 the no-candidate exits release through
        ``_abandon_switch_intent`` before any gate runs, so the one caller
        left is "every ranked target failed to freshen" — candidates keep
        qualifying but none can be activated. The wave says honestly that
        no swap happened (``DRAIN2_RELEASE_MESSAGE``), targets the
        sessions that provably parked (the same acked-only law as every
        resume), and the record is dropped regardless of wave health —
        parked agents keep their bounded self-rescue either way. A release
        backoff then keeps v2 from re-signaling a fresh pause while the
        same broken candidates keep qualifying. Transient freshen failures
        do NOT release: they retry next tick with the episode intact, so
        an orderly pause isn't wasted on a network blip.
        """
        if drain2 is None:
            return
        record = self._read_drain2()
        if record is None or record.get("phase") != "signaled":
            return
        self._drain2_resume_wave(
            record,
            reason=f"released without a swap: {reason}",
            message=DRAIN2_RELEASE_MESSAGE,
        )
        until = self.clock() + DRAIN2_BACKOFF_S
        if self.dry_run:
            self._drain2_mem = None
        else:
            # One atomic state write: close the episode and persist the
            # backoff together, so a crash between the two can't leave a
            # released park with no backoff (or vice versa).
            def close_and_backoff(state: dict) -> None:
                state.pop("drain2", None)
                state["drain2ReleaseUntil"] = until

            self._mutate_state(close_and_backoff)
        self._drain2_release_until = until

    def _abandon_switch_intent(
        self, trigger: str, reason: str, *, alert: bool = True
    ) -> None:
        """This tick had to switch and learned nobody can take the park:
        the switch intent is dead, and every drain artifact dies with it
        (CON-572 class B: the 15-08 stale v1 record — "already waited
        1648s" — outlived two drain2 releases and authorized a forced swap
        into a working park the moment ``cswap add`` produced a candidate).

        A signaled drain2 episode is released with the honest no-swap wave
        (same law as ``_drain2_release``); the passive v1 wait is dropped
        so a later candidate starts a FRESH episode instead of inheriting
        waited time from this dead one. No release backoff is armed: with
        candidates judged before any wave, a fresh pause cannot be
        signaled while there is still nobody to switch to, and the backoff
        would only stall the orderly episode a new candidate deserves.
        Only the KNOWN-dead intents call this — a transiently unreadable
        tick (no-comparison) holds every artifact instead, like the
        transient freshen failure does. ``alert=False`` skips the
        last-account cry: a below-threshold early tick (CON-582) that found
        nothing better is staying put by choice, not down to its last
        account.
        """
        record = self._read_drain2()
        if record is not None and record.get("phase") == "signaled":
            self._drain2_resume_wave(
                record,
                reason=f"released without a swap: {reason}",
                message=DRAIN2_RELEASE_MESSAGE,
            )
            self._write_drain2(None)
        if self._read_drain() is not None:
            self._write_drain(None)
        if alert:
            self._alert_last_account(trigger, reason)

    def _alert_last_account(self, trigger: str, reason: str) -> None:
        """Emit the "park is on its last working account" alert, once per
        drought (CON-572 class A). Dedup is two-layered: the in-process
        flag answers dry-run and saves the state read; the state-file
        stamp (``lastAccountAlertedAt``) shares the dedup with cron
        ``--once`` ticks and daemon restarts. ``_clear_last_account_alert``
        re-arms both the moment a qualifying candidate exists again."""
        if self._last_account_alerted:
            return
        if not self.dry_run:
            if self._read_state().get("lastAccountAlertedAt") is not None:
                self._last_account_alerted = True
                return

            def stamp(state: dict) -> None:
                state["lastAccountAlertedAt"] = self.clock()

            self._mutate_state(stamp)
        self._last_account_alerted = True
        self._emit(LastAccountAlertEvent(trigger=trigger, reason=reason))

    def _clear_last_account_alert(self) -> None:
        self._last_account_alerted = False
        if self.dry_run:
            return
        if self._read_state().get("lastAccountAlertedAt") is None:
            return

        def clear(state: dict) -> None:
            state.pop("lastAccountAlertedAt", None)

        self._mutate_state(clear)

    def _drain2_finish(self) -> None:
        """Complete a swapped drain-v2 episode: verify the new account
        answers, then wake the sessions that provably parked (checkpoint
        receipt) and still hold an open task.

        Runs right after the swap and again at the top of every tick, so a
        daemon restart or a cron ``--once`` handover finishes the resume
        instead of losing it. Bounded on every branch: the verify retries
        at most DRAIN2_VERIFY_ATTEMPTS times and then resumes anyway (a
        park frozen on a dead verify costs more than an optimistic resume),
        a failed wave or a herald-failed name keeps the episode open so the
        next tick re-waves exactly what's pending (the first live episode
        froze four sessions on a one-shot wave — CON-451), and an episode
        past the self-rescue window is closed without a wave — its sessions
        already resumed themselves.
        """
        record = self._read_drain2()
        if record is None or record.get("phase") != "swapped":
            return
        now = self.clock()
        swapped_at = record.get("swappedAt")
        if not isinstance(swapped_at, (int, float)) or (
            now - swapped_at > DRAIN2_SELF_RESCUE_S
        ):
            self._emit(
                Drain2ResumeEvent(
                    targets=[],
                    delivered=None,
                    skipped=(
                        "episode is past the self-rescue window; frozen "
                        "sessions resumed on their own"
                    ),
                )
            )
            self._write_drain2(None)
            return
        if not record.get("verified"):
            number = str(record.get("to") or "")
            attempt = int(record.get("verifyAttempts") or 0) + 1
            ok, detail = self._drain2_verify(number, swapped_at)
            self._emit(
                Drain2VerifyEvent(
                    number=number, ok=ok, attempt=attempt, detail=detail
                )
            )
            if not ok and attempt < DRAIN2_VERIFY_ATTEMPTS:
                self._write_drain2({
                    **record,
                    "verifyAttempts": attempt,
                    "updatedAt": now,
                })
                return
            # Wave retries below must re-wave, not re-verify: the account
            # was already judged once (answering, or resumed-regardless).
            record = {**record, "verified": True, "verifyAttempts": attempt}
        targets, wave = self._drain2_resume_wave(record)
        if wave is None:
            # Dry-run, or nobody left to wake — the episode is done.
            self._write_drain2(None)
            return
        if wave.ok:
            # Unconfirmed delivery (unparseable report) counts as sent:
            # the fixation loop's law — never respam on a fuzzy channel.
            confirmed = (
                set(wave.delivered) if wave.delivered is not None else set(targets)
            )
            confirmed -= set(wave.failed)
            resumed = sorted(
                {*(record.get("resumed") or []), *confirmed}
            )
            if not [n for n in targets if n not in resumed]:
                self._write_drain2(None)
                return
            self._write_drain2({**record, "resumed": resumed, "updatedAt": now})
            return
        # Channel failure: keep the episode, the next tick retries within
        # the self-rescue window.
        self._write_drain2({**record, "updatedAt": now})

    def _drain2_verify(
        self, number: str, swapped_at: float | None = None
    ) -> tuple[bool, str]:
        """The post-swap "new account answers" check. Never raises.

        "Answers" = the account's usage is provably readable. The collector
        serves within its TTL, so right after the swap this can be the very
        snapshot the target was chosen on (seconds old, fetched pre-swap) —
        that snapshot already proved the account alive, the token itself was
        refreshed by the pre-swap freshen, and ``switch_to`` confirmed the
        activation, so it counts; the detail says so honestly rather than
        claiming a live round-trip. The first post-swap poll is the network
        confirmation.
        """
        if not number:
            return False, "no target recorded"
        try:
            entries = self.switcher.usage_entries_by_account(fetch={number})
        except Exception as e:
            return False, f"usage fetch failed: {type(e).__name__}: {e}"
        entry = entries.get(number)
        value = entry.decision_value() if entry is not None else None
        if not isinstance(value, dict):
            return False, "usage not readable"
        fetched_at = entry.fetched_at if entry is not None else None
        if (
            isinstance(swapped_at, (int, float))
            and isinstance(fetched_at, (int, float))
            and fetched_at < swapped_at
        ):
            return True, (
                "usage readable (pre-swap snapshot; next poll confirms live)"
            )
        return True, ""

    def _session_quiet(self) -> tuple[bool, str]:
        """Whether session traffic has been silent for ``QUIET_WINDOW_S``.

        Returns ``(quiet, detail)``. No transcripts at all is quiet (nothing
        to burn). A transcript mtime *ahead* of the clock (clock skew, or a
        write racing the scan) counts as activity — the conservative side.
        """
        latest = latest_session_activity_ts(self.claude_projects_dir)
        if latest is None:
            return True, "no session transcripts"
        age = self.clock() - latest
        if age >= QUIET_WINDOW_S:
            return True, f"last session write {age:.0f}s ago"
        return False, (
            f"last session write {age:.0f}s ago; a voluntary switch waits "
            f"for {QUIET_WINDOW_S / 60:.0f}m of transcript silence"
        )

    def _check_model_names(
        self, quarantined: set[str], usage: dict[str, dict | str | None]
    ) -> None:
        """One-shot ``autoswitch.model`` typo guard.

        A configured name that no account reports means the filter looks
        active while gating nothing. That's only provable once every
        relevant oauth account has readable usage this tick — adaptive
        polling legitimately leaves gaps before that — and never worth a
        forced refresh of its own.
        """
        wanted = {m.lower(): m for m in self._models if m.lower() != "all"}
        if not wanted:
            self._model_check_done = True  # bare "all" needs no name match
            return
        relevant = [
            n
            for n in self.switcher.switchable_account_numbers()
            if n not in quarantined
            and self.switcher.account_kind_for(n) != "api_key"
        ]
        values = [usage.get(n) for n in relevant]
        readable = [v for v in values if isinstance(v, dict)]
        if not readable or len(readable) != len(values):
            return  # not every account observed yet — re-check next tick
        seen = {
            s["name"].lower()
            for v in readable
            for s in (v.get("scoped") or [])
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        }
        self._model_check_done = True
        missing = [name for low, name in wanted.items() if low not in seen]
        if missing:
            self._emit(
                ConfigWarningEvent(
                    message=(
                        f"autoswitch.model: {', '.join(missing)} matches no "
                        "account's usage windows — a configured window nobody "
                        "reports makes every account's headroom unknown, so "
                        "auto-switching is blocked until the name is fixed "
                        "(typo?)"
                    )
                )
            )

    def _earliest_recovery(
        self, usage: dict[str, dict | str | None]
    ) -> datetime | None:
        """Earliest moment any account becomes usable again (UTC), or None
        when that moment can't be proven.

        Per account that's the *latest* reset among its ≥100% relevant
        windows — an account blocked on both 5h and a scoped weekly limit
        isn't usable when the 5h rolls over — then the minimum across
        accounts, the active one included (its recovery also ends the
        blocked state). A blocked account whose exhausted windows carry no
        reset time at all could recover at any moment, so it makes the whole
        answer unprovable: return None and let the bounded blocked-cadence
        fallback re-check, rather than sleeping toward another account's
        later known reset."""
        earliest: float | None = None
        now = self.clock()
        for value in usage.values():
            if not isinstance(value, dict):
                continue
            blocked = [
                resets_at
                for _, pct, resets_at in oauth.relevant_windows(value, self._models)
                if pct >= 100.0
            ]
            if not blocked:
                continue  # not exhausted — doesn't gate the blocked state
            usable_at = _limiting_reset_ts(value, self._models)
            if usable_at is None or usable_at <= now:
                return None  # blocked with unprovable recovery — don't oversleep
            if earliest is None or usable_at < earliest:
                earliest = usable_at
        if earliest is None:
            return None
        return datetime.fromtimestamp(earliest, tz=timezone.utc)

    def _adopt_snapshot_event(
        self,
        prior_ref: dict | None,
        to_ref: dict,
        account_num: str,
        email: str | None,
    ) -> AdoptSnapshotEvent:
        """Run the forensic collector for an adoption (CON-2323). A collector
        that raises becomes an event with ``error`` — the tick goes on."""
        try:
            session_dir = (
                self.switcher._session_dir(account_num, email) if email else None
            )
            snapshot = self._adopt_snapshot(
                config_path=self.switcher._get_claude_config_path(),
                session_dir=session_dir,
                prior=prior_ref,
                to_ref=to_ref,
            )
            return AdoptSnapshotEvent(prior=prior_ref, to_ref=to_ref, snapshot=snapshot)
        except Exception as e:  # noqa: BLE001 — diagnostics never take the tick down
            return AdoptSnapshotEvent(
                prior=prior_ref,
                to_ref=to_ref,
                snapshot=None,
                error=f"{type(e).__name__}: {e}",
            )

    def _emit(self, event: AutoSwitchEvent) -> None:
        self.on_event(event)

    # -- loop -------------------------------------------------------------------

    def stop(self) -> None:
        """Ask ``run_loop`` to exit; wakes it from any sleep. Safe to call
        before the loop starts — the stop is never cleared, so the loop
        exits immediately (engines are single-use)."""
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Cut the current inter-tick sleep short and tick now."""
        self._wake.set()

    def apply_threshold(self, threshold: float) -> None:
        """Session override from the TUI: retarget the trigger and poll
        cadence mid-run. Pinned like a CLI flag, so the per-tick settings
        reload cannot quietly take it back. The frozen-settings swap is atomic
        and each tick snapshots ``self.settings`` once, so no locking."""
        self._settings_pins["threshold"] = threshold
        self.settings = replace(self.settings, threshold=threshold)
        self.switcher.set_poll_policy_inputs(threshold, self._models)

    def _next_delay(self, outcome: TickOutcome) -> float:
        interval = self.settings.interval_seconds
        if outcome is TickOutcome.BLOCKED:
            if self._sleep_until_ts is not None:
                delay = self._sleep_until_ts - self.clock()
                return min(max(delay, interval), MAX_SLEEP_S)
            if self._blocked_wait_long:
                # Truly exhausted with no reset time known / no candidates.
                return max(interval, NO_RESET_FALLBACK_S)
            # Blocked on something that can resolve any tick (hysteresis,
            # unreadable usage) — keep the normal cadence so the at-limit
            # escape isn't missed.
        elif outcome is TickOutcome.NO_ACTION and self._idle_hold_slow:
            # Idle-hold: Claude is idle on an expired token — nothing changes
            # until the user comes back, so crawl. Worst case protection
            # resumes one slow tick after they do.
            return max(interval, NO_RESET_FALLBACK_S)
        # ±10% jitter so multiple machines don't synchronize their API hits.
        return interval * (0.9 + 0.2 * random.random())

    def _wait_between_ticks(self, delay: float) -> None:
        """Sleep until the next tick — cut short by stop()/wake(), and by an
        edit to settings.json so a config change never waits out a long
        blocked/exhausted sleep. Sliced by ``SETTINGS_WATCH_S``: one stat per
        slice, and the nominal slice budget bounds the loop even when the
        wait itself returns early."""
        stamp = self._settings_stamp()
        remaining = delay
        while remaining > 0:
            chunk = min(remaining, SETTINGS_WATCH_S)
            if self._wake.wait(chunk):
                return
            remaining -= chunk
            if self._settings_stamp() != stamp:
                return

    def run_loop(self) -> int:
        """Tick forever (until :meth:`stop`); a failing tick never kills it."""
        while True:
            # Clear at the top, not after the wait: a wake() racing a wait
            # timeout is then never lost — the tick right after this clear
            # already sees whatever settings that wake announced.
            self._wake.clear()
            if self._stop.is_set():
                return 0
            try:
                outcome = self.tick()
            except Exception as e:  # pragma: no cover - tick() already guards
                self._emit(
                    ErrorEvent(message=f"{type(e).__name__}: {e}", transient=True)
                )
                outcome = TickOutcome.ERROR
            delay = self._next_delay(outcome)
            if delay > self.settings.interval_seconds * 1.5:
                until = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self._emit(
                    SleepEvent(
                        seconds=delay,
                        until=until.isoformat(timespec="seconds").replace(
                            "+00:00", "Z"
                        ),
                    )
                )
            self._wait_between_ticks(delay)
