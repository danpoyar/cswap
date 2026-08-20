#!/bin/bash
# test_deploy_door.sh — hermetic suite for scripts/deploy.sh (CON-954).
# The door exists because a bare `uv tool install` is a half-rollout: the
# daemon kept running pre-fix code for 2 days and 12 fleet slots died.
# Every case runs on mocks (no uv, no launchctl, no real processes), so it
# is green on macOS and on the ubuntu CI runner alike:
#   1. deploy WITHOUT an effective restart screams DEPLOY-INCOMPLETE, rc≠0;
#   2. a full deploy restarts auto (once) and watch (per pid) and ends
#      DEPLOY-OK, rc=0;
#   3. a failed install restarts nothing and fails loudly;
#   4. an unknown long-lived cswap process stops the door BEFORE install.
set -u
SDIR="$(cd "$(dirname "$0")/.." && pwd)"
DOOR="$SDIR/scripts/deploy.sh"
fail() { echo "FAIL: $1"; exit 1; }
[ -f "$DOOR" ] || fail "scripts/deploy.sh not found: $DOOR"

TD="$(mktemp -d)" || fail "mktemp -d failed"
trap 'rm -rf "$TD"' EXIT
NOW="$(date +%s)"

reset_case() {
  rm -f "$TD/receipt.toml" "$TD/procs.txt" "$TD/restarts.log" "$TD/install.log"
  export CSD_RECEIPT="$TD/receipt.toml"
  export CSD_INSTALL_CMD="$TD/install.sh"
  export CSD_PROC_LIST_CMD="cat $TD/procs.txt"
  export CSD_RESTART_AUTO_CMD="$TD/restart-noop.sh auto"
  export CSD_RESTART_WATCH_CMD="$TD/restart-noop.sh watch"
  export CSD_WAIT_S=3
  export CSD_POLL_S=1
  cat > "$TD/install.sh" <<EOF
#!/bin/bash
echo install >> "$TD/install.log"
touch "$TD/receipt.toml"
EOF
  cat > "$TD/restart-noop.sh" <<EOF
#!/bin/bash
printf '%s %s\n' "\$1" "\${2:-}" >> "$TD/restarts.log"
EOF
  cat > "$TD/restart-effective.sh" <<EOF
#!/bin/bash
printf '%s %s\n' "\$1" "\${2:-}" >> "$TD/restarts.log"
python3 - "$TD/procs.txt" <<'PY'
import sys, time
p = sys.argv[1]
rows = [l.split("\t") for l in open(p).read().splitlines() if l]
now = str(int(time.time()) + 5)
open(p, "w").write("".join("%s\t%s\t%s\n" % (r[0], now, r[2]) for r in rows))
PY
EOF
  chmod +x "$TD/install.sh" "$TD/restart-noop.sh" "$TD/restart-effective.sh"
  { printf '4242\t%s\tcswap auto --interval 30 --json\n' $(( NOW - 7200 ))
    printf '5151\t%s\tcswap watch\n' $(( NOW - 9000 )); } > "$TD/procs.txt"
}
run_door() { OUT="$(/bin/bash "$DOOR" 2>&1)"; RC=$?; }
N=0
ok() { N=$((N+1)); echo "OK $N: $1"; }

# ---------- 1: restarts are no-ops, processes stay stale -> scream ----------
reset_case
run_door
[ "$RC" != 0 ] || fail "case 1: a half-rollout passed silently (rc=0): $OUT"
printf '%s' "$OUT" | grep -q 'DEPLOY-INCOMPLETE' || fail "case 1: no DEPLOY-INCOMPLETE scream: $OUT"
printf '%s' "$OUT" | grep -q '4242' || fail "case 1: the scream does not name the stale pid: $OUT"
grep -q install "$TD/install.log" || fail "case 1: install was never called"
ok "deploy without restart screams DEPLOY-INCOMPLETE with rc≠0"

# ---------- 2: effective restarts -> DEPLOY-OK ----------
reset_case
export CSD_RESTART_AUTO_CMD="$TD/restart-effective.sh auto"
export CSD_RESTART_WATCH_CMD="$TD/restart-effective.sh watch"
run_door
[ "$RC" = 0 ] || fail "case 2: rc=$RC ($OUT)"
printf '%s' "$OUT" | grep -q 'DEPLOY-OK' || fail "case 2: no DEPLOY-OK: $OUT"
grep -q '^auto' "$TD/restarts.log" || fail "case 2: auto restart not invoked"
grep -q '^watch 5151' "$TD/restarts.log" || fail "case 2: watch restart not invoked with its pid"
ok "full deploy restarts auto and watch, verify passes, DEPLOY-OK"

# ---------- 3: failed install -> loud rc≠0, nothing restarted ----------
reset_case
printf '#!/bin/bash\nexit 1\n' > "$TD/install.sh"
chmod +x "$TD/install.sh"
run_door
[ "$RC" != 0 ] || fail "case 3: failed install passed silently: $OUT"
[ -f "$TD/restarts.log" ] && fail "case 3: restarts ran after a failed install"
ok "failed install: loud rc≠0, nothing restarted"

# ---------- 4: unknown long-lived cswap process -> stop BEFORE install ----------
reset_case
printf '6363\t%s\tcswap daemon-of-the-future\n' $(( NOW - 100 )) >> "$TD/procs.txt"
run_door
[ "$RC" != 0 ] || fail "case 4: unknown long-lived process passed silently: $OUT"
printf '%s' "$OUT" | grep -q '6363' || fail "case 4: the stop does not name the unknown pid: $OUT"
[ -f "$TD/install.log" ] && fail "case 4: install ran with an unrestartable process present"
ok "unknown long-lived process stops the door before install"

echo "=== test_deploy_door.sh: all $N cases green ==="
