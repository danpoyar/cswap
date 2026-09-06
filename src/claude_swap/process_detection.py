"""Detect running Claude Code instances.

Reads session PID files (~/.claude/sessions/{pid}.json) and IDE lockfiles
(~/.claude/ide/{port}.lock) to determine which Claude Code instances are
currently running. Uses the same mechanism Claude Code itself uses internally.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from claude_swap.paths import get_claude_config_home

logger = logging.getLogger(__name__)


@dataclass
class ClaudeSession:
    """A running Claude Code session from ~/.claude/sessions/{pid}.json."""

    pid: int
    session_id: str
    cwd: str
    started_at: int  # epoch milliseconds
    kind: str  # "interactive", "bg", "daemon", "daemon-worker"
    entrypoint: str  # "cli", "claude-vscode", "claude-desktop", "sdk-cli", "mcp"
    status: str | None = None  # "busy", "idle", "waiting"


@dataclass
class IdeInstance:
    """A running IDE instance from ~/.claude/ide/{port}.lock."""

    port: int  # from filename
    pid: int
    ide_name: str  # "Visual Studio Code", "Cursor", "Windsurf"
    workspace_folders: list[str] = field(default_factory=list)


def get_claude_dir() -> Path:
    """Return the Claude config directory, respecting CLAUDE_CONFIG_DIR."""
    return get_claude_config_home()


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Cross-platform:
    - macOS/Linux/WSL: os.kill(pid, 0)
    - Windows: ctypes OpenProcess
    """
    if pid <= 1:
        return False

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM means the process exists but we lack permission
        return True
    except OSError:
        return False


def _is_pid_alive_windows(pid: int) -> bool:
    """Windows-specific PID liveness check using ctypes."""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def list_sessions(claude_dir: Path | None = None) -> list[ClaudeSession]:
    """Read session PID files and return only those with alive processes."""
    sessions_dir = (claude_dir or get_claude_dir()) / "sessions"
    if not sessions_dir.is_dir():
        return []

    sessions = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data["pid"]
            if not is_pid_alive(pid):
                continue
            sessions.append(ClaudeSession(
                pid=pid,
                session_id=data.get("sessionId", ""),
                cwd=data.get("cwd", ""),
                started_at=data.get("startedAt", 0),
                kind=data.get("kind", ""),
                entrypoint=data.get("entrypoint", ""),
                status=data.get("status"),
            ))
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.debug("Skipping session file %s: %s", path, exc)
    return sessions


