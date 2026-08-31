"""Active token refresh for parked slots — no session landing (CON-1024).

The usage collector deliberately never rotates a session profile's token
family (doing so from the backup copy would strand the profile on a consumed
generation and log the next ``cswap run`` out — CON-849). The flip side: a
parked slot whose profile family rotated past its backup is unhealable by
any collect pass — the access token expires, the seed guard refuses the
backup's consumed grant, and the slot stays "token expired" until a human
lands an agent on it. ``cswap refresh`` closes that gap by doing what the
session bootstrap does — one refresh-token POST plus a full re-seed — but
without launching claude:

- The freshest generation wins: the profile's hashed keychain entry when one
  exists (claude rotates into it; the plaintext seed below it is a CONSUMED
  generation whose reuse the server may answer by revoking the login), else
  the profile's plaintext ``.credentials.json``, else the slot backup.
- The successor is persisted the way ``_bootstrap`` seeds a profile: slot
  backup + plaintext ``.credentials.json`` + seed-fingerprint stamp, with
  the stale keychain entry deleted so it cannot shadow the fresh seed.
- Slots that must not be touched are reported honestly instead: a live
  session (its claude refreshes lazily — consuming the grant under it would
  log it out), a revoked credential (no refresh token anywhere: only a
  re-login helps), a store-condemned lineage (same parole rule as the
  collector), and an unreadable keychain (guessing could consume a
  superseded generation).

Locking mirrors the bootstrap: the account ``FileLock`` serializes against
``cswap run`` bootstraps and swap/move relocations; the profile-scoped
proper-lockfile pair excludes a claude that was pointed at the profile
outside cswap. The token-endpoint POST runs inside both.
"""

from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap import macos_keychain
from claude_swap.claude_locks import CREDENTIALS_STALENESS_S, proper_lockfile
from claude_swap.exceptions import LockError
from claude_swap.inference_token import is_inference_token_credentials
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.oauth import (
    credential_fingerprint,
    extract_oauth_data,
    is_oauth_token_expired,
    try_refresh_oauth_credentials,
)
from claude_swap.session import (
    SEED_FINGERPRINT_FILE,
    STALE_MARKER,
    delete_macos_keychain_entry,
    keychain_service_name,
    read_seed_fingerprint,
    session_identity_drifted,
)
from claude_swap.usage_store import FetchRecord

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# The refresh holds the account FileLock across one token-endpoint POST
# (bounded below) plus small file writes — same headroom rationale as the
# session bootstrap's lock budget.
_REFRESH_LOCK_TIMEOUT_S = 30.0
# Network budget for the POST. Unlike the active-account path (6s, squeezed
# under a contended switch's acquire budget), nothing time-critical contends
# for a parked slot's lock — allow the endpoint's own default.
_REFRESH_POST_TIMEOUT_S = 10.0

# Outcome vocabulary (stable: scripts key off these).
REFRESHED = "refreshed"  # grant consumed, successor persisted everywhere
FRESH = "fresh"  # access token not expired — nothing to do
LIVE_SESSION = "live-session"  # a live claude owns the family; it heals lazily
ACTIVE_SLOT = "active-slot"  # the live default login; its own path heals it
RELOGIN_REQUIRED = "relogin-required"  # dead/absent refresh token — human only
TRANSIENT_ERROR = "transient-error"  # network blip; safe to retry later
DEFERRED = "deferred"  # unsafe to act now (locks, unreadable keychain)
NO_CREDENTIALS = "no-credentials"
API_KEY = "api-key"  # API-key slots have no OAuth family to refresh
# Pre-activation heal outcomes (CON-1579; ``heal_backup_before_activation``).
BACKUP_CURRENT = "backup-current"  # backup is the slot's newest generation (or no profile)
RESYNCED = "resynced"  # backup adopted the profile's FRESH generation — no grant consumed


@dataclass(frozen=True)
class RefreshReport:
    """One slot's refresh outcome."""

    number: str
    email: str
    outcome: str
    detail: str | None = None
    # Whether the post-refresh live usage read succeeded (the "last seen"
    # proof). Only meaningful for ``REFRESHED``.
    usage_read: bool = False


