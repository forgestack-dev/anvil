# Implementation milestones

## 0. Scaffold — implemented

- Python package, CLI, entry skill, and CI.
- Strict JSON ticket contracts and dependency-wave preview.
- Codex command preparation and local capability checks.
- Design plan and pinned upstream source reference.

## 1. Serial execution — accepted

- Persist immutable inputs, attempts, state transitions, and evidence in SQLite.
- Launch bounded Codex processes in isolated Git worktrees on macOS/Linux.
- Validate structured acceptance evidence and commit complete candidates.
- Review the exact integration revision with a separate read-only Codex turn.
- Require passing baseline and integration checks before advancing a managed local branch.
- Preserve failed work and logs; record interruption after terminating the active process group.
- Read saved state with `anvil status`; emit a JSON run report.
- Deterministic tests exercise successful dependency execution, false success, failed checks, review rejection, interruptions, and Git ownership/integration boundaries.
- A bounded live two-ticket exercise completed with independent reviews, six accepted criteria, and passing integrated checks; see [validation evidence](VALIDATION.md).

This version attempts each ticket once and stops the whole run on a blocker or failure. It does not resolve upstream skills, resume saved runs, or reconcile hard crashes. Verification commands are trusted argument arrays executed directly on the host. Completion means a verified local branch, not publication or tracker closeout.

## 1a. Claude Code adapter — implemented

- Select Codex or Claude Code per run, preserving Codex as the default and reading legacy Codex configurations.
- Probe the selected CLI and custom executable with `anvil doctor`.
- Run Claude Code with structured output, finite turns, file tools, and separate review; the supervisor provides the diff and runs checks.
- Preserve the same evidence, process lifecycle, and Git integration gates for both agents.
- Document Claude's customization and permission boundaries in [agent behavior](AGENT_ADAPTERS.md).

This adapter does not add parallel execution, upstream skill loading, or recovery. The serial milestone's live Codex exercise does not establish live Claude acceptance; consult [validation evidence](VALIDATION.md) for the recorded scope.

## 2. Parallel execution — implemented, live acceptance pending

- Named Codex/Claude worker pools, atomic claims, ownership tokens, recorded heartbeats, and shared managed-command limits.
- Dependency-driven dispatch and durable coordinator messages with accepted dependency handoffs.
- Single integration owner, declared resource reservations, and exclusive tickets.
- Deterministic real-process/Git tests for overlapping workers, stale-base integration, failures, and interruption.

The implemented contract is in [PARALLEL_EXECUTION.md](PARALLEL_EXECUTION.md). This milestone does not provide live conversational messaging, lease reassignment, multi-ticket atomic staging groups, or hard-crash recovery. A real mixed-agent acceptance exercise remains pending.

## 3. Recovery

- Pause/resume/stop commands, bounded retries, and backoff.
- Recover interrupted workers and reject stale results.
- Reconcile interrupted Git integration with persisted state.
- Test worker and supervisor failures at each state transition.

## 4. Full skill integration

- Discover complete, pinned upstream skill directories and supporting files.
- Resolve invocation requirements, human decisions, and missing tools.
- Record skill use and compatibility adaptations per attempt.
- Add Markdown intake and spec-to-ticket preparation.
- Independently evaluate skill behavior on realistic work.

## 5. Integrations

- Pilot Anvil in an application repository after the serial milestone is accepted.
- Add the preferred live issue tracker and external closeout semantics.
- Add further coding-agent adapters based on usage.
- Evaluate plugin distribution and remote execution based on usage.
