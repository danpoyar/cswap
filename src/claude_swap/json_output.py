"""Serialization helpers for ``--json`` structured output.

Centralizes the schema-v1 shapes so ``--list``/``--status``/``--switch`` agree on
field names (camelCase, matching the export envelope in transfer.py) and on how the
internal usage dict is projected to JSON. Callers build payloads here; the CLI does
the single ``json.dumps`` (see cli.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from claude_swap import oauth, pace

# Bump only on a breaking change to any payload shape. Scripts key off this.
SCHEMA_VERSION = 1

# Sentinel entries that ``_collect_usage`` / ``_fetch_active_usage`` yield in place
# of a usage dict. Kept here (the serialization hub) so the human renderer and the
# JSON projection agree instead of scattering raw strings.
USAGE_NO_CREDENTIALS = "no credentials"
USAGE_TOKEN_EXPIRED = "token expired"
# API-key (``/login`` managed key) accounts have no subscription quota; usage is
# reported as this sentinel instead of being fetched from the OAuth usage API.
USAGE_API_KEY = "api key"
# The active account's macOS Keychain was unreadable (locked / denied / timeout)
# with no plaintext fallback — distinct from a genuinely empty slot, so the user
# isn't misled into an unnecessary re-login.
USAGE_KEYCHAIN_UNAVAILABLE = "keychain unavailable"
# The stored refresh-token lineage is dead (repeated ``invalid_grant``). The
# account is quarantined from fetching until a re-login (``cswap login`` / ``add``)
# replaces the credential; distinct from "token expired" (which Claude Code can
# refresh on its own) because only the user can fix it.
USAGE_RELOGIN_REQUIRED = "re-login needed"

# Plain-language notes keyed by ``usageStatus`` (CON-2639). One table for every
# surface — the human ``list`` renderer, the menu bar and the JSON projection
# (``usageStatusText``) — so a state is described identically everywhere, and
# an operator never has to decode a status token to learn what to do.
STATUS_NOTES = {
    "token_expired": "token expired — refresh with: cswap refresh",
    "api_key": "API key (no quota)",
    "keychain_unavailable": "keychain unavailable — locked or in use; try again",
    "relogin_required": "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add",
    "no_credentials": "no credentials stored for this slot — run: cswap add",
}

# Plain-language notes for the fetch-error kinds ``oauth._classify_usage_error``
# yields (``http-NNN`` / ``timeout`` / ``network`` / ``bad-response`` / an
# exception type name). The kind stays beside the note for logs, scripts and
# grep; the note says what happened and what to do. HTTP meanings follow the
# API error table (https://docs.claude.com/en/api/errors). ``http-429`` on the
# usage gauge is the endpoint's per-token polling budget (see poll_policy), not
# the account's own window — its note must never read as "limit reached".
FETCH_ERROR_NOTES = {
    "http-401": "token rejected (expired or revoked) — re-login needed",
    "http-402": "billing problem — subscription unpaid",
    "http-403": "access denied — organization disabled Claude Code or no permission",
    "http-404": "usage endpoint not found — update cswap",
    "http-429": "usage gauge rate-limited (too many polls) — cswap backs off and retries",
    "http-529": "Anthropic overloaded — retrying",
    "timeout": "Anthropic did not answer in time — retrying",
    "network": "network problem — retrying",
    "bad-response": "unexpected reply from Anthropic — retrying",
}


def explain_fetch_error(kind: str | None) -> str | None:
    """Plain-language note for a fetch-error kind, ``None`` for no error.

    Unknown kinds still get a sentence: HTTP 5xx is a server-side failure
    (retried), any other HTTP status is a rejected request, and a bare
    exception name is a failed fetch — the kind is quoted so the log can be
    searched for it.
    """
    if not kind:
        return None
    note = FETCH_ERROR_NOTES.get(kind)
    if note:
        return note
    if kind.startswith("http-5"):
        return f"Anthropic server error ({kind}) — retrying"
    if kind.startswith("http-"):
        return f"request rejected by Anthropic ({kind}) — see claude-swap.log"
    return f"usage fetch failed ({kind}) — see claude-swap.log"


def status_note(status: str, last_error: str | None, consecutive_failures: int) -> str | None:
    """``usageStatusText`` for a non-``ok`` status: the sentinel's note, or for
    ``unavailable`` the reason the measurement is missing (the open failure
    streak's note, else "no successful measurement yet")."""
    if status == "ok":
        return None
    note = STATUS_NOTES.get(status)
    if note:
        return note
    if status == "unavailable":
        why = explain_fetch_error(last_error) if consecutive_failures > 0 else None
        return "usage unavailable — " + (why or "no successful measurement yet")
    return None


def _iso_utc(ts: float) -> str:
    """Epoch seconds → ISO-8601 UTC with a ``Z`` suffix (the schema-wide
    timestamp shape, matching the export envelope in transfer.py)."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _window_to_json(entry: dict) -> dict:
    """Project a 5h/7d usage window to JSON, preserving raw ``resetsAt``.

    ``countdown``/``clock`` are recomputed from ``resets_at`` at serialization
    time (the store may serve a measurement hours after its fetch); entries
    without ``resets_at`` fall back to the fetch-time strings.
    """
    out: dict = {"pct": entry["pct"]}
    if "resets_at" in entry:
        out["resetsAt"] = entry["resets_at"]
    cell = oauth.fresh_reset_strings(entry)
    if cell:
        out["countdown"], out["clock"] = cell
    return out


def _pace_fields(entry: dict, fetched_at: float | None) -> dict:
    """Weekly-window pace fields (issue #125): additive, JSON-only.

    Emitted only when pace is computable and not suppressed (see
    ``claude_swap.pace.compute_pace``). ``projectedExhaustionAt`` is a linear
    ETA — wide error bars against real, bursty usage — so it's kept out of
    every human-facing surface and only ever appears here.
    """
    if fetched_at is None:
        return {}
    result = pace.compute_pace(entry, fetched_at=fetched_at)
    if result is None:
        return {}
    out: dict = {
        "expectedPct": round(result.expected_pct, 1),
        "aheadOfPace": result.ahead,
    }
    eta = pace.projected_exhaustion_ts(result, fetched_at=fetched_at)
    if eta is not None:
        out["projectedExhaustionAt"] = _iso_utc(eta)
    will_last = pace.will_last_to_reset(result)
    if will_last is not None:
        out["willLastToReset"] = will_last
    return out


def _weekly_window_to_json(entry: dict, fetched_at: float | None) -> dict:
    """A 7d/scoped window's JSON projection, with pace fields layered in."""
    out = _window_to_json(entry)
    out.update(_pace_fields(entry, fetched_at))
    return out


def _scoped_window_to_json(entry: dict, fetched_at: float | None) -> dict:
    """Project a per-model scoped weekly window, carrying its model name."""
    out = _weekly_window_to_json(entry, fetched_at)
    out["name"] = entry["name"]
    return out


def usage_to_json(usage: dict, fetched_at: float | None = None) -> dict:
    """Convert the internal usage dict to its camelCase JSON projection.

    Sub-keys are emitted only when present in the source (the API does not always
    return every window or pay-as-you-go spend). ``fetched_at`` is the
    measurement's fetch time; passing it adds pace fields to the weekly
    windows (``seven_day``, ``scoped``) only — never ``five_hour`` (issue #125).
    """
    out: dict = {}
    if "five_hour" in usage:
        out["fiveHour"] = _window_to_json(usage["five_hour"])
    if "seven_day" in usage:
        out["sevenDay"] = _weekly_window_to_json(usage["seven_day"], fetched_at)
    if "spend" in usage:
        spend = usage["spend"]
        spend_out: dict = {
            "used": spend["used"],
            "limit": spend["limit"],
            "pct": spend["pct"],
            "currency": spend["currency"],
        }
        if "resets_at" in spend:
            spend_out["resetsAt"] = spend["resets_at"]
        cell = oauth.fresh_reset_strings(spend)
        if cell:
            spend_out["countdown"], spend_out["clock"] = cell
        out["spend"] = spend_out
    if "scoped" in usage:
        out["scoped"] = [_scoped_window_to_json(w, fetched_at) for w in usage["scoped"]]
    return out


def usage_fields(
    entry: dict | str | None, fetched_at: float | None = None
) -> tuple[str, dict | None]:
    """Map a collected usage entry to ``(usageStatus, usage|None)``.

    A collected entry is one of: a usage dict, the ``USAGE_TOKEN_EXPIRED`` sentinel
    (active token expired and the refresh was deferred this pass — lock
    contention, unattributable lineage, or a failed persist; retried
    automatically), the ``USAGE_API_KEY`` sentinel
    (managed API-key account, no subscription quota), the
    ``USAGE_KEYCHAIN_UNAVAILABLE`` sentinel (active Keychain unreadable), the
    ``USAGE_NO_CREDENTIALS`` sentinel, or ``None`` (fetch failed). ``fetched_at``
    is forwarded to ``usage_to_json`` for the weekly pace fields (issue #125).
    """
    if isinstance(entry, dict):
        return "ok", usage_to_json(entry, fetched_at)
    if entry == USAGE_TOKEN_EXPIRED:
        return "token_expired", None
    if entry == USAGE_API_KEY:
        return "api_key", None
    if entry == USAGE_KEYCHAIN_UNAVAILABLE:
        return "keychain_unavailable", None
    if entry == USAGE_RELOGIN_REQUIRED:
        return "relogin_required", None
    if isinstance(entry, str):
        return "no_credentials", None
    return "unavailable", None


def account_ref(number: int | None, email: str) -> dict:
    """A minimal account reference, used for switch ``from``/``to``."""
    return {"number": number, "email": email}


def usage_freshness_fields(
    fetched_at: float | None, age_s: float | None
) -> dict:
    """Additive ``usageFetchedAt``/``usageAgeSeconds`` fields describing how
    old the served ``usage`` measurement is (the store may serve last-good
    data on fetch failure). Emitted only alongside a non-null ``usage``."""
    if fetched_at is None:
        return {}
    fields: dict = {"usageFetchedAt": _iso_utc(fetched_at)}
    if age_s is not None:
        fields["usageAgeSeconds"] = round(age_s, 1)
    return fields


def last_good_usage_fields(
    usage: dict | None, fetched_at: float | None, age_s: float | None
) -> dict:
    """Display-grade last-good usage, separate from decision-grade ``usage``."""
    if not isinstance(usage, dict) or fetched_at is None:
        return {}
    freshness = usage_freshness_fields(fetched_at, age_s)
    out = {
        "lastGoodUsage": usage_to_json(usage, fetched_at),
        "lastGoodFetchedAt": freshness["usageFetchedAt"],
    }
    if "usageAgeSeconds" in freshness:
        out["lastGoodAgeSeconds"] = freshness["usageAgeSeconds"]
    return out


def fetch_failure_fields(last_error: str | None, consecutive_failures: int) -> dict:
    """Why a slot's measurement stopped moving, when it is a failure rather
    than the scheduler's own cadence — the kind plus its plain-language note
    (``lastErrorText``, CON-2639).

    Without it a consumer cannot tell a deliberately parked account (at its
    limit, next poll scheduled for the reset) from one whose token died hours
    ago: both serve the same quiet last-good numbers, and stale zeroes read
    like a free account. Emitted only while a failure streak is open, so a
    slot that recovered carries nothing.
    """
    if not last_error or consecutive_failures <= 0:
        return {}
    return {
        "lastError": last_error,
        "lastErrorText": explain_fetch_error(last_error),
        "consecutiveFailures": consecutive_failures,
    }


def account_row(
    number: int,
    email: str,
    org_name: str,
    org_uuid: str,
    active: bool,
    usage_entry: dict | str | None,
    *,
    usage_fetched_at: float | None = None,
    usage_age_s: float | None = None,
    last_good_usage: dict | None = None,
    last_error: str | None = None,
    consecutive_failures: int = 0,
    alias: str = "",
    disabled: bool = False,
    next_poll_at: float | None = None,
    token_expired_at: float | None = None,
    inference_token: bool = False,
) -> dict:
    """A full account row for ``--list``."""
    status, usage = usage_fields(usage_entry, usage_fetched_at)
    row = {
        "number": number,
        "email": email,
        "organizationName": org_name,
        "organizationUuid": org_uuid,
        "isOrganization": bool(org_uuid),
        "active": active,
        "usageStatus": status,
        "usage": usage,
    }
    # Additive (CON-2639): the plain-language note for a non-ok status, so a
    # dashboard prints "token expired — refresh with: cswap refresh" instead of
    # the bare status token. Absent on ok rows.
    text = status_note(status, last_error, consecutive_failures)
    if text is not None:
        row["usageStatusText"] = text
    # Additive: when the expired state was first measured (CON-1024), so a
    # dashboard can render "Token expired · <age>" instead of guessing.
    if status == "token_expired" and token_expired_at is not None:
        row["tokenExpiredAt"] = _iso_utc(token_expired_at)
    if alias:
        row["alias"] = alias
    # Additive field: present only when the slot is held out of rotation, so
    # existing consumers keying on the base schema are unaffected.
    if disabled:
        row["disabled"] = True
    if usage is not None:
        row.update(usage_freshness_fields(usage_fetched_at, usage_age_s))
    else:
        row.update(
            last_good_usage_fields(
                last_good_usage, usage_fetched_at, usage_age_s
            )
        )
    # Independent of which usage shape was served: a slot can be failing while
    # its last successful measurement is still inside the decision window.
    row.update(fetch_failure_fields(last_error, consecutive_failures))
    # Additive: when the scheduler plans to measure this slot next. Scheduler
    # state, not measurement state — emitted whether live or last-good usage
    # is being served.
    if next_poll_at is not None:
        row["nextPollAt"] = _iso_utc(next_poll_at)
    # Additive (CON-1329): the slot carries a year-long inference token —
    # sessions run on it, so a stale/dead ``usageStatus`` here describes the
    # quota gauge (the stored login), not the slot's ability to work. The
    # token value itself is never emitted. Consumers pair this with
    # ``usageAgeSeconds`` / ``lastGoodAgeSeconds`` to see how old the gauge is.
    if inference_token:
        row["inferenceToken"] = True
    return row


def error_envelope(exc: Exception) -> dict:
    """The structured error payload emitted on a handled ClaudeSwitchError."""
    return {
        "schemaVersion": SCHEMA_VERSION,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }
