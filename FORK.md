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
  ±2 min of a switch). `autoswitch.switchUnderLoad` releases the at-threshold
  (proactive) switch from that gate: an unattended fleet never goes quiet, so
  the gate held the swap until the account hit the wall and in-flight agents
  died on the limit first. Forced switches (failover, and proactive under
  `switchUnderLoad`) instead *drain* (CON-419): with
  `autoswitch.drainTimeoutSeconds` set they wait — re-checked each tick — for
  the same silence, and at the ceiling swap anyway after a one-per-episode
  `drain-timeout` WARN. Waiting ticks log `no-switch`/`drain-wait`, a drained
  switch carries an additive `drain: {outcome, waitedSeconds}`, and the
  episode persists in `autoswitch_state.json` (shared with cron `--once`).
  An at-limit switch skips the drain outright (CON-486): at-limit means the
  binding window is at 100%, every call on the account is already failing,
  so the silence the drain waits for only measures how long the dying takes
  (live episode 2026-08-14: 417s of drain-wait behind one background
  reviewer that burned itself to the session limit on a dead account).
  Every `switch` event carries an additive `gate: "quiet"|"forced"` field =
  traffic state at swap time, so cache damage per switch is measurable from
  the log. Fleet policy on this machine (settings.json, not code): threshold
  90, cooldownSeconds 7200, strategy consume-first, model Fable,
  switchUnderLoad true, drainTimeoutSeconds 600. Upstream may want this too,
  but the transcript-path heuristic is Claude-Code-layout-specific — offer
  upstream after it survives the fleet.
