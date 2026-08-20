#!/bin/bash
# deploy.sh — the ONLY door for deploying this package to the fleet machine
# (CON-954). A bare `uv tool install` is a half-rollout: on 2026-08-18 the
# CON-849 credential fix was installed but the cswap-auto daemon kept running
# pre-fix code for 2 days and 12 fleet slots lost their refresh tokens.
#
# The door makes a half-rollout impossible:
#   1. inventory long-lived cswap processes (`cswap auto` / `cswap watch`;
#      `cswap run` execvpe-replaces itself with claude and never stays);
#      an UNKNOWN long-lived cswap process stops the door BEFORE install —
#      installing first would leave it unrestartable stale code;
#   2. install: uv tool install --force git+https://github.com/danpoyar/cswap@<ref>;
#   3. restart every long-lived process:
#        cswap auto  — launchctl kickstart -k gui/<uid>/com.amouen.cswap-auto
#                      (the launchd job owns the daemon);
#        cswap watch — SIGTERM; the hub accounts pane runs it in an eternal
#                      `while true` loop that respawns it on the new code;
#   4. verify: every long-lived cswap process now running must have started
#      AT or AFTER the install moment (mtime of uv-receipt.toml), and every
#      class present before the deploy must be present again. Anything else
#      is DEPLOY-INCOMPLETE with rc≠0 — the scream the 18-08 rollout lacked.
#
# The fleet-sensors job on the machine (config repo, sensor G) watches the
# same invariant continuously and writes DEPLOY-SYNC lines when a process
# predates the installed package.
#
# Test seams (tests/test_deploy_door.sh and the config repo's
# cswap-deploy-sync-guard.test.sh): CSD_RECEIPT, CSD_INSTALL_CMD,
# CSD_PROC_LIST_CMD (line contract: pid<TAB>start_epoch<TAB>command),
# CSD_RESTART_AUTO_CMD, CSD_RESTART_WATCH_CMD (pid appended as an argument),
# CSD_WAIT_S, CSD_POLL_S, CSD_LAUNCHD_LABEL.
# Exit: 0 — DEPLOY-OK; 1 — the deploy did not complete (nothing installed,
# or stale/missing processes remain) — never trust rc≠0 silently.
set -u

REF="${1:-main}"
RECEIPT="${CSD_RECEIPT:-$HOME/.local/share/uv/tools/claude-swap/uv-receipt.toml}"
LABEL="${CSD_LAUNCHD_LABEL:-com.amouen.cswap-auto}"
WAIT_S="${CSD_WAIT_S:-30}"
POLL_S="${CSD_POLL_S:-1}"

say() { printf 'deploy: %s\n' "$*"; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }

mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null; }

# One inventory pass. Output lines: pid<TAB>start_epoch<TAB>command.
# start_epoch may be the literal `x` when unreadable (judged by the caller).
list_procs() {
  if [ -n "${CSD_PROC_LIST_CMD:-}" ]; then
    # shellcheck disable=SC2086  # a mock command with arguments splits on purpose
    $CSD_PROC_LIST_CMD 2>/dev/null || true
    return
  fi
  LC_ALL=C ps -axo pid=,command= 2>/dev/null \
    | grep -E '[c]swap (auto|watch)( |$)' \
    | while read -r pid cmd; do
        [ -n "$pid" ] || continue
        ls_out="$(LC_ALL=C ps -p "$pid" -o lstart= 2>/dev/null)"
        se=""
        if [ -n "$ls_out" ]; then
          se="$(LC_ALL=C date -j -f '%a %b %d %T %Y' "$ls_out" +%s 2>/dev/null \
                || LC_ALL=C date -d "$ls_out" +%s 2>/dev/null)"
        fi
        printf '%s\t%s\t%s\n' "$pid" "${se:-x}" "$cmd"
      done
}

# Class of one inventory line: auto / watch / unknown.
proc_class() {
  case "$1" in
    *"cswap auto"*)  echo auto ;;
    *"cswap watch"*) echo watch ;;
    *)               echo unknown ;;
  esac
}

# ---------- 1: pre-install inventory ----------
PRE="$(list_procs)"
HAD_AUTO=0
HAD_WATCH=0
WATCH_PIDS=""
UNKNOWN=""
while IFS=$'\t' read -r pid se cmd; do
  [ -n "$pid" ] || continue
  case "$(proc_class "$cmd")" in
    auto)    HAD_AUTO=1 ;;
    watch)   HAD_WATCH=1; WATCH_PIDS="$WATCH_PIDS $pid" ;;
    unknown) UNKNOWN="$UNKNOWN pid $pid ($cmd);" ;;
  esac
