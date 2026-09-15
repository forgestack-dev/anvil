# Native recovery: bounded milestone

## Contract

`anvil resume RUN_DIRECTORY [--json]` continues the same ledger and managed branch.
It uses the saved configuration and immutable tickets; it rejects changed inputs,
successful runs, failed/blocked runs, and ledgers predating the recovery protocol.
Ctrl-C and supervisor death are supported. `status` remains read-only.

Resume takes the common repository lock, then checks persistent command records.
A still-existing process group blocks recovery; Anvil never kills a recycled PID.
An unresolved spawn intent also blocks recovery for operator inspection. Commands
that deliberately detach from their process group remain outside containment.
Old Muse handoffs are retired; operators must stop servicing them before resume.

The accepted baseline is reconstructed from ordered completion evidence, validating
worker/reviewer criteria, passing configured checks, single-parent Git ancestry,
and matching reviewed/verified/integrated revisions. The managed branch must equal
that baseline. One recorded integration intent may have already advanced the branch:
only that exact, fully evidenced revision is reconciled to done. Unexplained branch
movement fails closed. Detached integration HEAD alone never establishes acceptance.

Recovery atomically records reconciliation, retires unfinished ownership tokens,
and resets only unfinished tickets. Completed tickets, original inputs, events,
attempts and artifacts remain. Fresh attempts and a fresh integration worktree
prevent late results from retired attempts from being consumed. The accepted base
is verified again before dispatch.

A recorded candidate with valid completed worker evidence and a single commit on
its recorded base can be reused if no rejection or failed check is recorded. Its
commit is immutable; old mutable worktrees are never copied. It is reused only when its recorded base equals the current accepted base, in a new
worktree and independently reviewed and verified again. Missing/invalid candidates
restart implementation. Restarting after interruption is explicitly authorized by
resume; this does not add automatic provider retries or retry failed/blocked runs.

Adaptive runs retain their frozen policy and CLI versions, charge all previous
invocation reservations (actual costs when complete, conservative reserves otherwise),
and reserve new work/review calls before reuse or implementation. Restarted attempts
consume the remaining run budget. The one review/check escalation limit is preserved.
Recovery-affected runs are excluded from policy training to avoid rewriting imported
samples or treating interruptions as model-quality evidence.

Runs with ticket-selected skills retain the pinned catalog revision and exact file
hashes recorded at startup. Resume validates that snapshot and supplies it to fresh
implementation attempts; changes to the repository installation do not alter the run.

## Acceptance tests

- Interrupt at implementation, review, verification, before/after branch advance,
  and after completion; resume without duplicate acceptance or prerequisite work.
- Reject live supervisors, live orphan groups, unresolved spawn records, foreign or
  moved branches, changed tickets, old ledgers and failed/blocked runs.
- Preserve old evidence and reject stale attempt tokens; replay candidate review
  and checks, restarting when the saved candidate cannot be verified.
- Keep source status publication on the same binding and restore cumulative budgets.
- Test repeated interruption, parallel pools, and a killed supervisor with real
  temporary Git repositories and fake agent processes. No paid model calls.

## Deferred

Pause commands, selective continuation after failures, general provider retries,
automatic orphan termination, cross-host recovery and migration of old runs remain
future milestones. Existing tools/recovery scripts create replacement runs and are
not a substitute for this ledger reconciliation protocol.
