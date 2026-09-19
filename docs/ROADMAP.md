# Implementation milestones

Numbered in the order they were specified, which after milestone 5 is no longer
the order they can be built in. The numbering is left alone because every
cross-reference and commit message depends on it; what is actionable is here
instead.

**Unblocked now.** Milestone 8 slices 1 to 3, terminal classification, failure
categories and cost basis: nothing precedes them and slice 3 is the seam any
later billed execution keys off. Milestone 11, amend retries, which milestone 8
slice 4 then needs. Milestone 5's delivery and issue slices, whose dashboard
slice is now additive views on the server milestone 10 shipped.

**Blocked or deferred.** Milestone 8 slices 4 and 5 follow milestone 11 and
milestone 10 respectively. Milestone 12 waits on a second person needing to
watch a run they did not start. Milestone 13 is decisions rather than code,
except its consent slices, and one item there has a deadline rather than a
priority. Milestone 7 is a record of what was measured and ships nothing.


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

## 2b. Ticket status and adaptive routing — implemented, live validation bounded

Source JSON status publication/reconciliation, explicit provider profiles, normalized usage, deterministic routing, bounded same-agent escalation, local history, held-out policy gates, promotion/rollback and opt-in benchmark comparisons. See [current contract](ADAPTIVE_ROUTING.md) and the [routing plan](ADAPTIVE_ROUTING_PLAN.md) it was built from. General execution recovery and external ticket sinks remain separate work.

## 3. Recovery — bounded native resume implemented

- Native resume for interrupted protocol-enabled runs: preserve accepted tickets, reconcile Git/ledger boundaries, retire old attempts, and revalidate candidates.
- Pause/stop commands, general failure retries, and backoff remain planned.
- Restart interrupted workers in fresh workspaces and reject stale results.
- Reconcile interrupted Git integration with persisted state.
- Test worker and supervisor failures at each state transition.

See [the recovery contract](RECOVERY.md) for what native resume validates before continuing, and what it refuses.

## 4. Full skill integration — compatibility preflight implemented

- Connect the managed, pinned upstream skill catalog to per-ticket execution. Implemented for explicitly selected UTF-8 text resources across Codex, Claude, Muse, and mixed pools.
- Resolve invocation requirements, human decisions, and missing tools. Implemented as an exact-instruction compatibility registry and pre-dispatch capability gate; adapters and interactive continuation for currently unavailable capabilities remain planned.
- Record the pinned revision, file hashes, and per-attempt instruction delivery. Implemented for the bounded text path.
- Add Markdown intake and spec-to-ticket preparation. Implemented for committed UTF-8 specifications through one read-only Codex, Claude, or Muse planning turn, with atomic validated output and source provenance.
- Select reviewed, installed skills automatically for empty ticket skill arrays. Implemented as opt-in deterministic rules with worker-capability filtering, recorded reasons, and recovery-safe frozen decisions.
- Independently evaluate skill behavior on realistic work.

The contracts are [skill execution](SKILL_EXECUTION.md), [skill requirements](SKILL_REQUIREMENTS.md), [automatic selection](AUTOMATIC_SKILL_SELECTION.md) and [specification preparation](SPEC_PREPARATION.md).

## 5. Repository delivery, issue synchronization, and dashboard — specified

- Publish accepted runs as draft/ready pull requests on GitHub.com or Bitbucket Cloud.
- Import and synchronize Jira Cloud, Linear, or GitHub Issues independently of the code host; cover all six combinations.
- Preserve local acceptance semantics and close external issues only after confirmed delivery merge.
- Reconcile durable delivery/status operations after interruption and expose their evidence in a local read-only run dashboard.
- View work grouped by epic, project, milestone, or initiative as a read-only lens across runs; parent-construct writes are later work.

Milestone 10 shipped the read-only server this milestone's dashboard slice proposed, so that slice is now additive views on [the serve contract](SERVE.md) rather than a second server. Measured limits from running this graph are in [observed execution limits](OBSERVED_LIMITS.md). See [the delivery and dashboard specification](DELIVERY_DASHBOARD_PLAN.md) for the proposed contracts, provider boundaries, six implementation slices, and acceptance matrix. This milestone is not implemented; its commands and configuration are design targets.

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
than any keyword rule, that a third never fires at all, and that the fourth is
unresolved — section 11.5 records how a labeling pass destroyed this corpus's
ability to decide it. The intake gate therefore annotates rather than blocks,
and two slices are withdrawn. The concession
check remains specified and unmeasured, because section 11.1 establishes that
this repository's ledger cannot label it — every recorded outcome is downstream
of the review being judged.

**Nothing here ships yet.** Section 10 keeps the capability out of the open CLI
and out of the paid service alike: it is internal, flag-gated, and unsupported.
That exclusion is a deferral rather than a verdict — section 10.4 records the
trigger for reopening it, which is Jev leaving early access, and which of the
objections general availability does and does not settle. The milestone is a
record of what was measured, not a release.

## 8. Run economics and terminal classification — specified

- Classify an invocation's terminal reason from its own result event rather than from its process exit code.
- Record turn exhaustion and budget exhaustion as failure categories the escalation ladder and local history can read.
- Distinguish a billed cost from a subscription list estimate, and gate `max_budget_usd` on the former.
- Retain a turn-exhausted attempt's workspace rather than restarting it from the base revision.
- Aggregate the per-invocation usage a run already records into its report and the read-only dashboard.

