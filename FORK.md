# Why this fork exists

`danpoyar/cswap` is a fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap)
(MIT). Upstream is good software — 17k lines, 1600 tests, and a rate-limit
model measured against the real API. This fork exists so the fleet it manages
(18 Claude accounts, ~$3.4k/mo) does not depend on somebody else's release
schedule, and so the Quota menu-bar app can rely on a JSON contract we own.

Nothing here is a rewrite. The rule is: stay a thin layer over upstream, send
anything generally useful back, and keep the divergence list short enough to
read in one sitting.

## What diverges from upstream

- `tests/conftest.py` — the two safety-net fixtures (`_isolate_real_home`,
  `block_real_keychain`) patch through their own `MonkeyPatch` instead of the
  shared fixture. A test calling `monkeypatch.undo()` to drop its injected
  failure used to revert the isolation too, so everything after that line ran
  against the developer's REAL `$HOME` and REAL macOS Keychain. That is why
  six swap/move rollback tests failed on macOS and passed on Linux CI: they
  were querying a Keychain that had never heard of their fixture accounts.
  Fix is one place; both fixtures keep their behaviour. Worth sending upstream.
- `tests/test_move_accounts.py::test_move_strict_clear_fails_closed_on_unreadable_dir`
  — `xfail` on macOS. Real behaviour gap, not a harness artifact: the
  credential lives in the Keychain, so the best-effort sweep inside the
  required clear removes it before the unreadable-directory unlink aborts the
  move. The abort still holds (nothing is committed), but the stale foreign
  credential does not survive it the way it does on Linux. Left alone on
  purpose: `move` is outside the fleet law this fork is maintained for, and
  the fix belongs upstream where the ordering was designed.

- Quiet gate on voluntary switches (`autoswitch.py`): proactive/consume-first
  switches are held until no `~/.claude/projects/**/*.jsonl` transcript has
  been written for 5 minutes (`QUIET_WINDOW_S`), because prompt caches are
  per-organization and a swap under live traffic full-misses the next turn of
  every running session (measured on this fleet: 47/64 FULL-MISS turns within
  ±2 min of a switch). At-limit/failover bypass the gate. Every `switch` event
  carries an additive `gate: "quiet"|"forced"` field = traffic state at swap
  time, so cache damage per switch is measurable from the log. Fleet policy
  on this machine (settings.json, not code): threshold 95, cooldownSeconds
  7200. Upstream may want this too, but the transcript-path heuristic is
  Claude-Code-layout-specific — offer upstream after it survives the fleet.
- `list --json` also reports `lastError` + `consecutiveFailures` on a slot
  with an open failure streak (`json_output.fetch_failure_fields`). Upstream
  exposes the last-good measurement but not WHY it stopped moving, so a
  consumer cannot tell an account parked at its limit from one whose token
  died — both serve the same quiet numbers, and stale zeroes read like a free
  account. This is what lets Quota render "http-429 · 5d ago" without reading
  cswap's private store file. Worth sending upstream.
- Dead-token quarantine condemns a credential GENERATION, not the slot:
  each permanent-auth strike stamps `deadTokenFingerprint` in the usage
  store, and the collector paroles a quarantined slot whose candidate
  credential (live store for the active slot, backup for a parked one) no
  longer matches the condemned lineage — one live probe per new generation,
  run with the stamp intact (reserve's parole override). A probe's outcome
  re-stamps the row: a POST death condemns the lineage actually consumed
  (the outcome names it — it may be a successor the chain rotated to), and
  the active path refuses to POST an already-condemned grant outright,
  condemning instead the candidate that justified the parole. Either way
  the same bytes are never POSTed twice and the flow converges. Before
  this, only a manual `cswap add` / `add-token` /
  `import` could lift the quarantine: a user who re-logged in with Claude
  Code still saw "re-login needed" forever, because the quarantine also
  blocked the very fetch whose resync machinery (ef27749) would have adopted
  the rotated credential. Live incident on this fleet, 2026-08-03: slot #5's
  backup lineage died 07-29 (rotation-before-collection), and the panel kept
  demanding a re-login while the account was running live sessions the whole
  time. Mirrors the autoswitch engine's own fingerprint-keyed quarantine
  release (`_release_recovered_quarantines`). Worth sending upstream.
- `oauth.account_headroom`: a *configured* per-model window (`autoswitch.model`)
  that an account's usage does not report yields headroom `None` (unknown,
  never an autoswitch target) instead of silently computing from the remaining
  5h/7d windows. 2026-08-02 incident on this fleet: an account answering with
  healthy 5h/7d but no `scoped` list at all read as 92% free and won the
  at-limit escape (`lastSwitchTo: 18` in the state file) — landing every live
  session on unverified Fable access. The `all` sentinel names no particular
  window and is exempt. Fail-safe corollary: an inert/typo model name now
  blocks `best`/autoswitch decisions entirely (with the existing
  config-warning explaining why) instead of being silently ignored. Worth
  sending upstream.

## Syncing with upstream

```
git fetch upstream
git log --oneline main..upstream/main        # read what changed first
git merge upstream/main                      # or rebase our commits on top
uv run --group dev pytest -q                 # must be green before installing
```

Then reinstall (below) and re-run Quota's own checks: `scripts/build-app.sh`
in ~/projects/quota runs two live guards against this binary's JSON.

## How it is installed

```
uv tool install --force git+https://github.com/danpoyar/cswap@main
```

Same package name (`claude-swap`) and the same two entrypoints (`cswap`,
`claude-swap`), so `~/bin/cswap`, the launchd job `com.amouen.cswap-auto` and
Quota keep working untouched. Rollback is one command — `uv tool install --force claude-swap==0.22.0` — but
know what it costs: no PUBLISHED upstream release carries `lastGoodUsage`
(it landed on main on 2026-07-28), so Quota loses every stored reading and
shows "No data" for each parked slot. It says so in the footer rather than
going quiet, and the way back is this fork. To roll back only OUR commits,
install an earlier ref of this repo instead: `…@<sha>`.

State lives outside the install and is never touched by reinstalling:
`~/.claude-swap-backup/` (sequence, usage store, autoswitch state) and the
macOS Keychain items (`claude-swap` service, `account-<n>-<email>`).

## The fleet law this fork serves

Quota (`~/projects/quota`) drives cswap under a written law — allowed
commands, no writes to Anthropic ever, `remove` only with a clearance. See
`~/projects/quota/CLAUDE.md`. Changes here must not widen what that app can
do by accident.