def list_ide_instances(claude_dir: Path | None = None) -> list[IdeInstance]:
    """Read IDE lockfiles and return only those with alive processes."""
    ide_dir = (claude_dir or get_claude_dir()) / "ide"
    if not ide_dir.is_dir():
        return []

    instances = []
    for path in ide_dir.glob("*.lock"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid is None or not is_pid_alive(pid):
                continue
            port = int(path.stem)
            instances.append(IdeInstance(
                port=port,
                pid=pid,
                ide_name=data.get("ideName", "Unknown IDE"),
                workspace_folders=data.get("workspaceFolders", []),
            ))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as exc:
            logger.debug("Skipping IDE lockfile %s: %s", path, exc)
    return instances


def get_running_instances(
    claude_dir: Path | None = None,
) -> tuple[list[ClaudeSession], list[IdeInstance]]:
    """Return all running Claude Code sessions and IDE instances."""
    resolved = claude_dir or get_claude_dir()
    return list_sessions(resolved), list_ide_instances(resolved)


# Splits a `ps -wwE` command+environment blob at " VAR=" boundaries. The env
# section follows the argv; a value containing " word=" text is truncated at
# that word, which can only under-match (see pids_with_config_dir on why the
# caller treats a non-member conservatively).
_ENV_TOKEN_BOUNDARY = re.compile(r" (?=[A-Za-z_][A-Za-z0-9_]*=)")

_PS_TIMEOUT = 10.0


def pids_with_config_dir(
    pids: Iterable[int], config_dir: Path
) -> set[int] | None:
    """Which of ``pids`` run with ``CLAUDE_CONFIG_DIR`` set to ``config_dir``.

    With the session registry shared across profiles (CON-340), the registry
    no longer says which profile a session runs against — but the process's
    own environment does, and ``CLAUDE_CONFIG_DIR`` is the documented
    contract that *defines* "runs against this profile". Environment access
    is same-user only, which covers every session cswap manages.

    Returns the member subset, or ``None`` when the environment can't be
    inspected at all (probe missing/failed) so the caller picks the safe
    direction. A single PID whose environment is gone or unreadable is
    treated as not a member: it either exited or isn't ours.

    Paths are compared literally and resolved, so a profile reached through
    a symlinked spelling still matches. Linux reads ``/proc``; macOS/BSD one
    batched ``ps -wwE`` (verified live on macOS: same-user processes expose
    their environment; a value containing both a space and a ``word=`` token
    is truncated by the parser and would under-match — default profile
    locations never contain spaces).
    """
    unique = sorted({int(p) for p in pids if int(p) > 0})
    if not unique:
        return set()
    if sys.platform == "win32":
        # No shared registry on Windows (profiles keep private registries),
        # so membership never needs the environment there.
        return None

    matches = config_dir_matcher(config_dir)
    if os.path.isdir("/proc/self"):
        return _pids_with_config_dir_proc(unique, matches)
    return _pids_with_config_dir_ps(unique, matches)


def config_dir_matcher(config_dir: Path) -> Callable[[str], bool]:
    """``value == config_dir``, literally or resolved: a profile reached
    through a symlinked spelling still matches. Shared by the membership
    probe above and the adopt-time snapshot (adopt_snapshot.py)."""
    wanted_raw = str(config_dir)
    try:
        wanted_resolved = str(config_dir.resolve())
    except OSError:
        wanted_resolved = wanted_raw

    def matches(value: str) -> bool:
        if value in (wanted_raw, wanted_resolved):
            return True
        try:
            return str(Path(value).resolve()) == wanted_resolved
        except OSError:
            return False

    return matches


def _pids_with_config_dir_proc(
    pids: list[int], matches: Callable[[str], bool]
) -> set[int]:
    """Linux: read each candidate's ``/proc/<pid>/environ`` (NUL-separated)."""
    owned: set[int] = set()
    for pid in pids:
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue  # exited, or not our process — not a member
        for chunk in environ.split(b"\0"):
            if chunk.startswith(b"CLAUDE_CONFIG_DIR="):
                value = chunk.split(b"=", 1)[1].decode("utf-8", "replace")
                if matches(value):
                    owned.add(pid)
                break
    return owned


def _pids_with_config_dir_ps(
    pids: list[int], matches: Callable[[str], bool]
) -> set[int] | None:
    """macOS/BSD: one batched ``ps -wwE`` pass over all candidate PIDs.

    Decoded as UTF-8 with ``errors="replace"``: the output carries every
    candidate's raw argv+env, and a foreign process's argv can hold bytes
    that aren't valid UTF-8 (the kernel truncates a long argv mid-character,
    CON-465). Locale-independent and never raises; like the ``/proc`` path,
    a mangled value can only under-match.
    """
    cmd = [
        "ps",
        "-wwE",
        "-o",
        "pid=,command=",
        "-p",
        ",".join(map(str, pids)),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # probe unavailable — let the caller stay conservative
    # ps exits non-zero when some requested PIDs are already gone; the ones
    # it did find are still on stdout, so judge by output, not return code.
    if not result.stdout.strip():
        return set() if result.returncode != 0 else None
    owned: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        for token in _ENV_TOKEN_BOUNDARY.split(parts[1]):
            if token.startswith("CLAUDE_CONFIG_DIR="):
                if matches(token.split("=", 1)[1]):
                    owned.add(pid)
                break
    return owned
