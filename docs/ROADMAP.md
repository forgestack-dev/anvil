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

Legacy configurations attempt each ticket once and stop the whole run on a blocker or failure. This initial milestone did not resolve upstream skills or recover saved runs; native recovery is covered below. Verification commands are trusted argument arrays executed directly on the host. Completion means a verified local branch, not publication or tracker closeout.

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

## 2a. Native AI Hero skill management — implemented

- Install complete upstream skill directories for Codex, Claude Code, or both in repository or global scope.
- Resolve a source reference once to an exact commit; preserve instruction bytes, metadata, supporting files, executable flags, and upstream license notices.
- Default to `engineering` and `productivity`; support explicit skill selection and opt-in experimental skills.
- Record installation selection, revision, and file hashes; update every recorded agent target together after checking for local changes and destination conflicts.
- Preview install/update changes and inspect installed files without network access through `skills status`.

These are native skills for ordinary agent sessions. Ticket-selected text instruction execution is implemented in milestone 4; catalog-wide behavior compatibility remains. Install/update previews fetch the source; only status is offline. Normal application errors roll back, while hard termination across multiple skill directories can require manual inspection. See [UPSTREAM.md](UPSTREAM.md).

## 3. Recovery — bounded native resume implemented

- Native resume for interrupted protocol-enabled runs: preserve accepted tickets, reconcile Git/ledger boundaries, retire old attempts, and revalidate candidates.
- Pause/stop commands, general failure retries, and backoff remain planned.
- Restart interrupted workers in fresh workspaces and reject stale results.
- Reconcile interrupted Git integration with persisted state.
- Test worker and supervisor failures at each state transition.

## 4. Full skill integration — compatibility preflight implemented

- Connect the managed, pinned upstream skill catalog to per-ticket execution. Implemented for explicitly selected UTF-8 text resources across Codex, Claude, Muse, and mixed pools.
- Resolve invocation requirements, human decisions, and missing tools. Implemented as an exact-instruction compatibility registry and pre-dispatch capability gate; adapters and interactive continuation for currently unavailable capabilities remain planned.
- Record the pinned revision, file hashes, and per-attempt instruction delivery. Implemented for the bounded text path.
- Add Markdown intake and spec-to-ticket preparation. Implemented for committed UTF-8 specifications through one read-only Codex, Claude, or Muse planning turn, with atomic validated output and source provenance.
- Select reviewed, installed skills automatically for empty ticket skill arrays. Implemented as opt-in deterministic rules with worker-capability filtering, recorded reasons, and recovery-safe frozen decisions.
- Independently evaluate skill behavior on realistic work.

## 5. Repository delivery, issue synchronization, and dashboard — specified

- Publish accepted runs as draft/ready pull requests on GitHub.com or Bitbucket Cloud.
- Import and synchronize Jira Cloud, Linear, or GitHub Issues independently of the code host; cover all six combinations.
- Preserve local acceptance semantics and close external issues only after confirmed delivery merge.
- Reconcile durable delivery/status operations after interruption and expose their evidence in a local read-only run dashboard.
- View work grouped by epic, project, milestone, or initiative as a read-only lens across runs; parent-construct writes are later work.

Measured limits from running this graph are in [observed execution limits](OBSERVED_LIMITS.md). See [the delivery and dashboard specification](DELIVERY_DASHBOARD_PLAN.md) for the proposed contracts, provider boundaries, six implementation slices, and acceptance matrix. This milestone is not implemented; its commands and configuration are design targets.

## 6. Invariant enforcement — implemented

- Close the process launch-site set so a new site fails the suite until its credential exclusion source is declared.
- Assert coordinator thread ownership of ledger and repository writes in the runtime, not only in review.
- Check the JSON schemas against runtime validation, and the CLI subcommands against the README and entry skill.
- Mark each enforceable claim in `AGENTS.md` with the test that enforces it, leaving unenforced claims visibly unmarked.

See [the invariant test specification](INVARIANT_TESTS.md) for the claim classification and the seven implementation slices, all complete, and for the violations recorded at `17ebf0e`, all closed. One slice was withdrawn rather than implemented: a scan for live models in ordinary tests rested on a bare executable name being the signal, which three prototypes disproved, so that section records the reason in place of a test. Nine claims carry a test marker, and claims with no mechanical predicate carry none, so a reader can tell a guarantee the suite enforces from one that rests on review.

## 7. Calibrated judgments — studied, not shipped

- Measure whether a calibrated model reads `docs/CRITERIA.md`'s criterion rules
  better than the keyword rule that was rejected for firing on 36 of 128.
- Detect a review that marked an acceptance criterion satisfied without the means
  to decide it, the defect recorded at run `51fec4cd`.
- Bound any such capability to tightening only: it may convert an acceptance into
  a stop, never satisfy a criterion, approve a candidate, or widen a scope.

The study ran. See [the judgment specification](JUDGMENT.md): section 11.4
records that a model reads two of the four criterion rules materially better
than any keyword rule and the other two no better than a regex, so the intake
gate annotates rather than blocks and two slices are withdrawn. The concession
check remains specified and unmeasured, because section 11.1 establishes that
this repository's ledger cannot label it — every recorded outcome is downstream
of the review being judged.

**Nothing here ships yet.** Section 10 keeps the capability out of the open CLI
and out of the paid service alike: it is internal, flag-gated, and unsupported.
That exclusion is a deferral rather than a verdict — section 10.4 records the
trigger for reopening it, which is Jev leaving early access, and which of the
objections general availability does and does not settle. The milestone is a
record of what was measured, not a release.

## Later integrations

- Add further coding-agent adapters based on usage.
- Evaluate plugin distribution and remote execution based on usage.

## 2b. Ticket status and adaptive routing — implemented, live validation bounded

Source JSON status publication/reconciliation, explicit provider profiles, normalized usage, deterministic routing, bounded same-agent escalation, local history, held-out policy gates, promotion/rollback and opt-in benchmark comparisons. See [current contract](ADAPTIVE_ROUTING.md). General execution recovery and external ticket sinks remain separate work.
