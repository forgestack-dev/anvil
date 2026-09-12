# Implementation milestones

## 0. Scaffold — current

- Python package, CLI, entry skill, and CI.
- Strict JSON ticket contracts and dependency-wave preview.
- Codex command preparation and local capability checks.
- Design plan and verified upstream source reference.

## 1. Serial execution

- Persist a run and attempts in SQLite.
- Create an isolated attempt workspace and launch Codex.
- Capture structured results and acceptance evidence.
- Review a complete candidate revision and verify integration.
- Exercise one small specification end to end.

## 2. Parallel execution

- Atomic claims, ownership tokens, heartbeat handling, and global process limits.
- Dependency-driven dispatch and durable worker messages.
- Single integration owner and explicit wide-refactor groups.
- Prove overlapping workers cannot corrupt task state or each other's workspaces.

## 3. Recovery

- Pause/resume/stop, attempt limits, timeouts, and backoff.
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

- Add the preferred live issue tracker and external closeout semantics.
- Add another coding-agent adapter.
- Evaluate plugin distribution and remote execution based on usage.
