"""Forensic snapshot of the moment a foreign login was adopted (CON-2323).

``adopt-real-login`` fires when ``~/.claude.json``'s ``oauthAccount`` names a
managed slot other than the recorded active one — by design a manual
``/login``. On 2026-09-05 it fired three times in 28 minutes for the same
slot with no ``/login``, no switch and no hand, and every trace was gone
before anyone looked: the writer had already exited, the sensor that
returned the login home had rewritten the file, the Keychain item carried
only its latest mdat. This module captures that moment for the daemon —
the process table of every Claude Code binary with its ``CLAUDE_CONFIG_DIR``
and ``HOME``, the config file's mtime and identity, the Keychain items'
modification dates, whoever holds the file open — so the next episode names
its writer.

Rules: never raises (every probe failure is a line in ``errors``), never
carries a secret (the Keychain is asked for attributes only, never ``-w``;
process entries keep argv, not the environment — a token lives there), and
stays cheap: one ``ps``, two ``security`` calls, one ``lsof``, all bounded
by ``timeout_s`` and only ever run on an adoption.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from claude_swap import macos_keychain
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE
from claude_swap.process_detection import _ENV_TOKEN_BOUNDARY, config_dir_matcher

# pid ppid "Sat Sep  5 09:48:00 2026" argv+env — the lstart field is five
# space-separated words, so the line is matched, not split.
_PS_LINE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+"
    r"([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$"
)
# The argv/environment boundary is process_detection._ENV_TOKEN_BOUNDARY:
# ``ps -E`` appends ``NAME=value`` words after argv, a flag like
# ``--effort=max`` starts with ``-`` and is not a boundary. An env value that
# itself holds a space yields words without ``=``, which the env parse skips;
# a value holding `` word=`` text is split there and can only under-report.
_MDAT = re.compile(r'"mdat"<timedate>=0x[0-9A-Fa-f]+\s+"(\d{14})Z')
_VERSIONS_DIR = "/.local/share/claude/versions/"

COMMAND_CHARS = 120
PROCESS_CAP = 300
# ``security find-generic-password`` exit status for "item not found"
# (errSecItemNotFound, live on macOS 26); every other non-zero is a failure.
SECURITY_RC_NOT_FOUND = 44
# ``lstart`` is parsed in the C locale ("Sat Sep  5 09:48:00 2026"); a
# localized ps ("dom.  6 sept.") would parse nothing and say nothing.
_C_LOCALE_ENV = {"LC_ALL": "C", "LANG": "C"}


# The daemon runs under launchd with a short PATH (no /usr/sbin, where lsof
# lives); prefer the system binaries by absolute path, fall back to PATH.
_TOOL_PATHS = {
    "ps": ("/bin/ps",),
    "lsof": ("/usr/sbin/lsof", "/usr/bin/lsof"),
    "security": ("/usr/bin/security",),
}


def _tool(name: str) -> str:
    for candidate in _TOOL_PATHS.get(name, ()):
        if os.access(candidate, os.X_OK):
            return candidate
    return name


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_claude_binary(argv0: str) -> bool:
    """The Claude Code executable in any of its spellings: ``claude`` on PATH,
    the ``ClaudeCode.app`` bundle binary, or a bare ``versions/<v>`` file (how
    the bg daemon launches its spares)."""
    if argv0.rsplit("/", 1)[-1] == "claude":
        return True
    return _VERSIONS_DIR in argv0


def parse_ps_claude(text: str, *, cap: int = PROCESS_CAP) -> list[dict]:
    """Claude Code processes out of ``ps -o pid=,ppid=,lstart=,command=`` (with
    ``-E`` the environment follows argv). Returns at most ``cap`` entries."""
    procs: list[dict] = []
    for line in text.splitlines():
        m = _PS_LINE.match(line)
        if not m:
            continue
        pid, ppid, started, rest = m.groups()
        parts = _ENV_TOKEN_BOUNDARY.split(rest, maxsplit=1)
        argv = parts[0].strip()
        argv0 = argv.split(None, 1)[0] if argv else ""
        if not _is_claude_binary(argv0):
            continue
        env: dict[str, str] = {}
        if len(parts) == 2:
            for token in parts[1].split():
                if "=" in token:
                    k, v = token.split("=", 1)
                    env[k] = v
        procs.append({
            "pid": int(pid),
            "ppid": int(ppid),
            "started": started,
            "command": argv[:COMMAND_CHARS],
            "configDir": env.get("CLAUDE_CONFIG_DIR"),
            "home": env.get("HOME"),
            "tokenEnv": "CLAUDE_CODE_OAUTH_TOKEN" in env,
        })
        if len(procs) >= cap:
            break
    return procs


def count_ps_lines(text: str) -> int:
    """How many lines of ``ps`` output the parser understands at all — zero
    on non-empty output means the format is not the C-locale one."""
    return sum(1 for line in text.splitlines() if _PS_LINE.match(line))


def parse_security_mdat(text: str) -> str | None:
    """Modification date of a Keychain item from ``security
    find-generic-password`` attribute output (no ``-w``), as ISO UTC."""
    m = _MDAT.search(text)
    if not m:
        return None
    raw = m.group(1)
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}:{raw[12:14]}Z"


def parse_lsof(text: str) -> list[dict]:
    """``lsof -F pc`` field output → ``[{pid, command}]``."""
    openers: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = {"pid": int(line[1:]), "command": ""}
            openers.append(current)
        elif line.startswith("c") and current is not None:
            current["command"] = line[1:]
    return openers


def _file_summary(path: Path, *, identity: bool) -> dict:
    out: dict = {"path": str(path)}
    try:
        st = path.stat()
    except OSError:
        out["exists"] = False
        return out
    out["exists"] = True
    out["mtime"] = _iso(st.st_mtime)
    out["size"] = st.st_size
    if identity:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out["parseError"] = f"{type(e).__name__}: {e}"
            return out
        oauth = data.get("oauthAccount") if isinstance(data, dict) else None
        oauth = oauth if isinstance(oauth, dict) else {}
        out["oauthEmail"] = oauth.get("emailAddress")
        out["accountUuid8"] = (oauth.get("accountUuid") or "")[:8] or None
        out["orgUuid8"] = (oauth.get("organizationUuid") or "")[:8] or None
        projects = data.get("projects") if isinstance(data, dict) else None
        out["projects"] = len(projects) if isinstance(projects, dict) else None
    return out



def collect_adopt_snapshot(
    config_path: Path,
    *,
    session_dir: Path | None,
    keychain_account: str,
    live_service: str = CLAUDE_CODE_KEYCHAIN_SERVICE,
    profile_service: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    timeout_s: float = 5.0,
    platform: str = sys.platform,
    ps_cap: int = PROCESS_CAP,
) -> dict:
    """The snapshot dict (see module docstring). Never raises."""
    t0 = time.monotonic()
    errors: list[str] = []

    def run(
        name: str, cmd: list[str], env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess | None:
        try:
            return runner(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{name}: timed out after {timeout_s}s")
        except Exception as e:  # noqa: BLE001 — a probe must never take the tick down
            errors.append(f"{name}: {type(e).__name__}: {e}")
        return None

    def keychain(service: str) -> dict:
        info: dict = {"service": service, "present": None, "mdat": None}
        if platform != "darwin":
            info["skipped"] = "not macOS"
            return info
        result = run(
            "security",
            [_tool("security"), "find-generic-password", "-s", service, "-a", keychain_account],
        )
        if result is None:
            return info
        if result.returncode == SECURITY_RC_NOT_FOUND:
            info["present"] = False
            return info
        if result.returncode != 0:
            # Anything but "no such item" (locked/unavailable Keychain) is a
            # failed probe, not an absent item — say so.
            stderr = (result.stderr or "").strip().splitlines()
            errors.append(
                f"security: rc={result.returncode}"
                f"{': ' + stderr[0] if stderr else ''}"
            )
            return info
        info["present"] = True
        info["mdat"] = parse_security_mdat(result.stdout or "")
        return info

    # Processes: on macOS one ``ps -E`` carries every same-user process's
    # environment; elsewhere ``ps`` lists argv and /proc (Linux) fills the env.
    ps_cmd = [_tool("ps"), "-wwaxE" if platform == "darwin" else "-wwax",
              "-o", "pid=,ppid=,lstart=,command="]
    procs: list[dict] = []
    truncated = False
    result = run("ps", ps_cmd, env={**os.environ, **_C_LOCALE_ENV})
    if result is not None:
        stdout = result.stdout or ""
        if result.returncode != 0:
            stderr = (result.stderr or "").strip().splitlines()
            errors.append(
                f"ps: rc={result.returncode}{': ' + stderr[0] if stderr else ''}"
            )
        total = len(stdout.splitlines())
        parsed = count_ps_lines(stdout)
        if total and not parsed:
            errors.append(
                f"ps: parsed 0 of {total} lines — unexpected format (locale?)"
            )
        procs = parse_ps_claude(stdout, cap=ps_cap)
        truncated = len(procs) >= ps_cap
        if platform != "darwin" and os.path.isdir("/proc/self"):
            for p in procs:
                try:
                    environ = Path(f"/proc/{p['pid']}/environ").read_bytes()
                except OSError:
                    continue
                for chunk in environ.split(b"\0"):
                    if chunk.startswith(b"CLAUDE_CONFIG_DIR="):
                        p["configDir"] = chunk.split(b"=", 1)[1].decode("utf-8", "replace")
                    elif chunk.startswith(b"HOME="):
                        p["home"] = chunk.split(b"=", 1)[1].decode("utf-8", "replace")
                    elif chunk.startswith(b"CLAUDE_CODE_OAUTH_TOKEN="):
                        p["tokenEnv"] = True

    openers: list[dict] = []
    result = run("lsof", [_tool("lsof"), "-F", "pc", str(config_path)])
    if result is not None:
        openers = parse_lsof(result.stdout or "")  # rc 1 = nobody holds it

    on_profile = (
        config_dir_matcher(session_dir) if session_dir is not None else (lambda _v: False)
    )
    lock_path = config_path.with_name(config_path.name + ".lock")
    lock = _file_summary(lock_path, identity=False)
    lock.pop("path", None)

    snapshot = {
        "configFile": _file_summary(config_path, identity=True),
        "lock": lock,
        "profileConfig": (
            _file_summary(session_dir / ".claude.json", identity=True)
            if session_dir is not None
            else None
        ),
        "keychainLive": keychain(live_service),
        "keychainProfile": keychain(profile_service) if profile_service else None,
        "processes": {
            "claude": len(procs),
            "withoutConfigDir": [p["pid"] for p in procs if p["configDir"] is None],
            "withTokenEnv": [p["pid"] for p in procs if p["tokenEnv"]],
            "onAdoptedProfile": [
                p["pid"]
                for p in procs
                if p["configDir"] is not None and on_profile(p["configDir"])
            ],
            "truncated": truncated,
            "list": procs,
        },
        "openers": openers,
        "errors": errors,
    }
    snapshot["elapsedMs"] = int((time.monotonic() - t0) * 1000)
    return snapshot


def default_adopt_snapshot(*, config_path: Path, session_dir: Path | None, **_: object) -> dict:
    """The engine's collector: live tools, the Keychain names Claude Code and
    cswap derive for the active store and the adopted slot's profile."""
    from claude_swap.session import keychain_service_name

    profile_service = (
        keychain_service_name(session_dir) if session_dir is not None else None
    )
    try:
        account = macos_keychain.keychain_account_name()
    except Exception:  # noqa: BLE001 — a missing username must not skip the snapshot
        account = os.environ.get("USER", "")
    return collect_adopt_snapshot(
        config_path,
        session_dir=session_dir,
        keychain_account=account,
        profile_service=profile_service,
    )
