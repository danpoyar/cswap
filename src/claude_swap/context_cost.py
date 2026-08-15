"""Estimate what moving the park costs, from session transcripts (CON-582).

Prompt caches are per-organization: after an account swap every live session
re-creates its whole context at full ``cache_creation`` price on its next
call. The best available estimate of that price is the session's own
transcript — the last assistant record's usage (``input_tokens +
cache_creation_input_tokens + cache_read_input_tokens``) is exactly the
prompt footprint the next turn would re-create. Summed over the sessions a
drain episode is about to judge, that is the migration price of the swap,
known BEFORE any wave goes out.

An estimate, deliberately: subagents spawned by a session carry their own
contexts in separate transcripts the roster doesn't name, so the real burn
runs higher than this sum (the 15-08 measure: 12 sessions plus their
subagents totalled 22 transcripts). Good enough to judge thresholds by;
never a billing number. Everything here degrades to "unknown" (None) —
an unreadable transcript must never break the tick that asked.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# How much of a transcript tail to scan for the last usage record. Transcript
# lines carry whole tool results, so single lines run to hundreds of KB; a
# few MB of tail always covers the last assistant record of a live session.
TAIL_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class MoveCost:
    """Per-session context sizes; ``None`` = transcript unreadable."""

    per_session: dict[str, int | None]

    @property
    def total(self) -> int | None:
        """Sum of the known sizes, or None when nothing was readable.

        None and 0 must stay distinct: a park whose transcripts all failed
        to read has an UNKNOWN price, not a free move.
        """
        known = [v for v in self.per_session.values() if v is not None]
        return sum(known) if known else None

    @property
    def unknown(self) -> list[str]:
        """Names whose context size could not be read, sorted."""
        return sorted(n for n, v in self.per_session.items() if v is None)


def _usage_tokens(record: dict) -> int | None:
    """Context size from one transcript record, or None.

    Sidechain records are a subagent's turns embedded in the parent file —
    their usage describes the subagent's own (typically tiny) context, and
    taking one as the session's footprint would understate the move.
    """
    if record.get("isSidechain") is True:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        seen = True
        total += int(value)
    return total if seen else None


def last_context_tokens(path: Path, tail_bytes: int = TAIL_BYTES) -> int | None:
    """Context size of a session by its transcript's last usage, or None.

    Reads only the file tail: transcripts grow to hundreds of MB and the
    answer lives on the last assistant record. The seek can land mid-line;
    that fragment fails JSON parsing and is skipped like any other
    unparseable line.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            tail = fh.read(tail_bytes)
    except OSError:
        return None
    for raw in reversed(tail.split(b"\n")):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        tokens = _usage_tokens(record)
        if tokens is not None:
            return tokens
    return None


def _transcripts_by_session_id(
    projects_dir: Path, session_ids: set[str]
) -> dict[str, Path]:
    """``<session_id>.jsonl`` files under ``projects_dir``, matched by name
    at any depth (main sessions sit one level down, subagent transcripts
    nest deeper). ``os.walk`` swallows unreadable directories — degrade
    toward "unknown", never raise."""
    wanted = {f"{sid}.jsonl": sid for sid in session_ids if sid}
    found: dict[str, Path] = {}
    if not wanted:
        return found
    for dirpath, _dirnames, filenames in os.walk(projects_dir):
        for name in filenames:
            sid = wanted.get(name)
            if sid is not None and sid not in found:
                found[sid] = Path(dirpath) / name
        if len(found) == len(wanted):
            break
    return found


def estimate_move_cost(
    projects_dir: Path, sessions: Iterable[tuple[str, str]]
) -> MoveCost:
    """Estimate the migration price of ``sessions`` (name, session_id)."""
    pairs = list(sessions)
    transcripts = _transcripts_by_session_id(
        projects_dir, {sid for _, sid in pairs}
    )
    per_session: dict[str, int | None] = {}
    for name, sid in pairs:
        path = transcripts.get(sid)
        per_session[name] = (
            last_context_tokens(path) if path is not None else None
        )
    return MoveCost(per_session=per_session)
