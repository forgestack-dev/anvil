# Coordinated worker pools

The worker-pool runtime keeps one supervisor, one ledger, and one integration owner
for a repository. A configured worker pool may contain Codex, Claude Code, or
both. Each worker slot owns one ticket until that ticket has been accepted or
stopped. This bounds both running implementations and queued candidates.

## Configuration and dispatch

`workers` is a list of named agent slots. The existing top-level `agent` and
`agent_binary` select the independent reviewer for a pool run. Without `workers`,
the existing serial execution path remains available. `max_processes` bounds
managed command invocations across workers, review, verification, and supervisor
Git commands; it defaults to the number of worker slots.

Tickets can name a `worker`, declare shared `resources`, or set `exclusive: true`.
Unassigned tickets go to an available compatible slot. Dependencies must be
accepted before dispatch. Resources stay reserved through integration; an
exclusive ticket runs without any other ticket in flight. These declarations
coordinate known shared work, not an inferred file-ownership system.

## Ownership and communication

The supervisor atomically claims each ticket in SQLite with a unique attempt ID,
worker assignment, and base commit. Only the supervisor writes ledger state or
Git refs. Threads run bounded agent or verification commands, returning results
to the supervisor. The supervisor records heartbeats for active attempts and
durable dispatch/result messages. Accepted dependency summaries and commit IDs
are delivered in downstream prompts as evidence to inspect, not instructions.

This milestone does not add conversational messaging into an already-running
model turn, retries, lease reassignment, or hard-crash recovery. Heartbeats record
supervisor ownership; they do not establish that an agent is making progress.

## Integration and stopping

Completed candidates enter one integration queue. The supervisor applies each
candidate to the latest accepted commit, independently reviews that exact
integration revision, runs required checks, checks for subsequent mutations,
and advances the managed branch with compare-and-swap. Dependencies are released
only after the corresponding `done` transaction. Conflicts or rejected checks
stop the run with work and evidence preserved.

A blocker, failure, or interruption cancels every active managed command and
waits for process-group cleanup before releasing the repository lock. Other
unfinished claimed tickets become interrupted; undispatched tickets stay
pending. Completed tickets and the accepted branch remain available. A new run
still starts fresh from the target checkout; status does not resume a run.

## Required validation

- Demonstrate overlapping real fake Codex/Claude CLI processes without model calls.
- Require dependent tickets to observe accepted changes from both agents.
- Exercise resource reservations, exclusive tickets, and worker affinity.
- Apply stale-base candidates to the latest accepted branch and review/check the
  exact resulting commit; preserve the branch on conflicts and semantic failures.
- Exercise worker/reviewer failures, permission denials, stale ownership tokens,
  shared process limits, cancellation, repeated SIGINT, and durable saved status.
- Keep existing serial, CLI, configuration, and packaging checks passing.