def _read_profile_credentials(
    session_dir: Path,
) -> tuple[str | None, str | None]:
    """(credentials, error) — the profile's freshest readable generation.

    Distinguishes the sources where ``read_session_credentials`` does not:
    when a hashed keychain entry EXISTS but cannot be read, the plaintext
    below it is a consumed predecessor — POSTing it risks the documented
    reuse reaction (whole-login revocation), so the caller must defer
    rather than fall back.
    """
    if not session_dir.is_dir():
        return None, None
    if Platform.detect() == Platform.MACOS:
        service = keychain_service_name(session_dir)
        account = macos_keychain.keychain_account_name()
        try:
            if macos_keychain.item_exists(service, account):
                creds = macos_keychain.get_password(service, account)
                if not creds:
                    return None, "keychain entry unreadable"
                return creds, None
        except macos_keychain.KeychainError:
            return None, "keychain unavailable"
    try:
        return (
            (session_dir / ".credentials.json").read_text(encoding="utf-8"),
            None,
        )
    except (OSError, ValueError):
        return None, None


def _reseed_profile(session_dir: Path, credentials: str) -> None:
    """Persist a refreshed generation into the profile, bootstrap-shaped:
    stale keychain entry deleted (it would shadow the plaintext), plaintext
    seed written, seed fingerprint re-stamped (backup now holds the SAME
    generation, so the stamp keeps the collector's seed guard truthful)."""
    delete_macos_keychain_entry(session_dir)
    creds_path = session_dir / ".credentials.json"
    creds_path.write_text(credentials, encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(creds_path, 0o600)
    stamp_path = session_dir / SEED_FINGERPRINT_FILE
    stamp_path.write_text(
        credential_fingerprint(credentials) or "", encoding="utf-8"
    )
    if sys.platform != "win32":
        os.chmod(stamp_path, 0o600)


def _backup_is_newer(session_dir: Path, backup: str) -> str | None:
    """Why the BACKUP, not the profile, is the slot's newer generation — or
    ``None`` when the profile ran ahead (the incident shape).

    Fingerprint inequality alone does not say who ran ahead (review r.1 of
    CON-1579): a re-login/re-add or a persisted rotation rewrites the BACKUP
    under a live session and leaves the profile with its older family plus
    the stale marker (``_post_backup_write``); seeding stamps the generation
    both copies started from, so a stamp that no longer matches the backup
    means the backup moved after the profile was seeded. Shared by the
    pre-activation heal and the parked-slot resync so the two never disagree.
    """
    if (session_dir / STALE_MARKER).exists():
        return "profile marked stale — the backup is the newer login"
    seed = read_seed_fingerprint(session_dir)
    if seed and backup and seed != credential_fingerprint(backup):
        return "backup rewritten after the profile was seeded — the backup is newer"
    return None


def refresh_account(
    switcher: ClaudeAccountSwitcher, identifier: str
) -> RefreshReport:
    """Refresh one slot's access token in place. Raises only for an
    unresolvable identifier (mirrors every other account command)."""
    account_num, email, org_uuid = switcher.resolve_account(identifier)
    report = _refresh_resolved(switcher, account_num, email, org_uuid)
    if report.outcome != REFRESHED:
        return report

    # Live proof outside the lock: one collect pass restricted to this slot.
    # The fetch serves from the just-reseeded profile (or refreshed backup),
    # so success moves the store's measurement — "last seen" updates without
    # any landing. Best-effort: the refresh itself already succeeded.
    try:
        before = switcher._usage_store.clock()
        info = (int(account_num), email, "", org_uuid or "", False,
                switcher.read_account_credentials(account_num, email), "")
        entries = switcher._collect_usage_entries([info], fetch={account_num})
        entry = entries[account_num]
        # A NEW measurement only — pre-existing last-good would claim a
        # "live read" that never happened (a failed or claim-lost fetch).
        usage_read = (
            entry.sentinel is None
            and entry.fetched_at is not None
            and entry.fetched_at >= before
        )
    except Exception:
        switcher._logger.warning(
            "Post-refresh usage read failed for account %s; the next "
            "collect pass will pick the fresh token up.", account_num,
            exc_info=True,
        )
        usage_read = False
    return RefreshReport(report.number, report.email, REFRESHED,
                         report.detail, usage_read)


def _refresh_resolved(
    switcher: ClaudeAccountSwitcher,
    account_num: str,
    email: str,
    org_uuid: str,
) -> RefreshReport:
    def out(outcome: str, detail: str | None = None) -> RefreshReport:
        return RefreshReport(account_num, email, outcome, detail)

    if switcher._account_kind(account_num) == "api_key":
        return out(API_KEY)

    session_dir = switcher._session_dir(account_num, email)
    try:
        with FileLock(switcher.lock_file, timeout=_REFRESH_LOCK_TIMEOUT_S):
            # The ACTIVE slot is never refreshed from here: its live
            # credential is Claude Code's store, while this path reads the
            # profile/backup copy — for the active account that copy is a
            # consumed predecessor whenever CC rotated during normal use
            # (rotation-before-collection), and POSTing it is at best an
            # invalid_grant strike on a LIVE login, at worst the documented
            # reuse reaction. The active account already has a locked
            # refresh path (_fetch_active_usage) that any collect pass runs.
            # Judged under the lock, so a concurrent `cswap switch` (same
            # lock) cannot make this slot active mid-refresh.
            current = switcher._get_current_account()
            if current is not None:
                cur_email, cur_org = current
                if cur_email == email and (
                    not cur_org or not org_uuid or cur_org == org_uuid
                ):
                    return out(ACTIVE_SLOT)
            # Re-checked under the lock: a `cswap run` bootstrap racing us
            # holds the same lock, so a session that appears later than this
            # check will find the profile already reseeded — never half-written.
            if switcher._live_session_pids(account_num, email):
                return out(LIVE_SESSION)

            profile_creds = None
            profile_owned = session_dir.is_dir() and not session_identity_drifted(
                session_dir, email, org_uuid
            )
            if profile_owned:
                profile_creds, err = _read_profile_credentials(session_dir)
                if profile_creds is None and err is not None:
                    return out(DEFERRED, err)

            token_profile = False
            if profile_creds is not None and is_inference_token_credentials(
                profile_creds
            ):
                # CON-1329: the profile runs on the attached inference token
                # and holds no family — the backup LOGIN is what expires and
                # gets refreshed here; the profile is never re-seeded with the
                # family (review r.1: judged the token as "no refresh token"
                # → relogin-required on a healthy slot). Shape alone decides
                # (review r.2): after a detach the profile may still hold the
                # token credential until the next run re-seeds it.
                profile_creds = None
                token_profile = True

            if profile_creds is not None:
                candidate = profile_creds
            else:
                backup = switcher.read_account_credentials(account_num, email)
                backup = switcher.reconcile_pending_rotation_locked(
                    account_num, email, backup
                )
                if not backup:
                    return out(NO_CREDENTIALS)
                seed = read_seed_fingerprint(session_dir)
                if seed and seed == credential_fingerprint(backup):
                    # The profile's family is the slot's newest generation but
                    # is unreadable/absent while its seed (= this backup) is a
                    # consumed grant — POSTing it is the account-death shape.
                    return out(
                        DEFERRED,
                        "backup is the profile's consumed seed generation",
                    )
                candidate = backup

            oauth_data = extract_oauth_data(candidate)
            if not oauth_data or not oauth_data.get("accessToken"):
                return out(NO_CREDENTIALS)
            if not oauth_data.get("refreshToken"):
                # Revoked shape: nothing to POST, only a human re-login helps.
                return out(RELOGIN_REQUIRED, "no refresh token")
            if not is_oauth_token_expired(oauth_data.get("expiresAt")):
                if profile_creds is not None and _backup_lags_profile(
                    switcher, account_num, email, session_dir, profile_creds
                ):
                    # CON-1595: a FRESH profile over a lagging backup was
                    # "fresh — nothing to touch" for ever; the fleet's refresh
                    # job ran into the incident's exact shape every 10 minutes
                    # and said so. Adopt the profile's generation into the
                    # backup (no POST, no grant consumed) and reseed the
                    # profile bootstrap-shaped, exactly like REFRESHED does —
                    # the hand recipe behind the TOKEN-DRIFT sensor line.
                    with (
                        proper_lockfile(
                            session_dir / ".oauth_refresh.lock",
                            staleness=CREDENTIALS_STALENESS_S,
                        ),
                        proper_lockfile(
                            session_dir.parent / (session_dir.name + ".lock"),
                            staleness=CREDENTIALS_STALENESS_S,
                        ),
                    ):
                        switcher.write_account_credentials(
                            account_num, email, profile_creds
                        )
                        _reseed_profile(session_dir, profile_creds)
                    return out(RESYNCED, "backup adopted the profile's generation")
                return out(FRESH)

            identity = {account_num: (email, org_uuid or "")}
            entry = switcher._usage_store.entries(identity)[account_num]
            if entry.token_dead() and not switcher._parole_eligible(
                entry, candidate
            ):
                return out(RELOGIN_REQUIRED, "credential lineage condemned")

            # Exclude a claude pointed at this profile outside cswap: same
            # two locks, same order, as its own refresh path.
            with (
                proper_lockfile(
                    session_dir / ".oauth_refresh.lock",
                    staleness=CREDENTIALS_STALENESS_S,
                ) if profile_owned else nullcontext(),
                proper_lockfile(
                    session_dir.parent / (session_dir.name + ".lock"),
                    staleness=CREDENTIALS_STALENESS_S,
                ) if profile_owned else nullcontext(),
            ):
                result = try_refresh_oauth_credentials(
                    candidate, timeout_s=_REFRESH_POST_TIMEOUT_S
                )
                if result.error in ("invalid_grant", "no_refresh_token"):
                    # Permanently unrefreshable — advance the store's strike
                    # so every surface flips to "re-login needed" instead of
                    # anyone retrying a server-rejected grant.
                    switcher._usage_store.record(
                        {
                            account_num: FetchRecord(
                                error=result.error,
                                credential_fingerprint=credential_fingerprint(
                                    candidate
                                ),
                            )
                        },
                        identity,
                    )
                    return out(RELOGIN_REQUIRED, result.error)
                if result.error is not None:
                    return out(TRANSIENT_ERROR, result.error)
                if not result.credentials:
                    # Contract says a success carries credentials; if that
                    # ever drifts, retry later — never condemn a lineage on
                    # a shape the token endpoint did not reject.
                    return out(TRANSIENT_ERROR, "empty refresh result")

                working = result.credentials
                # The grant is consumed: the successor MUST survive. Backup
                # first (write_account_credentials expects the held lock),
                # then the profile re-seed.
                switcher.write_account_credentials(account_num, email, working)
                if profile_owned and not token_profile:
                    _reseed_profile(session_dir, working)
            # A successful rotation is proof the lineage is alive: lift any
            # stale quarantine state the same way a re-login does.
            switcher._usage_store.clear_dead_token([account_num], identity)
            return out(REFRESHED)
    except LockError:
        # Covers ClaudeCodeLockTimeout too (a live claude mid-refresh on
        # this profile): the credential is being handled — never steal.
        return out(DEFERRED, "credential locks held elsewhere")


def _backup_lags_profile(
    switcher: ClaudeAccountSwitcher,
    account_num: str,
    email: str,
    session_dir: Path,
    profile_creds: str,
) -> bool:
    """Whether the stored backup is a superseded (or missing) generation of
    the family the profile holds — the shape ``switch`` used to activate dead.
    Caller holds ``switcher.lock_file``; a pending spilled rotation is folded
    into the backup first, so a spill that already carries the profile's
    generation is not mistaken for a lag."""
    backup = switcher.read_account_credentials(account_num, email)
    backup = switcher.reconcile_pending_rotation_locked(account_num, email, backup)
    if backup and credential_fingerprint(backup) == credential_fingerprint(
        profile_creds
    ):
        return False
    return _backup_is_newer(session_dir, backup) is None


def refresh_accounts(
    switcher: ClaudeAccountSwitcher,
    identifiers: list[str],
) -> list[RefreshReport]:
    """Refresh several slots sequentially (one POST at a time — the token
    endpoint has per-token budgets, and slot locks are independent)."""
    return [refresh_account(switcher, ident) for ident in identifiers]


def heal_backup_before_activation(
    switcher: ClaudeAccountSwitcher,
    account_num: str,
    email: str,
    org_uuid: str,
) -> RefreshReport:
    """Make a slot's stored backup activatable when its session profile has
    rotated past it (CON-1579).

    ``switch`` activates the slot BACKUP. Once a ``cswap run`` session has run
    on the slot, its claude rotates the token family inside the profile and
    nothing syncs it back — the backup is then a CONSUMED generation, and
    activating it lands the default login dead on arrival ("Login expired ·
    Please run /login" on the first request; live incident 2026-08-31, three
    slots in a row). Outcomes:

    - ``BACKUP_CURRENT`` — no profile, a profile owned by another login, an
      unusable profile credential, the same lineage — or the BACKUP is the
      newer generation: fingerprint inequality alone does not say who ran
      ahead, so the two ordering oracles the codebase already keeps are
      read first. ``STALE_MARKER`` (set by ``_post_backup_write`` when the
      backup was rewritten under a live session — re-login/re-add, a
      persisted rotation) means the profile copy is presumed stale; a seed
      stamp that no longer matches the backup means the backup moved after
      the profile was seeded. Either way the backup wins, and the superseded
      profile copy is dropped when no session is live (what
      ``setup_session`` would do on the next ``cswap run`` anyway).
    - ``RESYNCED`` — the profile's generation is fresh: adopted into the
      backup as-is (no POST, no grant consumed). The write invalidates the
      idle profile's credential material, so the family ends up with ONE
      copy — the one about to go live.
    - ``REFRESHED`` — the profile's generation is expired: one refresh POST
      with the PROFILE's grant via the parked-slot refresh path, successor
      persisted; the reseeded profile copy is then dropped for the same
      one-copy reason.
    - ``LIVE_SESSION`` — a live claude owns the family (detail: its PIDs).
      Handing out either copy would put one rotating refresh token in two
      config dirs with a running writer on the other side; the caller must
      refuse, not warn.
    - ``RELOGIN_REQUIRED`` / ``TRANSIENT_ERROR`` / ``DEFERRED`` /
      ``NO_CREDENTIALS`` — as ``refresh_account``; the caller refuses the
      activation rather than landing a dead login.

    The freshest-generation read follows ``_read_profile_credentials`` (an
    existing-but-unreadable keychain entry is DEFERRED, never a fall-back to
    the plaintext seed — that seed is the consumed generation).
    """
    def out(outcome: str, detail: str | None = None) -> RefreshReport:
        return RefreshReport(account_num, email, outcome, detail)

    if switcher._account_kind(account_num) == "api_key":
        return out(API_KEY)
    session_dir = switcher._session_dir(account_num, email)
    if not session_dir.is_dir():
        return out(BACKUP_CURRENT, "no session profile")
    if session_identity_drifted(session_dir, email, org_uuid):
        return out(BACKUP_CURRENT, "profile is logged in as another account")
    profile_creds, err = _read_profile_credentials(session_dir)
    if profile_creds is None:
        if err is not None:
            return out(DEFERRED, err)
        return out(BACKUP_CURRENT, "profile holds no credential")
    profile_fp = credential_fingerprint(profile_creds)
    backup = switcher.read_account_credentials(account_num, email)
    if backup and credential_fingerprint(backup) == profile_fp:
        return out(BACKUP_CURRENT)
    pids = switcher._live_session_pids(account_num, email)

    # Who ran ahead? Inequality alone cannot tell (review r.1, CON-1579) —
    # the two ordering oracles decide (``_backup_is_newer``); the superseded
    # profile copy is dropped when no session is live (what ``setup_session``
    # would do on the next ``cswap run`` anyway).
    newer = _backup_is_newer(session_dir, backup)
    if newer is not None:
        if not pids:
            switcher._invalidate_session_credentials(account_num, email)
        return out(BACKUP_CURRENT, newer)

    profile_oauth = extract_oauth_data(profile_creds)
    if not profile_oauth or not (
        profile_oauth.get("accessToken") and profile_oauth.get("refreshToken")
    ):
        # Not a full OAuth pair: nothing safer than the backup to offer.
        return out(BACKUP_CURRENT, "profile credential is not a full token pair")
    if pids:
        return out(LIVE_SESSION, ", ".join(str(p) for p in pids))

    if is_oauth_token_expired(profile_oauth.get("expiresAt")):
        report = _refresh_resolved(switcher, account_num, email, org_uuid)
        if report.outcome == REFRESHED:
            # The parked-slot path reseeds the profile for a future `cswap
            # run`; here the generation is about to become the live login,
            # so the profile copy must not survive (one family, one copy).
            switcher._invalidate_session_credentials(account_num, email)
            return report
        if report.outcome != FRESH:
            return report
        # FRESH: a concurrent refresh (its own lock) renewed the profile
        # between our read and its lock — the backup still lags; adopt the
        # now-fresh generation below instead of misreporting a failure.

    try:
        with FileLock(switcher.lock_file, timeout=_REFRESH_LOCK_TIMEOUT_S):
            # Re-judged under the lock: a `cswap run` bootstrap or a claude
            # landing on the profile since the pre-lock read owns the family.
            pids = switcher._live_session_pids(account_num, email)
            if pids:
                return out(LIVE_SESSION, ", ".join(str(p) for p in pids))
            fresh, err = _read_profile_credentials(session_dir)
            if fresh is None:
                return out(DEFERRED, err or "profile credential vanished")
            fresh_oauth = extract_oauth_data(fresh)
            if not fresh_oauth or not (
                fresh_oauth.get("accessToken") and fresh_oauth.get("refreshToken")
            ):
                return out(DEFERRED, "profile credential changed under the lock")
            # write_account_credentials expects the held lock; its
            # post-write hook drops the idle profile's credential copy.
            switcher.write_account_credentials(account_num, email, fresh)
    except LockError:
        return out(DEFERRED, "credential locks held elsewhere")
    return out(RESYNCED)
