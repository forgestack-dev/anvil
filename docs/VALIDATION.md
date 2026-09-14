# Validation

Validated on 2026-09-12 using Python 3.11 on macOS and Codex CLI 0.153.4.

## Automated checks

Claude stream compatibility was checked on 2026-09-14. The suite now includes
269 tests, with regressions for `system/task_summary` events following a result
from both worker and reviewer processes. Serial dependency integration and mixed
worker-pool tests emit these trailers while checking the exact reviewed,
verified, and integrated revision. Raw stream bytes remain preserved; malformed
trailers, duplicate results, permission denials, failed results, nonzero exits,
and timeouts still fail the turn.

Replaying a captured 21-turn Claude Code worker event log through the parser reproduced
the failure on `0.1.0.dev3` and returned the original structured result on
`0.1.0.dev4`. This replay did not invoke a model or change saved run evidence;
it verifies stream compatibility rather than retroactively accepting the ticket.

Global Claude profile support adds 14 regression cases, bringing the suite to
267 tests. They exercise custom destinations outside the home directory through
the CLI, saved-profile checks, legacy default installations, local edits,
unsafe or conflicting paths, manifest tampering, and rollback across both agent
directories. The primary CLI regression fails against the original implementation
because it reports the default Claude directory instead of the configured one.

A network check also installed all 25 stable upstream skills into an isolated
temporary home and an external Claude profile at revision
`3cca18b368ae95cdbdebbff572ccafa662551015`. Both copies matched, preview created
no installation, update reported no changes, and offline status passed. The
default Claude directory was not created. This used no real personal skill
directories or model calls.

The AI Hero management milestone passes 253 tests, including 51 new source,
installation/update, and CLI cases. Offline fixtures check pinned source
resolution, complete skill resources and license notices, archive boundaries,
both agent destinations, saved selections, local-change protection, previews,
read-only status, unreadable local directories, concurrent-operation locks, rollback after partial placement
or manifest failure, and repeated interruption during a synchronized update.

A separate network acceptance check downloaded revision
`3cca18b368ae95cdbdebbff572ccafa662551015` from the official upstream repository.
It previewed and installed all 25 stable skills into both native agent directories
in an isolated temporary Git repository, checked matching skill contents and
license notices, then confirmed that update was a no-op and offline status was
clean. No personal skill directories were changed and no upstream code or model
turns were executed. This verifies native skill installation, not Anvil ticket
skill loading or the behavior of every upstream skill.

The previous serial/Claude milestone passed 155 tests. The expanded suite now also covers coordinated mixed-agent pools, strict scheduling contracts, shared process capacity and cancellation, ownership heartbeats, and durable handoffs. Tests use Python 3.11 on macOS without live model calls; CI also runs Python 3.12 on Linux. See the mixed-pool validation below for the new behavioral coverage.

Execution tests use temporary real Git repositories. One runs through the real Codex adapter with a local fake executable, including its schema files, output artifacts, and subprocess boundary. Other scenarios cover rejected review, missing acceptance evidence, failed verification, modified candidates, worker-created commits, stale attempts, repository locks, and interruptions immediately after persisted completion. Regressions also cover Git filters surviving timeout or interruption, case-distinct ticket IDs sharing a filesystem, and interruption immediately after an attempt is recorded. Real SIGINT regressions verify cleanup when interruption arrives before process creation returns or repeatedly during shutdown, along with restoration and delivery of the caller's signal handlers. Failed work cannot release dependent tickets or advance the accepted branch.

The example graphs validate and plan. The entry skill passes the skill-creator structural validator. Its pool guidance covers one coordinated supervisor, explicit worker/reviewer selection, dependency handoffs, declared resources, and stopping behavior. Instruction validation is distinct from the live runtime exercises below and does not establish full AI Hero skill compatibility.

Additional regressions use real Git subprocesses to prove inherited repository overrides cannot redirect worker, reviewer, or verification commands to the original checkout. Startup coverage sends real SIGINT before and after the transition to running, checks persisted reports and lock release, and verifies startup errors preserve pending tasks and the original exception when no ledger was initialized.

Claude-specific tests exercise configuration selection through a real fake executable, two dependent tickets, supervisor-supplied review diffs, and the exact reviewed/verified/integrated commit gate. Invalid or contradictory approval, permission denial, reviewer mutation, requested changes, and failed checks preserve the accepted branch and leave dependents pending. Adapter tests reject malformed or ambiguous JSON streams, error envelopes, oversized output, replaced artifact files, timeouts, and nonzero exits. Doctor coverage includes relative executable paths and relative PATH entries. The installed Claude Code 2.1.260 passes the version/help compatibility probe.

Executable-resolution regressions exercise relative PATH lookup before worktree execution, retain the selected entrypoint through subsequent directory/PATH changes, and run basename-dispatching symlink wrappers through both doctor and execution. Configuration loading preserves Claude aliases and symlinked parent traversal. A two-ticket serial fixture verifies the same alias handles every worker and reviewer even after PATH changes between tickets.

## Coordinated mixed-agent pool validation

An actual Codex adapter and actual Claude adapter execute private fake CLI programs
concurrently in distinct real Git worktrees. An external bounded barrier proves
their implementation intervals overlap. Their first two tickets start from the
same base; the second candidate is integrated on top of the first accepted change.
A third dependent ticket observes both accepted outputs and receives their saved
summaries and commit IDs. Its recorded reviewed, verified, and integrated SHAs
match. The original checkout remains clean and its HEAD is unchanged.