done <<EOF
$PRE
EOF
if [ -n "$UNKNOWN" ]; then
  die "STOP before install: unknown long-lived cswap process(es) the door cannot restart —$UNKNOWN teach scripts/deploy.sh to restart them first, otherwise the install would strand them on pre-deploy code (the CON-954 half-rollout class)"
fi
say "inventory: auto=$HAD_AUTO watch=$HAD_WATCH${WATCH_PIDS:+ (watch pids:$WATCH_PIDS)}"

# ---------- 2: install ----------
if [ -n "${CSD_INSTALL_CMD:-}" ]; then
  # shellcheck disable=SC2086  # a mock command with arguments splits on purpose
  $CSD_INSTALL_CMD || die "install failed (mock rc≠0) — nothing restarted, nothing verified"
else
  say "installing git+https://github.com/danpoyar/cswap@$REF (uv tool install --force)"
  uv tool install --force "git+https://github.com/danpoyar/cswap@$REF" \
    || die "uv tool install failed — nothing restarted; the running processes still serve the OLD (consistent) code"
fi
DEPLOY_EPOCH="$(mtime "$RECEIPT")"
case "${DEPLOY_EPOCH:-x}" in
  *[!0-9]*|'') die "install ran but the receipt is unreadable ($RECEIPT) — cannot verify the rollout, treat as incomplete" ;;
esac
say "installed: receipt mtime $DEPLOY_EPOCH ($RECEIPT)"

# ---------- 3: restart every long-lived process ----------
if [ "$HAD_AUTO" = 1 ]; then
  if [ -n "${CSD_RESTART_AUTO_CMD:-}" ]; then
    # shellcheck disable=SC2086
    $CSD_RESTART_AUTO_CMD || say "WARN: auto restart command rc≠0 — verify below will catch a stale daemon"
  else
    launchctl kickstart -k "gui/$(id -u)/$LABEL" \
      || say "WARN: launchctl kickstart $LABEL rc≠0 — verify below will catch a stale daemon"
  fi
  say "restarted: cswap auto (launchd $LABEL)"
fi
if [ "$HAD_WATCH" = 1 ]; then
  for wp in $WATCH_PIDS; do
    if [ -n "${CSD_RESTART_WATCH_CMD:-}" ]; then
      # shellcheck disable=SC2086
      $CSD_RESTART_WATCH_CMD "$wp" || say "WARN: watch restart command rc≠0 for pid $wp"
    else
      kill -TERM "$wp" 2>/dev/null || say "WARN: TERM to cswap watch pid $wp failed"
    fi
    say "restarted: cswap watch pid $wp (the hub accounts loop respawns it)"
  done
fi

# ---------- 4: verify — no process may predate the install ----------
DEADLINE=$(( $(date +%s) + WAIT_S ))
while :; do
  STALE=""
  MISSING=""
  SEEN_AUTO=0
  SEEN_WATCH=0
  CUR="$(list_procs)"
  while IFS=$'\t' read -r pid se cmd; do
    [ -n "$pid" ] || continue
    case "$(proc_class "$cmd")" in
      auto)  SEEN_AUTO=1 ;;
      watch) SEEN_WATCH=1 ;;
    esac
    case "$se" in
      ''|*[!0-9]*) STALE="$STALE pid $pid (start unreadable: $cmd);" ;;
      *) [ "$se" -ge "$DEPLOY_EPOCH" ] || STALE="$STALE pid $pid (started $se < deploy $DEPLOY_EPOCH: $cmd);" ;;
    esac
  done <<EOF
$CUR
EOF
  [ "$HAD_AUTO" = 1 ] && [ "$SEEN_AUTO" = 0 ] && MISSING="$MISSING cswap auto (launchd $LABEL did not bring it back);"
  [ "$HAD_WATCH" = 1 ] && [ "$SEEN_WATCH" = 0 ] && MISSING="$MISSING cswap watch (hub accounts loop did not respawn it);"
  if [ -z "$STALE" ] && [ -z "$MISSING" ]; then
    say "DEPLOY-OK: package installed and every long-lived cswap process runs code from this deploy (start ≥ receipt mtime $DEPLOY_EPOCH)"
    exit 0
  fi
  [ "$(date +%s)" -ge "$DEADLINE" ] && break
  sleep "$POLL_S"
done
die "DEPLOY-INCOMPLETE: after ${WAIT_S}s —${STALE:+ stale:$STALE}${MISSING:+ missing:$MISSING} the rollout is HALF-DONE (CON-954 class): restart the listed processes and re-run the verify (re-run this door with the same ref is safe)"
