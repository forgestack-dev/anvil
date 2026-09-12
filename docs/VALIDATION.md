# Validation

Validated on 2026-09-12 using Python 3.11 on macOS and Codex CLI 0.153.4.

## Automated checks

The standard unittest suite passes **150 tests** without live model calls, including the Claude Code adapter additions tested on Python 3.11. It covers dependency planning, strict input and result contracts, SQLite transactions and evidence gates, Git ownership and compare-and-swap integration, real subprocess lifecycle handling, and CLI run/status/doctor behavior.

Execution tests use temporary real Git repositories. One runs through the real Codex adapter with a local fake executable, including its schema files, output artifacts, and subprocess boundary. Other scenarios cover rejected review, missing acceptance evidence, failed verification, modified candidates, worker-created commits, stale attempts, repository locks, and interruptions immediately after persisted completion. Regressions also cover Git filters surviving timeout or interruption, case-distinct ticket IDs sharing a filesystem, and interruption immediately after an attempt is recorded. Real SIGINT regressions verify cleanup when interruption arrives before process creation returns or repeatedly during shutdown, along with restoration and delivery of the caller's signal handlers. Failed work cannot release dependent tickets or advance the accepted branch.

Both example graphs validate and plan. The entry skill passes the skill-creator structural validator. An independent instruction review checked planning, authorized serial execution, explicit upstream skill requests, failed-run inspection, and unsupported parallel execution/closeout; those routes match the CLI. This instruction review is distinct from the live runtime exercise below and does not establish full AI Hero skill compatibility.

Additional regressions use real Git subprocesses to prove inherited repository overrides cannot redirect worker, reviewer, or verification commands to the original checkout. Startup coverage sends real SIGINT before and after the transition to running, checks persisted reports and lock release, and verifies startup errors preserve pending tasks and the original exception when no ledger was initialized.

Claude-specific tests exercise configuration selection through a real fake executable, two dependent tickets, supervisor-supplied review diffs, and the exact reviewed/verified/integrated commit gate. Invalid or contradictory approval, permission denial, reviewer mutation, requested changes, and failed checks preserve the accepted branch and leave dependents pending. Adapter tests reject malformed or ambiguous JSON streams, error envelopes, oversized output, replaced artifact files, timeouts, and nonzero exits. Doctor coverage includes relative executable paths and relative PATH entries. The installed Claude Code 2.1.260 passes the version/help compatibility probe.

## Live Codex exercise

The opt-in fixture was generated with [tests/create_live_fixture.py](../tests/create_live_fixture.py). It begins with a committed Python module and one passing baseline test. The two dependent tickets implement ASCII slug normalization and a collision-checked label map. Each has three acceptance criteria.

Run `87d58241d63e4aea9d712a2580955fb7` completed successfully in approximately 3 minutes 47 seconds, using two real Codex implementation turns and two separate read-only review turns. The configured model and existing account were used without a model override or sandbox bypass.

| Ticket | Result | Reviewed, verified, and integrated commit |
|---|---|---|
| `slugify` | Done; all three criteria approved; 7 project tests passed | `acded144a3969429d9e1d3384604e54df8f73a6c` |
| `slug-map` | Done; all three criteria approved; 13 project tests passed | `26aa82be4288a7b313c7e5eb2aa80580e1da80bd` |

The second attempt's baseline was the first accepted commit. The managed branch ended at the second accepted commit; the original checkout remained clean at its original baseline. A separate post-run check confirmed normalization, Unicode separator handling, input validation, ordering, and duplicate-slug rejection directly against the integrated module. The saved ledger and report agree on successful completion.

The runtime retains the fixture's local logs, structured worker/reviewer evidence, and worktrees. Transcripts and machine-specific paths are not part of this repository. This is a small acceptance exercise, not evidence of production reliability, unattended recovery, parallel coordination, or upstream skill resolution.

## Reproduce a live exercise

From a source checkout, choose a new directory outside the Anvil checkout. The generator refuses to overwrite an existing destination and does not launch an agent:

```sh
python3 tests/create_live_fixture.py /tmp/anvil-live-example
anvil plan /tmp/anvil-live-example/tickets.json
anvil run /tmp/anvil-live-example/run.json
```

The last command intentionally launches real Codex turns and uses the configured account. To generate a Claude Code configuration, add `--agent claude-code` to the fixture generator command, using a new destination directory. The generator still launches no agent; the subsequent `anvil run` uses the selected agent. These instructions enable a separate exercise and do not imply a recorded live Claude result.

Each agent turn has a 300-second limit and each verification command a 30-second limit. Use the saved run directory printed by `run` with `anvil status <run-directory> --json`. A new run is a fresh attempt; automatic resume is not available.