Further tests cover worker affinity, resource reservations through review,
exclusive tickets waiting for active peers, and slots retaining queued candidates.
Two-connection SQLite tests prove claims are atomic; stale and foreign attempt
tokens cannot update ownership or messages. Saved ledgers and reports retain
assignments, heartbeats, dependency handoffs, review messages, and terminal states.

Failure tests preserve accepted work on a conflicting cherry-pick, a clean
cherry-pick that fails combined semantic checks, invalid evidence, reviewer
rejection/mutation, and a peer failure during verification. A worker failure
cancels peers even while supervisor Git waits for shared command capacity.
Subprocess tests send real SIGINT to mixed worker runs, repeat it during shutdown
and terminal persistence, and interrupt immediately after an atomic claim. They
verify process-group cleanup, saved interrupted ownership, pending dependencies,
and repository-lock release. A one-command capacity test includes supervisor Git,
verification, and agents, including cancellation while waiting for capacity.

These are deterministic execution tests, not recorded live mixed-model acceptance.
No live mixed Codex/Claude pool run has been performed. The opt-in
`examples/parallel-run.json` and `examples/parallel-tickets.json` describe a
three-ticket mixed-agent exercise against a fresh sibling `anvil-demo` repository;
see the README for preparation. Running it intentionally makes real model calls.

## Live Codex exercise

The opt-in fixture was generated with [tests/create_live_fixture.py](../tests/create_live_fixture.py). It begins with a committed Python module and one passing baseline test. The two dependent tickets implement ASCII slug normalization and a collision-checked label map. Each has three acceptance criteria.

Run `87d58241d63e4aea9d712a2580955fb7` completed successfully in approximately 3 minutes 47 seconds, using two real Codex implementation turns and two separate read-only review turns. The configured model and existing account were used without a model override or sandbox bypass.

| Ticket | Result | Reviewed, verified, and integrated commit |
|---|---|---|
| `slugify` | Done; all three criteria approved; 7 project tests passed | `acded144a3969429d9e1d3384604e54df8f73a6c` |
| `slug-map` | Done; all three criteria approved; 13 project tests passed | `26aa82be4288a7b313c7e5eb2aa80580e1da80bd` |

The second attempt's baseline was the first accepted commit. The managed branch ended at the second accepted commit; the original checkout remained clean at its original baseline. A separate post-run check confirmed normalization, Unicode separator handling, input validation, ordering, and duplicate-slug rejection directly against the integrated module. The saved ledger and report agree on successful completion.

The runtime retains the fixture's local logs, structured worker/reviewer evidence, and worktrees. Transcripts and machine-specific paths are not part of this repository. This is a small acceptance exercise, not evidence of production reliability, unattended recovery, parallel coordination, or upstream skill resolution.

## Live Claude Code exercises

Three bounded runs used Claude Code 2.1.260 with the existing account and configured model, without a model override or permission bypass. Live initialization events confirmed the worker's `Edit`, `Glob`, `Grep`, `Read`, and `Write` tools, the reviewer's `Glob`, `Grep`, and `Read` tools, and the internal `StructuredOutput` tool in both roles. Both roles produced schema-shaped terminal results with no permission denials.

| Run | Recorded outcome |
|---|---|
| `8f65751cbe004d8d8d86c9088d35509c` | Failed: the reviewer approved but included a note in `findings`, contradicting the shared acceptance contract. Prompts and schema descriptions now explicitly require empty findings for approval; validation remains unchanged. |
| `d5f7d413ca51442b9ae8a3e5f33b586e` | Blocked: independent review caught a generated test using ASCII K in place of the intended Unicode Kelvin sign. Running the suite independently reproduced the failure. |
| `d37945be4ad343aeb3778c0cd2eeddc9` | Blocked: independent review caught an incorrect expected result for a decomposed Unicode string. Running the suite independently reproduced the failure. |

All three preserved the original clean checkout and left the managed branch at its baseline with the dependent ticket pending. Saved ledgers and reports agree. The fixture now asks for explicit Unicode code points in tests to avoid ambiguous glyphs.

These exercises establish live invocation, file implementation, structured results, independent review, and stopping behavior. **A complete two-ticket live Claude burndown has not yet passed.** Deterministic fake-CLI tests cover successful integration and dependency advancement. File-only Claude workers cannot run tests before review, and this milestone does not automatically repair rejected candidates. No live transcripts or machine-specific paths are committed.

## Reproduce a live exercise

From a source checkout, choose a new directory outside the Anvil checkout. The generator refuses to overwrite an existing destination and does not launch an agent:

```sh
python3 tests/create_live_fixture.py /tmp/anvil-live-example
anvil plan /tmp/anvil-live-example/tickets.json
anvil run /tmp/anvil-live-example/run.json
```

The last command intentionally launches real Codex turns and uses the configured account. To generate a Claude Code configuration, add `--agent claude-code` to the fixture generator command, using a new destination directory. The generator still launches no agent; the subsequent `anvil run` uses the selected agent. These instructions enable a separate exercise and do not imply a recorded live Claude result.

Each agent turn has a 300-second limit and each verification command a 30-second limit. Use the saved run directory printed by `run` with `anvil status <run-directory> --json`. A new run is a fresh attempt; automatic resume is not available.
