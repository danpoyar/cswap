"""Park channel for drain v2: roster reads and herald waves.

The autoswitch engine talks to the *park* — the live Claude Code sessions on
this machine — through two documented surfaces:

- ``claude agents --json``: the scripting roster (interactive and background
  sessions). ``status`` is the process-level truth ("busy" = a turn is
  executing right now, "idle" = at a turn boundary); ``state`` is the
  background task state (working/blocked/done/failed/stopped). A parked
  session with an open task shows ``state=working, status=idle``.
- A one-shot headless ``claude -p`` session (the *herald*) that delivers a
  checkpoint/resume message to named sessions via its SendMessage tool.
  The daemon cannot post to inbox sockets itself: a raw-socket message
  asserts no permission class, and bypass-permissions receivers hold such
  messages for approval that a terminal-less background session can never
  give (docs: cross-session messaging → inbound controls). A real Claude
  session inheriting the pinned permission mode is deliverable, so the
  herald is the channel.

Everything here is a thin, injectable boundary: the engine depends on
``roster()`` and ``send_wave()`` only, and tests replace the whole object.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger("claude-swap")

ROSTER_TIMEOUT_S = 20.0
# One wave = one headless model session doing N SendMessage calls; startup
# plus a few tool rounds. Killed past this — the engine then treats the wave
# as failed and falls back (STOP) or relies on self-rescue (RESUME).
HERALD_TIMEOUT_S = 120.0
# The relay is a mechanical one-step job (no decisions, no code): the small
# model tier is the right spend, and an alias stays current across releases.
HERALD_MODEL = "sonnet"

# Where `claude` lives when the daemon's launchd PATH doesn't carry it.
_CLAUDE_FALLBACKS = (
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

# Auth/env vars that must not leak into herald/roster subprocesses: a stray
# ANTHROPIC_API_KEY would re-bill the wave, a parent session's CLAUDE_CODE_*
# vars would tangle the herald with that session, and CLAUDE_CONFIG_DIR would
# aim it at a per-terminal profile instead of the global login being swapped.
_ENV_DROP_EXACT = ("CLAUDE_CONFIG_DIR", "CLAUDECODE", "ANTHROPIC_API_KEY")
_ENV_DROP_PREFIX = "CLAUDE_CODE_"


@dataclass(frozen=True)
class ParkSession:
    """One roster row, reduced to what the drain needs."""

    name: str
    session_id: str
    kind: str  # "interactive" | "background"
    status: str | None  # "busy" | "idle"; None when the process is gone
    state: str | None  # background only: working|blocked|done|failed|stopped
    pid: int | None

    @property
    def executing(self) -> bool:
        """A turn is running right now (API calls in flight or imminent)."""
        return self.status == "busy"


@dataclass(frozen=True)
class WaveResult:
    """Outcome of one herald wave.

    ``ok=False`` means the channel itself failed (no binary, spawn error,
    timeout, error envelope) — the caller treats the channel as unavailable.
    ``ok=True`` with ``delivered=None`` means the wave ran but the per-name
    report could not be parsed: delivery is unconfirmed, not absent.
    """

    ok: bool
    delivered: list[str] | None = None
    failed: dict[str, str] = field(default_factory=dict)
    detail: str = ""


_HERALD_PROMPT = (
    "You are a message relay. Deliver one message to each target session "
    "listed below using the SendMessage tool, then report.\n"
    "\n"
    "Message text — send EXACTLY this text to every target, unchanged:\n"
    "<<<MESSAGE\n"
    "{message}\n"
    "MESSAGE>>>\n"
    "\n"
    "Targets — one SendMessage call per name, the name is the 'to' value:\n"
    "{targets}\n"
    "\n"
    "Rules: if a send fails with an error suggesting a 'name [ref]' form, "
    "retry that one target once with the suggested form; never message any "
    "session not listed; use no other tools. When every target has been "
    "attempted, output ONLY this JSON object and nothing else: "
    '{{"sent": ["<name>", ...], "failed": {{"<name>": "<short reason>", ...}}}}'
)


class ParkChannel:
    """Subprocess-backed park access; both calls degrade to explicit failure."""

    def __init__(
        self,
        claude_bin: str | None = None,
        herald_cwd: Path | None = None,
        run=subprocess.run,
    ):
        self._claude_bin = claude_bin
        self._herald_cwd = herald_cwd
        self._run = run
        self._resolve_done = False
        self._resolved: str | None = None

    # -- binary ----------------------------------------------------------

    def binary(self) -> str | None:
        """Absolute path of the ``claude`` CLI, or None when unavailable."""
        if self._resolve_done:
            return self._resolved
        path = self._claude_bin or shutil.which("claude")
        if path is None:
            for candidate in _CLAUDE_FALLBACKS:
                expanded = os.path.expanduser(candidate)
                if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                    path = expanded
                    break
        self._resolve_done = True
        self._resolved = path
        return path

    def _child_env(self) -> dict[str, str]:
        return {
            k: v
            for k, v in os.environ.items()
            if k not in _ENV_DROP_EXACT and not k.startswith(_ENV_DROP_PREFIX)
        }

    def _cwd(self) -> str | None:
        if self._herald_cwd is None:
            return None
        try:
            self._herald_cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        return str(self._herald_cwd)

    # -- roster ----------------------------------------------------------

    def roster(self) -> list[ParkSession] | None:
        """Current sessions, or None when the roster can't be read."""
        binary = self.binary()
        if binary is None:
            return None
        try:
            proc = self._run(
                [binary, "agents", "--json"],
                capture_output=True,
                text=True,
                timeout=ROSTER_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                env=self._child_env(),
                cwd=self._cwd(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            _logger.debug("park roster failed: %r", e)
            return None
        if proc.returncode != 0:
            _logger.debug("park roster rc=%s: %s", proc.returncode, proc.stderr)
            return None
        try:
            rows = json.loads(proc.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(rows, list):
            return None
        sessions: list[ParkSession] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            session_id = row.get("sessionId")
            if not isinstance(name, str) or not name:
                continue  # unaddressable — SendMessage routes by name
            pid = row.get("pid")
            sessions.append(
                ParkSession(
                    name=name,
                    session_id=session_id if isinstance(session_id, str) else "",
                    kind=str(row.get("kind") or "background"),
                    status=(
                        row["status"] if isinstance(row.get("status"), str) else None
                    ),
                    state=(
                        row["state"] if isinstance(row.get("state"), str) else None
                    ),
                    pid=pid if isinstance(pid, int) else None,
                )
            )
        return sessions

    # -- herald ----------------------------------------------------------

    def send_wave(self, targets: list[str], message: str) -> WaveResult:
        """Deliver ``message`` to each target session name via one herald."""
        if not targets:
            return WaveResult(ok=True, delivered=[], detail="no targets")
        binary = self.binary()
        if binary is None:
            return WaveResult(ok=False, detail="claude CLI not found")
        prompt = _HERALD_PROMPT.format(
            message=message,
            targets="\n".join(f"- {name}" for name in targets),
        )
        try:
            proc = self._run(
                [
                    binary,
                    "-p",
                    prompt,
                    "--allowedTools",
                    "SendMessage",
                    "--output-format",
                    "json",
                    "--model",
                    HERALD_MODEL,
                ],
                capture_output=True,
                text=True,
                timeout=HERALD_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
                env=self._child_env(),
                cwd=self._cwd(),
            )
        except subprocess.TimeoutExpired:
            return WaveResult(ok=False, detail=f"herald timeout {HERALD_TIMEOUT_S:.0f}s")
        except (OSError, subprocess.SubprocessError) as e:
            return WaveResult(ok=False, detail=f"herald spawn failed: {e!r}")
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-200:]
            return WaveResult(
                ok=False, detail=f"herald rc={proc.returncode}: {tail}"
            )
        return _parse_wave_output(proc.stdout)


def _parse_wave_output(stdout: str) -> WaveResult:
    """Parse the herald's ``--output-format json`` envelope and its report.

    A malformed report degrades to "delivery unconfirmed" (``delivered=None``)
    rather than a channel failure: the sends most likely went out, and the
    engine's fixation loop judges the park by roster anyway.
    """
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return WaveResult(ok=False, detail="herald output is not JSON")
    if not isinstance(envelope, dict):
        return WaveResult(ok=False, detail="herald output is not an object")
    if envelope.get("is_error"):
        return WaveResult(
            ok=False, detail=f"herald errored: {str(envelope.get('result'))[:200]}"
        )
    result = envelope.get("result")
    if not isinstance(result, str):
        return WaveResult(ok=True, delivered=None, detail="no result text")
    start, end = result.find("{"), result.rfind("}")
    if start < 0 or end <= start:
        return WaveResult(ok=True, delivered=None, detail="no report object")
    try:
        report = json.loads(result[start : end + 1])
    except json.JSONDecodeError:
        return WaveResult(ok=True, delivered=None, detail="unparseable report")
    if not isinstance(report, dict):
        return WaveResult(ok=True, delivered=None, detail="report is not an object")
    sent = report.get("sent")
    failed = report.get("failed")
    return WaveResult(
        ok=True,
        delivered=(
            [s for s in sent if isinstance(s, str)]
            if isinstance(sent, list)
            else None
        ),
        failed=(
            {k: str(v) for k, v in failed.items()}
            if isinstance(failed, dict)
            else {}
        ),
        detail="",
    )