- Drain v2 — the active checkpoint (CON-433): with
  `autoswitch.drain2WaitSeconds` set, the forced proactive switch CREATES
  the park pause instead of waiting for one. The engine signals every
  mid-turn background session to checkpoint and freeze — the wave is
  delivered by a one-shot headless `claude -p` *herald*, listed in the
  session roster as `Jerry` (explicit `--name`; CON-464), using its
  SendMessage tool, because a raw daemon write to a session's inbox socket
  asserts no permission class and bypass-mode receivers hold it unread
  (cross-session-messaging inbound rules) — then confirms fixation
  machine-wise from `claude agents --json` (`status` busy→idle = turn
  boundary; sessions of per-terminal `cswap run` profiles excluded by pid,
  their credentials don't swap), swaps, verifies the new account answers
  (usage fetch, ≤2 attempts), and wakes the frozen sessions: a message to
  an idle session starts its next turn, so each agent pays exactly one cold
  cache write on the new account. Sessions still mid-turn at the cap are
  forced and honestly counted in the switch event's additive
  `drain2: {outcome: "ready"|"timeout", waitedSeconds, fixed, forced}`.
  Events: `drain2-signal`, `no-switch`/`drain2-wait`, one `drain2-timeout`
  WARN per episode, `drain2-verify`, `drain2-resume`; any channel failure
  emits `drain2-unavailable` and falls back to the passive v1 drain above
  (with a 10-minute in-process backoff). The episode persists in
  `autoswitch_state.json` (phases signaled→swapped), so the resume wave
  survives a daemon restart; a frozen session self-rescues after 10 minutes
  if the resume never arrives (the STOP text says so). At-limit/failover
  keep the passive drain — calls are already failing there, minutes of
  orderly pause is time the park doesn't have. `0` (the default) keeps v2
  off and v1 behavior bit-for-bit. Fleet enablement:
  `cswap config set autoswitch.drain2WaitSeconds 180`. Delivery
  precondition (cross-session-messaging inbound rules): the herald and the
  receiving sessions must sit in the same permission class — this machine
  pins `permissions.defaultMode: bypassPermissions` in ~/.claude/settings.json,
  so the plain `claude -p` herald inherits bypass and bypass→bypass
  delivers; in a mixed-class fleet the waves are held unread and every
  episode ends in `timeout` with all-`forced` counts. Before enabling,
  prove the channel with one live wave (herald → a busy bypass background
  session: message delivered, not held). Claude-Code-specific by
  construction (roster + messaging surfaces); not upstream material.
- `add` registers without activating (CON-438): upstream's `add` records the
  freshly captured login as the active account — but `add` runs right after a
  `claude /login` that already replaced the live credential, so on a fleet it
  IS an account swap under every running session, outside both drain paths
  above (live episode 2026-08-14: drain of #27 in progress, `cswap add` made
  fresh #29 active with no `switch` event; ~10 agents cold-started their
  prompt cache at once). Now `add` snapshots the new login into its slot and
  writes the recorded active account's stored login back over the live one
  (same lock set and oauthAccount splice as a switch; no stash needed — the
  displaced credential was just backed up into its slot), leaving
  `activeAccountNumber` untouched; the new slot waits as a rotation
  candidate. Re-login to an already-managed account keeps the same rule. The
  fresh login still becomes active when there is nothing to protect (first
  account, re-add of the active account itself) or nothing to restore
  (unreadable backup — honest state wins, with a warning), and
  `cswap add --activate` is the conscious immediate swap, logged as a drain
  bypass. Upstream-relevant in spirit, but the motivation is fleet-shaped;
  offer after it survives here.
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
  longer matches the condemned lineage — probes run with the stamp intact
  (reserve's parole override skips only the quarantine gate; the failure
  backoff still paces them, so a transiently-failed probe retries on the
  store's normal cadence, not per pass). A probe's permanent death
  re-stamps the row: a POST death condemns the lineage actually consumed
  (the outcome names it — it may be a successor the chain rotated to), and
  the active path refuses to POST an already-condemned grant outright,
  condemning instead the candidate that justified the parole. Either way
  condemned bytes are never POSTed again and the flow converges. Before
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

- Session profiles share claude's session registry: `<profile>/sessions/`
  is a symlink to `~/.claude/sessions` (POSIX only; a pre-existing private
  registry is migrated with live registrations preserved, dead-PID leftovers
  dropped). `CLAUDE_CONFIG_DIR` relocates the registry together with the
  credentials and no documented mechanism splits them (checked against the
  full env-var list, claude 2.1.231), so an isolated profile is invisible
  to `claude agents`/ListAgents everywhere else and blind to the machine.
  Live incident on this fleet, 2026-08-13: the orchestrator session ran in
  a profile and reported "the park is empty" off its one-entry roster while
  12 sessions were live (CON-340). Profile-scoped liveness guards (stale
  invalidation, history migration, remove/purge refusal) keep their meaning
  by asking the process environment (`ps -wwE` / `/proc`) which sessions
  actually run with `CLAUDE_CONFIG_DIR` pointing at the profile, falling
  back to the whole roster when the probe can't answer. Worth sending
  upstream.

- Pool-shield under `consume-first` (`autoswitch.py`, CON-712): with a
  configured `autoswitch.model`, voluntary decisions judge the account-wide
  5h/7d axis — the below-threshold/early trigger comparison uses it, and
  voluntary landings prefer model-burned hosts (model window past the
  threshold, account itself healthy) over model-fresh ones; a rescue move
  off a model-fresh active onto a burned host skips reset ordering and the
  early-swap hysteresis, and the reverse trade (burned host → fresh) is
  refused below the threshold. The model window still binds the at-limit
  escape and every escape landing. Without this, the rotation hoarded the
  fleet's scarcest resource: on 2026-08-16 the engine held account 36
  (Fable 60%, 5h 0%) as the active host for a whole working day — every
  other candidate read "unhealthy" through the Fable lens — while the slot
  pool the bg fleet feeds from ran dry of Fable-fresh accounts. Fleet-shaped
  policy; offer upstream after it survives the fleet.

- `switch` heals or refuses a slot whose session profile rotated past the
  stored backup (CON-1579). A `cswap run` session's claude rotates the token
  family inside its profile and nothing syncs it back, so the slot backup is
  a CONSUMED generation from the first rotation on — and `switch` activated
  exactly that backup. Live incident on this fleet, 2026-08-31 09:53–09:56:
  after a reboot the operator switched by hand onto three slots with live
  agents; each landed dead ("Login expired · Please run /login" on the first
  request), Claude Code wiped the live token fields, and only a browser
  `/login` recovered the terminal. The pre-activation heal
  (`refresh.heal_backup_before_activation`, wired into `_perform_switch`
  before the locks) adopts the profile's fresh generation into the backup
  without a POST, refreshes an expired one through the parked-slot refresh
  path, drops the idle profile's copy either way (one family, one live
  copy), and REFUSES when a live session owns the family (recipe:
  `cswap run N`) or the grant is rejected (recipe: `cswap add --slot N`) —
  the old warn-and-proceed drift notice stays only for the equal-lineage
  case. Fingerprint inequality alone does not say who ran ahead: the heal
  reads the two ordering oracles first — the profile's stale marker (set by
  `_post_backup_write` when the backup was rewritten under a live session:
  re-login, re-add, persisted rotation) and the seed stamp (backup moved
  after seeding) — and in both cases the BACKUP is the newer login: it is
  activated as-is and the superseded profile copy is dropped when idle
  (review r.1 of the fix caught the one-sided reading). `cswap refresh --all` never healed this shape on purpose: it judges
  the profile's freshness, and a fresh profile over a dead backup is FRESH.
  Fleet-shaped (profiles are this fork's session mode); offer upstream with
  the session-mode work.

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
scripts/deploy.sh [ref]        # default ref: main
```

The door is the ONLY supported way to install on the fleet machine
(CON-954). It runs `uv tool install --force
git+https://github.com/danpoyar/cswap@<ref>` and then RESTARTS every
long-lived cswap process (`cswap auto` via its launchd job, `cswap watch`
via SIGTERM — the hub pane loop respawns it) and verifies each one started
at or after the install moment. A bare `uv tool install` is a half-rollout:
on 2026-08-18 the installed fix never reached the running daemon for 2 days
and 12 fleet slots lost their refresh tokens. The fleet's sensor job
watches the same invariant and logs `DEPLOY-SYNC` when a running process
predates the installed package.

Same package name (`claude-swap`) and the same two entrypoints (`cswap`,
`claude-swap`), so `~/bin/cswap`, the launchd job `com.amouen.cswap-auto` and
Quota keep working untouched. Rollback is one command — `uv tool install --force claude-swap==0.22.0` — but
know what it costs: no PUBLISHED upstream release carries `lastGoodUsage`
(it landed on main on 2026-07-28), so Quota loses every stored reading and
shows "No data" for each parked slot. It says so in the footer rather than
going quiet, and the way back is this fork. To roll back only OUR commits,
deploy an earlier ref of this repo instead: `scripts/deploy.sh <sha>` (the
door restarts the long-lived processes on rollbacks too — a half-rollback
strands them the same way a half-rollout does).

State lives outside the install and is never touched by reinstalling:
`~/.claude-swap-backup/` (sequence, usage store, autoswitch state) and the
macOS Keychain items (`claude-swap` service, `account-<n>-<email>`).

## The fleet law this fork serves

Quota (`~/projects/quota`) drives cswap under a written law — allowed
commands, no writes to Anthropic ever, `remove` only with a clearance. See
`~/projects/quota/CLAUDE.md`. Changes here must not widen what that app can
do by accident.
