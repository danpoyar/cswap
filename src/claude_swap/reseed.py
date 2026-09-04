"""``cswap reseed`` — the backup's newer login generation goes INTO a session
profile, live sessions included (CON-2030, second half).

Why a door that writes under a live claude. The fleet's healer for a dead
orchestrator login (config repo, ``fleet-sensors.sh`` sensor P →
``yor-slot-move.sh --restart`` → ``cswap run <email> -- --resume``) relied
on the deferred-invalidation marker: the backup was rewritten under the
live session, the profile got ``STALE_MARKER``, and the next ``cswap run``
would re-bootstrap it from the backup. Two facts void that:

- ``setup_session`` honors the marker only on an IDLE profile
  (``stale = marker and not live_sessions_for(...)``), and the reuse probe
  ``claude auth status --json`` answers ``loggedIn: true`` for an EXPIRED
  token (review r.1 of config PR #1253, probe with ``expiresAt: 1000``);
- since 2026-09-04 the operator's terminal tabs live in the orchestrator's
  profile (``cswap run yor@…``, hub ``terminal-account``), so that profile
  is never idle — the restart came back up on the profile's consumed
  generation and printed "Login expired" again.

What the door does. It judges who ran ahead — profile or backup — by the
SEED STAMP (``SEED_FINGERPRINT_FILE``), never by the stale marker (review
r.1 of fork PR #32: ``_post_backup_write`` marks a live profile stale on
EVERY backup rewrite, so the marker lies after the session's next rotation,
and ``_backup_is_newer`` — marker first — would call the consumed backup
"newer"). The same oracle ``_live_session_shares_login`` reads:

- backup unmoved since seeding (stamp == backup) and the profile holds a
  different full token pair → the PROFILE rotated ahead and owns the
  family's newest generation: nothing is written into its store; the
  backup adopts that generation (``adopt_profile_family`` — no POST, no
  grant consumed) so the collector and ``switch`` see the real family.
  Outcome ``PROFILE_AHEAD``.
- backup moved after seeding (stamp != backup) → the profile holds a
  CONSUMED generation (the incident shape: a persisted rotation, a
  re-login/re-add or an ``--even-if-live`` visit rewrote the backup under
  the live session) → the backup's ``claudeAiOauth`` family is written into
  the profile store by the bootstrap-shaped writer ``_reseed_profile``
  (stale hashed keychain entry deleted — it would shadow the plaintext —
  plaintext ``.credentials.json`` written, seed stamp re-stamped), the
  marker is dropped. The profile's own machine-shared families
  (``mcpOAuth`` and friends) stay: each profile rotates its fork
  independently (CON-1432), and the live session's copy is the current
  one. Outcome ``RESEEDED``.
- an EXPIRED backup is proven alive first — one refresh POST with the
  BACKUP's grant (the family's newest, held by no live process; the
  profile's grant is the consumed predecessor). Rejected
  (``invalid_grant`` / no refresh token) → ``RELOGIN_REQUIRED``, the store
  records the strike, the profile is untouched. A transient failure →
  ``TRANSIENT_ERROR``, untouched.
- equal fingerprints → ``IN_SYNC``: nothing to reseed, nothing written.
- the ACTIVE slot (the live default login) is refused (``ACTIVE_SLOT``),
  exactly as ``refresh._refresh_resolved`` refuses it: its live credential
  is Claude Code's own store and the backup copy is a consumed predecessor
  whenever CC rotated in place — POSTing or copying it strikes a LIVE
  login; the active account heals through its own locked collector path
  (``_fetch_active_usage``). Review r.1, Major 2.
- an idle profile whose copy the backup-write hook dropped (one family,
  one copy) must not keep its seed stamp: a stamp over no credential would
  freeze the freshly adopted backup for the collector's seed guard,
  ``cswap refresh`` and this door (review r.1, Major 1; the invariant of
  ``_invalidate_session_credentials``).

Every read of the profile happens under the profile's credential-lock pair
(review r.1, Minor 3): the shared-fields merge writes back what the live
claude holds NOW, not a pre-lock snapshot.

Live sessions do NOT block the door — that is its point. Their PIDs are
reported. What they do with the new generation rests on Claude Code's own
store discipline, quoted from ``ClaudeAccountSwitcher._fetch_active_usage``
(this fork, verified against the 2.1.218 bundle): "Claude Code 2.1.218 is
built to *adopt* an externally rotated credential rather than collide with
it: its refresh takes the ``.oauth_refresh.lock`` + legacy ``.claude.lock``
pair, re-reads the store under the lock, and skips the network call when the
token already changed (race-resolved); its 401 path re-reads the store
before forcing re-auth." That proof is for the ACTIVE store (``~/.claude``);
a session profile is the same code with ``CLAUDE_CONFIG_DIR`` as the config
home (same lock names relative to it — ``claude_locks``), so the door holds
that profile-scoped pair around every store write, exactly like
``refresh._refresh_resolved`` does. The adoption by a live claude in a
PROFILE is not separately proven in this fork: live sessions pick the
generation up at their next 401-path re-read of the store; the guarantee is
a restart of the session (``cswap run N -- --resume``, which the healer does
anyway).

Locking mirrors the bootstrap and the parked-slot refresh: the account
``FileLock`` serializes against ``cswap run`` bootstraps, the collector's
persists and swap/move; the profile-scoped proper-lockfile pair excludes the
profile's own claude mid-refresh. The config lock (``.claude.json.lock``) is
not taken: the door never writes ``.claude.json`` (identity unchanged — a
drifted identity is refused).

Refusals raise :class:`ReseedRefusal` (a ``SessionError``) carrying a stable
``outcome``; the CLI turns them into the switch-style JSON error envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from claude_swap import oauth
from claude_swap.claude_locks import CREDENTIALS_STALENESS_S, proper_lockfile
from claude_swap.credentials import (
    merge_shared_credential_fields,
    shared_credential_fields,
)
from claude_swap.exceptions import LockError, SessionError
from claude_swap.inference_token import is_inference_token_credentials
from claude_swap.locking import FileLock
from claude_swap.refresh import _reseed_profile
from claude_swap.session import (
    SEED_FINGERPRINT_FILE,
    STALE_MARKER,
    read_profile_generation,
    read_seed_fingerprint,
    session_identity_drifted,
)
from claude_swap.usage_store import FetchRecord

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

# Same headroom as the bootstrap and the parked-slot refresh: one POST plus
# small file writes under the account lock.
_RESEED_LOCK_TIMEOUT_S = 30.0
_RESEED_POST_TIMEOUT_S = 10.0

# Outcome vocabulary (stable: scripts key off these).
RESEEDED = "reseeded"  # backup generation now sits in the profile store
IN_SYNC = "in-sync"  # profile and backup hold the same generation
PROFILE_AHEAD = "profile-ahead"  # profile owns the newest generation; backup adopted it
# Refusals (ReseedRefusal.outcome; exit 1).
NO_PROFILE = "no-profile"  # nothing to reseed: no session profile on disk
API_KEY = "api-key"  # API-key slots have no OAuth family
ACTIVE_SLOT = "active-slot"  # the live default login heals through its own path
TOKEN_PROFILE = "token-profile"  # profile runs on an inference token (CON-1329)
IDENTITY_DRIFTED = "identity-drifted"  # profile is logged in as another account
DEFERRED = "deferred"  # unsafe to judge now (Keychain busy, locks held elsewhere)
NO_CREDENTIALS = "no-credentials"  # backup holds no full token pair
RELOGIN_REQUIRED = "relogin-required"  # backup grant rejected / condemned / consumed
TRANSIENT_ERROR = "transient-error"  # expired backup, refresh failed transiently
UNDECIDABLE = "undecidable"  # no seed stamp: who ran ahead cannot be told


class ReseedRefusal(SessionError):
    """The door did not write: ``outcome`` says why (stable vocabulary above),
    ``live_pids`` names the sessions that were running in the profile."""

    def __init__(
        self, outcome: str, message: str, *, live_pids: list[int] | None = None
    ) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.live_pids = list(live_pids or [])


@dataclass(frozen=True)
class ReseedReport:
    """One slot's reseed outcome (only non-refusal outcomes reach a report)."""

    number: str
    email: str
    outcome: str
    detail: str | None = None
    # Fingerprint of the generation the profile store holds after the door
    # (RESEEDED: the one written; PROFILE_AHEAD/IN_SYNC: the profile's own).
    generation: str | None = None
    live_pids: list[int] = field(default_factory=list)

    @property
    def reseeded(self) -> bool:
        return self.outcome == RESEEDED

    @property
    def source(self) -> str | None:
        return "backup" if self.reseeded else None


