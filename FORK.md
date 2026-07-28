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
Quota keep working untouched. Rollback to upstream is one command:
`uv tool install --force claude-swap==0.22.0`.

State lives outside the install and is never touched by reinstalling:
`~/.claude-swap-backup/` (sequence, usage store, autoswitch state) and the
macOS Keychain items (`claude-swap` service, `account-<n>-<email>`).

## The fleet law this fork serves

Quota (`~/projects/quota`) drives cswap under a written law — allowed
commands, no writes to Anthropic ever, `remove` only with a clearance. See
`~/projects/quota/CLAUDE.md`. Changes here must not widen what that app can
do by accident.
