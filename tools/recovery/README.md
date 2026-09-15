# Anvil run recovery tools

Operator tooling for surviving supervisor deaths. Anvil has no resume: if the
supervisor process dies (VM restart, OOM kill), the run cannot be continued.
These scripts launch a replacement run over the remaining tickets and carry
across any implementations that were filed but never integrated.

## Scripts

- `recover.sh <dead-run-id> [--runs-dir DIR] [--state-dir DIR]`
  One-command recovery. Reads the dead run's `state.sqlite`, checks out the
  dead run's integration HEAD as the new base, builds a new run directory
  with remaining tickets (dropping `done` tickets and pruning their
  dependencies), launches the new supervisor, and backgrounds `auto-port.sh`.
- `auto-port.sh <new-run-id> <port-manifest> [state-base]`
  Waits for each ported ticket's new worker handoff, then applies the old
  worktree's tracked diff plus untracked files. Tickets whose diff conflicts
  are reported for manual porting.
- `watchdog.sh [--state-dir DIR]`
  Polls the run recorded in `$STATE/ACTIVE_RUN`; if the supervisor is dead
  and the run did not exit cleanly, runs `recover.sh`. Safe to run every few
  minutes from cron. Never recovers the same run twice.

## Environment

- `ANVIL_BIN` — path to the anvil executable (default: `anvil` on PATH).
- `ANVIL_RECOVERY_STATE` — directory holding `ACTIVE_RUN`, `RECOVERED`, and
  `watchdog.log` (default: `~/.local/state/anvil-recovery`).
- Any environment the run needs (e.g. `DATABASE_URL` for DB-backed
  verification) must be exported by whoever invokes `recover.sh`; the
  relaunched supervisor inherits it.

## Watching a manually launched run

`recover.sh` records the new run in `$STATE/ACTIVE_RUN` itself. To watch a
run you launched by hand, write `"<run-id> <run-dir>"` there:

```
echo "<run-id> /path/to/run-dir" > ~/.local/state/anvil-recovery/ACTIVE_RUN
```

Then run `watchdog.sh` on a schedule:

```
*/3 * * * * /path/to/anvil/tools/recovery/watchdog.sh
```

## Requirements

`bash`, `sqlite3`, `git`, `python3`, and `pgrep`.