def _full_pair(credentials: str | None) -> dict | None:
    """The ``claudeAiOauth`` payload when it carries BOTH tokens, else None."""
    if not credentials:
        return None
    data = oauth.extract_oauth_data(credentials)
    if data and data.get("accessToken") and data.get("refreshToken"):
        return data
    return None


def _profile_locks(session_dir: Path):
    """Claude Code's credential-lock pair for THIS config dir, in CC's own
    order — the same pair ``refresh._refresh_resolved`` holds around a
    profile write (``claude_locks`` derives the names from the config home:
    ``<home>/.oauth_refresh.lock`` then legacy ``<home>.lock``)."""
    return (
        proper_lockfile(
            session_dir / ".oauth_refresh.lock", staleness=CREDENTIALS_STALENESS_S
        ),
        proper_lockfile(
            session_dir.parent / (session_dir.name + ".lock"),
            staleness=CREDENTIALS_STALENESS_S,
        ),
    )


def _login_family_into_profile(profile_raw: str | None, backup: str) -> str:
    """The bytes to write: the backup's login family under the profile's OWN
    machine-shared fields. ``merge_shared_credential_fields`` lets the
    shared allowlist (``mcpOAuth`` …) of the second argument win, absence
    included — so the live session keeps the MCP forks it rotated itself
    (CON-1432) and gets only the ``claudeAiOauth`` generation replaced. A
    profile without a readable credential object contributes nothing: the
    backup goes in as-is (the CON-1432 parity re-seed fills holes later)."""
    profile_shared = shared_credential_fields(profile_raw)
    if profile_shared is None:
        return backup
    return merge_shared_credential_fields(backup, profile_shared)