See [run economics](RUN_ECONOMICS.md) for the five slices, the measured token and
terminal-reason decomposition behind them, and what one day of one repository's
graph does not establish. [OBSERVED_LIMITS.md](OBSERVED_LIMITS.md) records the
same runs from the turn-budget side; the two agree on the mechanism and neither
is a fix.

This milestone is not eighth in dependency order. Slices 1 to 3 are unblocked.
Slice 4 needs the tree restoration milestone 11 has since shipped as
`Repository.create_amended_worktree`, so it is no longer blocked on it, and
slice 5 extends the serve contract milestone 10 shipped.

## 9. The acceptance decision — implemented

- Run the configured checks on the integration revision before independent review, so a reviewer that cannot execute anything never judges a candidate the commands already reject.
- Require a review to assess every acceptance criterion whichever verdict it returns, and every finding to name the criterion it fails and a location the supervisor resolves against the reviewed revision.
- Stop a ticket for its author, without escalating a worker, when a rejection concedes every criterion or points outside the sites its criterion declared.
- Let a criterion declare the paths its claim holds over, checked against the base revision before a run directory exists.

See [the acceptance decision](ACCEPTANCE.md) for the four stages, all implemented, and for the designs that were refuted on the way — including two that would have merged a candidate which fails its own tests. The stage order was revised mid-implementation: bounding a rejection has to precede the conceded-rejection stop, because that stop's trigger is vacuous until the acceptance map is mandatory. The measurements are in [observed execution limits](OBSERVED_LIMITS.md), whose account of run `51fec4cd` this work corrected; the authoring side is [the criterion contract](CRITERIA.md).

## 10. Run observation and retained revisions — implemented

- Serve saved and in-progress runs read-only over local HTTP, with a packaged dashboard page and an event stream that resumes exactly from a ledger cursor.
- Version the read contract so an additive change in the CLI cannot break a separate consumer.
- Name every candidate and integration revision a run creates, so a rejected attempt survives the run that discarded it.
- Give the operator an explicit way to list and remove those refs, refusing to strand a revision a ledger still records.

See [the serve contract](SERVE.md) for the routes, the loopback-only binding, and the page. Retention is `tickets/candidate-retention.json`, both tickets implemented: before it, a rejected candidate was reachable from no ref and an ordinary `git gc` destroyed the evidence a ledger pointed at. A ref's lifetime is tied to its ledger's, so the pair is reclaimed together rather than becoming a second thing to remember.

## 11. Amend retries — implemented

- Start a replacement attempt from the rejected candidate's tree rather than from an empty worktree, addressing the bound findings instead of implementing the ticket again.
- Keep the profile that produced the candidate, leaving the escalation allowance for the attempt after it.
- Permit an amend only while the accepted base is unchanged, because restoring a stale tree onto an advanced base would silently revert a peer's integrated work.

See [amend retries](AMEND_RETRIES.md). The mechanism was prototyped rather than proposed and shipped as prototyped: `commit_candidate` refuses a worktree whose `HEAD` is not the base, so an amend restores the candidate's tree into a worktree left at the base and squashes it into one commit as usual. `commit_candidate`, `prepare_integration` and the phase machine are untouched. The saving is unmeasured — orientation is roughly 80% of an invocation by the same measurements, and an amend pays it too — and that document names the live exercise that would settle it, so this milestone ships a mechanism rather than a demonstrated economy.

## 12. Reaching a run from another device — specified

- State what binding the read-only server beyond loopback would have to satisfy, so the decision is made once rather than argued per request.
- Keep the SSH tunnel documented as the supported path, not as a workaround.

See [remote access](REMOTE_ACCESS.md). A mobile client is not a UI problem: it is this milestone plus packaging, and pointless before it. The document also records why the work should wait — the trigger worth acting on is a second person needing to watch a run they did not start, which is the point at which [the product boundary](PRODUCT_BOUNDARY.md) says the requirement stops being local. This milestone is not implemented and may never be built in this form.

## 13. Open core and the paid boundary — decisions open, the open CLI's half specified

- Draw the line at what requires other people: everything needed to run Anvil correctly on one machine stays open, and anything requiring other machines, users, or runs' data is a paid service.
- Settle the licensing questions before they stop being available, starting with a contributor agreement.
- Specify the interface between the open CLI and any paid service before either exists, so an additive change in one cannot break the other.
- Carry contribute and consume decisions per memory tier in a run configuration and a per-ticket exclusion in the ticket contract, recorded as a ledger event so a projection honors what a run declared rather than only what is configured now.
- Withhold excluded content at `anvil serve`, serving a marker that names the scope which excluded it rather than dropping the row, and keep the decision a read-time filter so a withdrawal reaches rows already written.

See [the product boundary](PRODUCT_BOUNDARY.md), which is a recommendation and not ratified, [the licensing decisions](LICENSING.md), [the cloud sync contract](CLOUD_SYNC.md), and [shared memory](SHARED_MEMORY.md). Nothing here is implemented and no commitment has been made. The consent work is the exception to that status rather than to the implementation: section 12 of the shared memory record carries four slices and the two decisions they rest on, bounded to this repository because the service that holds the corpus is outside it per section 6 of the product boundary. Until its fourth slice exists a declaration is intent that nothing enforces. One item has a deadline rather than a priority: a contributor agreement stops being available the first time an outside contribution merges without one, which freezes the licence permanently.

## Later integrations

- Add further coding-agent adapters based on usage.
- Evaluate plugin distribution and remote execution based on usage.