def reseed_account(
    switcher: ClaudeAccountSwitcher, identifier: str
) -> ReseedReport:
    """Put the stored login's newer generation into the slot's session
    profile (see the module docstring for the law). Raises
    :class:`ReseedRefusal` when nothing is written; every other
    ``ClaudeSwitchError`` (unresolvable identifier) propagates as usual."""
    account_num, email, org_uuid = switcher.resolve_account(identifier)
    identity = {account_num: (email, org_uuid or "")}

    def refuse(outcome: str, message: str, pids: list[int] | None = None) -> ReseedRefusal:
        return ReseedRefusal(
            outcome, f"Account-{account_num} ({email}): {message}", live_pids=pids
        )

    def report(
        outcome: str, pids: list[int], generation: str | None, detail: str | None = None
    ) -> ReseedReport:
        return ReseedReport(
            account_num, email, outcome, detail, generation, list(pids)
        )

    if switcher._account_kind(account_num) == "api_key":
        raise refuse(API_KEY, "an API-key slot has no OAuth login family to reseed")
    session_dir = switcher._session_dir(account_num, email)
    if not session_dir.is_dir():
        raise refuse(
            NO_PROFILE,
            "nothing to reseed — the slot has no session profile on disk "
            f"(a `cswap run {account_num}` seeds one from the backup)",
        )
    if session_identity_drifted(session_dir, email, org_uuid):
        raise refuse(
            IDENTITY_DRIFTED,
            "the session profile is logged in as another account (an "
            "in-session /login re-pointed it) — not this slot's family to reseed",
        )

    try:
        with FileLock(switcher.lock_file, timeout=_RESEED_LOCK_TIMEOUT_S):
            # The ACTIVE slot is never reseeded from here (mirror of
            # `_refresh_resolved`, review r.1 Major 2): its live credential
            # is Claude Code's store, and the backup copy is a consumed
            # predecessor whenever CC rotated during normal use — POSTing
            # it is an invalid_grant strike on a LIVE login at best, and
            # copying it under a second writer forks the live family.
            # Judged under the lock, so a concurrent `cswap switch` (same
            # lock) cannot make this slot active mid-reseed.
            current = switcher._get_current_account()
            if current is not None:
                cur_email, cur_org = current
                if cur_email == email and (
                    not cur_org or not org_uuid or cur_org == org_uuid
                ):
                    raise refuse(
                        ACTIVE_SLOT,
                        "the slot is the active default login — its live "
                        "credential is Claude Code's own store and the stored "
                        "copy may be a consumed predecessor; the active login "
                        "heals through the collector's locked path, nothing to "
                        "reseed here",
                    )
            pids = switcher._live_session_pids(account_num, email)

            # Everything that reads or writes the profile runs under Claude
            # Code's credential-lock pair for THIS config dir: the profile's
            # own claude mid-refresh is excluded, and the bytes the
            # shared-fields merge writes back are the ones it holds NOW
            # (review r.1, Minor 3).
            lock_a, lock_b = _profile_locks(session_dir)
            with lock_a, lock_b:
                # ONE read of the profile's newest generation feeds the
                # token gate, the ordering judge and the shared-fields
                # merge (CON-1740, review r.1: two reads let a Keychain
                # timeout skip one guard and pass another).
                profile_read = read_profile_generation(session_dir)
                profile_raw, profile_err = profile_read
                if profile_raw is None and profile_err is not None:
                    raise refuse(
                        DEFERRED,
                        f"the profile's own credential cannot be read right "
                        f"now ({profile_err}) — who holds the newer generation "
                        "is undecidable; not overwriting what may be the "
                        "family's newest. Retry shortly (CON-1740)",
                        pids,
                    )
                if profile_raw and is_inference_token_credentials(profile_raw):
                    raise refuse(
                        TOKEN_PROFILE,
                        "the profile runs on an inference token, its login "
                        "family is the backup (CON-1329) — there is no drift "
                        "to reseed",
                        pids,
                    )
                # The backup is read AFTER the profile on purpose: on macOS
                # both live in the Keychain, and the backup reader answers a
                # Keychain timeout with "" (credentials.py) — judged first it
                # would turn a busy Keychain into a false "no credentials".
                # A pending spilled rotation is the backup family's newest
                # generation (CON-849): folded in before judging the disk
                # (its backup-write hook lands under these locks too).
                backup = switcher.read_account_credentials(account_num, email)
                backup = switcher.reconcile_pending_rotation_locked(
                    account_num, email, backup
                )
                backup_oauth = _full_pair(backup)
                if backup_oauth is None:
                    raise refuse(
                        NO_CREDENTIALS,
                        "the stored backup holds no full token pair — re-add "
                        f"the slot after logging in: cswap add --slot {account_num}",
                        pids,
                    )
                fp_backup = oauth.credential_fingerprint(backup)
                seed = read_seed_fingerprint(session_dir)
                profile_oauth = _full_pair(profile_raw)
                fp_profile = (
                    oauth.credential_fingerprint(profile_raw)
                    if profile_raw and profile_oauth is not None
                    else None
                )

                if fp_profile == fp_backup:
                    return report(
                        IN_SYNC, pids, fp_profile,
                        "profile and backup hold the same generation",
                    )

                if profile_oauth is not None:
                    if seed is None:
                        raise refuse(
                            UNDECIDABLE,
                            "the profile holds a different generation and "
                            "carries no seed stamp — who ran ahead cannot be "
                            "told; not overwriting a generation that may be "
                            f"the newest (idle: cswap refresh {account_num})",
                            pids,
                        )
                    if seed == fp_backup:
                        # Backup unmoved since seeding: the profile rotated
                        # ahead and owns the newest generation. Nothing is
                        # written into its store; the backup adopts that
                        # generation (`adopt_profile_family` — no POST;
                        # re-stamps the seed — the bootstrap's own stamp-only
                        # judge). The backup-write hook marks a LIVE profile
                        # stale, or drops an IDLE profile's copy (one family,
                        # one copy). After the adoption backup == profile, so
                        # a marker would lie (review r.1 of #32) — dropped.
                        switcher.adopt_profile_family(
                            account_num, email, org_uuid, locked=True,
                            profile_read=profile_read,
                        )
                        (session_dir / STALE_MARKER).unlink(missing_ok=True)
                        # An idle profile that lost its copy must not keep
                        # the seed stamp the adoption re-wrote: a stamp over
                        # no credential reads as "the backup is the profile's
                        # consumed seed" to the collector's seed guard,
                        # `cswap refresh` and this door — freezing the
                        # freshly adopted backup (review r.1, Major 1; the
                        # invariant `_invalidate_session_credentials` keeps
                        # for its own callers).
                        dropped = read_profile_generation(session_dir) == (None, None)
                        if dropped:
                            (session_dir / SEED_FINGERPRINT_FILE).unlink(
                                missing_ok=True
                            )
                        return report(
                            PROFILE_AHEAD, pids, fp_profile,
                            _profile_ahead_detail(
                                account_num, profile_oauth, pids, dropped
                            ),
                        )
                    # seed != backup: the backup moved after the profile was
                    # seeded — the profile's copy is the consumed predecessor.
                elif seed and seed == fp_backup:
                    # No usable pair in the profile (claude's invalid_grant
                    # wipe, or nothing at all) over a backup that still
                    # equals the generation the profile was seeded with: the
                    # profile's family rotated past it and died — the backup
                    # is the consumed predecessor, and replaying a consumed
                    # grant is the documented reuse signal (CON-849).
                    raise refuse(
                        RELOGIN_REQUIRED,
                        "the profile holds no usable login and the stored "
                        "backup is the consumed seed generation the profile "
                        "rotated past — only a re-login helps: "
                        f"cswap-relogin.sh {account_num} (or `cswap add --slot "
                        f"{account_num}` after logging in with {email})",
                        pids,
                    )

                # The backup is the newer generation. Never reseed a lineage
                # the store already condemned (same parole rule as the
                # collector, `cswap refresh` and the bootstrap).
                entry = switcher._usage_store.entries(identity)[account_num]
                if entry.token_dead() and not switcher._parole_eligible(
                    entry, backup
                ):
                    raise refuse(
                        RELOGIN_REQUIRED,
                        "the stored login's lineage is condemned (the token "
                        "endpoint already answered invalid_grant for this "
                        "generation) — not seeding a profile with it. Re-login "
                        f"and re-add the slot: cswap-relogin.sh {account_num}",
                        pids,
                    )

                working = backup
                if oauth.is_oauth_token_expired(backup_oauth.get("expiresAt")):
                    # Proof of life before the write: one POST with the
                    # BACKUP's grant — the family's newest, held by no live
                    # process (the profile's is the consumed predecessor).
                    # Inside the profile locks: a live claude mid-refresh
                    # re-reads the store under them and adopts the successor
                    # instead of POSTing its own (consumed) grant.
                    result = oauth.try_refresh_oauth_credentials(
                        backup, timeout_s=_RESEED_POST_TIMEOUT_S
                    )
                    if result.error in ("invalid_grant", "no_refresh_token"):
                        switcher._usage_store.record(
                            {
                                account_num: FetchRecord(
                                    error=result.error,
                                    credential_fingerprint=fp_backup,
                                )
                            },
                            identity,
                        )
                        raise refuse(
                            RELOGIN_REQUIRED,
                            "the stored login is expired and the token "
                            "endpoint rejected its refresh grant "
                            f"({result.error}) — the profile is left as it is; "
                            "only a re-login helps: cswap-relogin.sh "
                            f"{account_num}",
                            pids,
                        )
                    if result.error is not None or not result.credentials:
                        raise refuse(
                            TRANSIENT_ERROR,
                            "the stored login is expired and its refresh "
                            "failed transiently "
                            f"({result.error or 'empty refresh result'}) — "
                            "nothing written; retry",
                            pids,
                        )
                    working = result.credentials
                    # The grant is consumed: the successor MUST survive —
                    # backup first (write_account_credentials expects the
                    # held account lock), the profile next.
                    switcher.write_account_credentials(account_num, email, working)
                    switcher._usage_store.clear_dead_token([account_num], identity)
                _reseed_profile(
                    session_dir, _login_family_into_profile(profile_raw, working)
                )
                # Backup and profile are one generation again: the deferred
                # re-bootstrap the marker asks for has nothing left to do.
                (session_dir / STALE_MARKER).unlink(missing_ok=True)
    except LockError as e:
        raise ReseedRefusal(
            DEFERRED,
            f"Account-{account_num} ({email}): credential locks held elsewhere "
            f"({e}) — a claude on this profile or another cswap operation is "
            "mid-write; retry shortly",
        ) from e

    switcher._logger.info(
        f"Reseeded the session profile of account {account_num} from the "
        f"stored login (generation {oauth.credential_fingerprint(working)}; "
        f"live sessions: {pids or 'none'})"
    )
    return report(RESEEDED, pids, oauth.credential_fingerprint(working))



def _profile_ahead_detail(
    account_num: str, profile_oauth: dict, pids: list[int], dropped: bool
) -> str:
    """Human detail for PROFILE_AHEAD: what happened to the backup and to
    the profile copy, and who refreshes an expired generation."""
    detail = "the profile rotated past the backup; backup adopted its generation"
    if dropped:
        return detail + (
            " — the idle profile copy was dropped by the backup-write hook "
            f"(one family, one copy); the next `cswap run {account_num}` "
            "seeds it back"
        )
    if oauth.is_oauth_token_expired(profile_oauth.get("expiresAt")):
        if pids:
            return detail + (
                " — its access token is expired: a live session refreshes it "
                "on its own 401 path"
            )
        return detail + f" — its access token is expired: cswap refresh {account_num}"
    return detail
