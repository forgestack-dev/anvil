# Ticket lifecycle visibility and adaptive model routing

Status: implemented in the adaptive-routing change, with deterministic validation; live acceptance remains limited by account availability and the authorized $0.50 pilot budget. See ADAPTIVE_ROUTING.md for the shipped contract and VALIDATION.md for evidence.

Implementation choices: additive version-1 configuration fields; schema-version-2 run ledgers; profiles and routing live under `adaptive`; the command form for training/promotion takes a run configuration. Assessment is deterministic (model-assisted assessment remains optional future work). The first release publishes JSON tickets and implements the one-retry live-run subset, not full crash recovery. Live savings and long-term defect learning are not claimed.

## Objective

For each ticket, assess complexity and risk, select a suitable model and reasoning effort, implement and verify the change, evaluate the observed outcome, and use accumulated evidence to improve future routing. Optimize total cost per accepted ticket subject to quality and completion constraints. Include assessment, failed attempts, implementation, review, and evaluation in the cost accounting.

Deliver this inside Anvil's Python runtime, supported by its entry skill. A skill alone cannot reliably enforce selection, budgets, concurrency, or persistent learning. Keep Python 3.11 and the standard library unless a concrete need justifies a dependency.

## Current constraints

- `config.py`, `contracts.py`, and the run/ticket schemas have no model or effort fields. Both adapters inherit CLI model settings.
- `execution.py` and `parallel.py` implement serial and mixed-agent execution with independent review and exact-revision verification.
- `store.py` keeps one SQLite ledger per run. Its attempts table makes task_id unique, and terminal failures stop the run. Adding escalation requires explicit state-machine and attempt-history changes.
- Only the coordinator writes Git and SQLite. Resource reservations and worker slots remain held through acceptance. All active processes must stop before the repository lock is released.
- Current logs are evidence, not a normalized usage dataset or a learning mechanism. Historical runs may lack reliable model, effort, or cost information.

## Design decisions

1. Make routing opt-in. Existing configurations retain their current behavior. Do not silently pick new models, increase effort, retry, or spend additional budget.
2. Define named execution profiles in trusted run configuration. Each specifies agent, exact model identifier, supported effort, executable, and optional limits. Names such as economical, standard, and demanding are project policy labels, not universal rankings of models.
3. Keep model identifiers and supported effort values provider-specific. Validate installed CLI support against official documentation and local capability probes during implementation. Do not assume Codex and Claude share an effort scale. Reject unsupported explicit settings before starting work; never silently drop them.
4. Select profiles only within the configured worker's agent/executable capabilities. Honor ticket worker affinity and explicit profile overrides. An unavailable compatible slot waits; an impossible assignment fails preflight. Do not substitute a different provider merely because a slot is free.
5. Keep the review profile independently configured and fixed during the initial rollout. Worker routing cannot lower the review standard, remove criteria, or alter verification commands.
6. Freeze the routing policy, model catalog, pricing assumptions, and history snapshot for each run. Learning changes subsequent runs, not in-flight decisions. New attempts can escalate only along the frozen, configured path.
7. Store learning locally per repository initially. Cross-project sharing is outside the first release. Repository text and worker claims cannot modify routing policy or prices.

## Proposed interfaces and records

These are design targets, not usable commands or schema fields yet.

- Run configuration: `profiles`, separate worker/reviewer defaults, and a `routing` section with mode, eligible profiles, assessment bounds, escalation path, and budget policy.
- Ticket configuration: optional explicit execution profile and structured risk hints. Preserve worker affinity. Hints are inputs to assessment, not authority to expand permissions or budgets.
- Routing modes: `off` preserves existing behavior; `shadow` records recommendations while executing the configured baseline; `rules` uses a fixed policy; `adaptive` uses a validated learned policy.
- Proposed CLI: `anvil route <run.json>` previews recommendations without implementations; any model-assisted assessment must be explicitly enabled and budgeted. `anvil routing report <run-dir>` explains decisions and outcomes. `anvil routing train --repo <path>` creates a candidate policy; promotion and rollback select immutable policy versions.
- Prefer a new versioned configuration contract for routing, while preserving readers for version 1. Decide and document the ticket-schema version alongside runtime validation. Old run ledgers remain readable without destructive migration.

Persist these records separately from the worker's acceptance JSON:

| Record | Required evidence |
| --- | --- |
| Assessment | Ticket/input digest, accepted base SHA, feature version, scope, dependencies, uncertainty, risk, estimated complexity, confidence, evidence, assessor usage |
| Decision | Run/task/attempt/role, eligible profiles, chosen profile, explicit override or rationale, policy/history version, reserved budget |
| Invocation | Requested and reported model/effort, CLI version, timestamps, exit/timeout status, artifact paths, normalized usage with provenance |
| Outcome | Accepted/rejected/blocked/interrupted, exact reviewed/verified/integrated SHAs, findings, check outcomes, retries, failure category, total cost and duration |
| Evaluation | Observed suitability, confidence, comparison cohort, missing evidence, candidate policy recommendation |

Requested settings and reported settings are different fields. Missing reported model/effort remains unknown. A usage parse failure does not turn valid implementation evidence into a failed ticket, but it makes the sample ineligible for cost-sensitive promotion where complete accounting is required.

## PR 0: Publish ticket lifecycle status

Ship this before model routing. Anvil already records execution phases internally, but its ticket input contract has no status and the source ticket is not updated. Users should see progress on the tickets they use, without locating a separate run ledger.

### User-visible lifecycle

| Ticket status | Authoritative trigger |
| --- | --- |
| `todo` | Newly prepared ticket, not claimed |
| `in_progress` | Coordinator persists an owned attempt before launching implementation |
| `ready_for_review` | Validated worker result and committed candidate enter the review queue |
| `in_review` | Independent review of the prepared integration revision starts |
| `verifying` | Review approves that revision; checks/integration are still pending |
| `done` | Required evidence, independent review, and passing checks apply to the exact integrated revision, the managed branch advances, and the ledger records completion |
| `blocked` | A required decision or dependency prevents progress, or review requests changes without an available retry |
| `failed` | Terminal execution/check failure, including exhausted retries |
| `interrupted` | Cancellation or stopped run interrupts an owned attempt |

When bounded retries arrive in PR 4, a ticket returns to `in_progress` with a new attempt ID and retains its earlier findings/history. Unclaimed dependents remain `todo` with a waiting reason when prerequisites are unfinished; never report that they started. `ready_for_review` means queued for Anvil's independent review, not an implemented human approval pause. `done` means verified local integration under today's contract, not GitHub merge, deployment, or external publication.

### Local ticket publication first

Extend the ticket schema and matching runtime validation with a separate execution-metadata object containing status, run/attempt IDs, worker, timestamps, latest reason, evidence location, and relevant commit IDs. Newly prepared Anvil tickets should expose `todo`. Keep objectives and acceptance criteria separate from mutable metadata. Add model/profile and cost summaries when PRs 1–2 supply them. Existing version-1 documents remain readable.

Provide an explicitly configured writable ticket source. The first sink updates the canonical local JSON ticket document by stable ticket ID, including documents containing multiple tickets. It must update the actual ticket status, not only another report. The immutable run input snapshot and SQLite ledger remain authoritative for execution; a user editing a displayed status cannot bypass acceptance or release dependencies. Do not silently treat a displayed `done` from an older run as verified completion in a new run. Keep the existing fresh-run semantics until deliberate continuation is implemented.

Preserve all objective fields and user changes. Record an expected source-content fingerprint and publish through a locked read/compare/write with atomic replacement; on unexpected concurrent edits, leave the file intact and report a synchronization conflict. Revalidate the current document before changing only owned metadata. Never replace a source file with the stale immutable run snapshot. Refuse symlink/path substitution and ambiguous or duplicate IDs.

Treat the canonical status destination as a control file: workers cannot own or mutate it, and status writes must not be included in implementation commits or invalidate the revision being reviewed. For ticket documents inside the application repository, explicitly handle generated metadata-only dirtiness in clean-checkout validation; verify it differs only in owned status fields and preserve every other dirty-file check. Test this design against the actual worktree/branch checks before shipping; do not broadly ignore a ticket file or automatically commit its contents. Status publication is single-writer coordinator work, including in mixed worker pools.

### Reliable synchronization

Persist a status-publication intent in the same SQLite transaction as its corresponding execution event. Use a monotonically ordered event identifier and run/attempt identity for idempotence. Drain this outbox after the transaction commits; a file or future tracker write is never part of the Git acceptance decision. Late events from an old attempt/run cannot overwrite newer ticket status. Bind a destination to its active run under an explicit lock; replacing that binding requires an explicit new-run or reconciliation operation.

If publication fails, retain the pending intent and surface `status_sync_pending` or a conflict in progress and the final report. Never claim the source ticket was updated unless publication succeeded. An accepted ticket remains accepted even if its status sink is unavailable. Use bounded publication retries. A proposed `anvil tickets sync <run-dir>` reconciles publication without rerunning implementation or advancing Git; this is separate from execution recovery. Detect interruption at publication boundaries and safely replay pending events. After an unclean supervisor death, report an unreconciled/stale run rather than assuming persisted `in_progress` means a live worker or relaunching it.

Keep source references and a status-sink boundary ready for Markdown and issue trackers, but deliver local JSON first. External tracker integration requires an explicit source mapping and destination write configuration; do not infer a GitHub issue from a ticket label. Do not create comments or close external issues as part of the local status release. Later sinks must map the same lifecycle and keep publication separate from acceptance.

Acceptance: source tickets visibly change as serial and parallel work progresses; several updates to one JSON file cannot lose one another; review/check failures never publish `done`; stale events, retry transitions, duplicate delivery, source edits, filesystem errors, and interrupted publication preserve correctness. Confirm exact-revision gates, source-checkout cleanliness rules, immutable snapshots, and historical status readers remain valid. Add an end-to-end fake-process test observing intermediate statuses while workers/reviewers are deliberately held at controlled barriers. Update ticket examples, preparation guidance, CLI documentation, and the entry skill so status appears in newly prepared tickets and existing users can opt into synchronization.

## PR 1: Explicit profiles and provider support

Add profile contracts and defaults to configuration, matching JSON schemas, adapter invocation options, and doctor diagnostics. Resolve precedence as ticket override, routing decision, then worker/run default. Legacy configuration continues inheriting CLI defaults. A profile must be compatible with its selected worker; top-level reviewer behavior remains compatible when no review profile is supplied.

Use fresh invocation configuration per attempt rather than mutable shared runner settings. Verify actual supported flags and model/effort combinations with current provider documentation and installed CLIs. Help probes establish flag support, not account entitlement; launch failures must be classified and must not cause an unrequested fallback.

Acceptance: fake executables capture exact intended arguments for both providers and both roles; invalid combinations fail before claims; parallel tasks cannot leak settings into one another; legacy serial and mixed-pool tests remain green.

## PR 2: Usage, provenance, and outcome accounting

Add a provider-neutral invocation envelope without weakening strict worker/reviewer result validation. Normalize token categories, cache usage, elapsed time, and provider-reported cost where available; retain original streams. Explicitly distinguish measured charges, price-based estimates, and unavailable cost. Subscription usage is not equivalent to cash billed per invocation. Version any configured price table and avoid double-counting provider totals or repeated usage events.

Record failed invocations as well as successes. Distinguish implementation defects from authentication, rate limits, infrastructure, harness protocol errors, baseline failures, integration conflicts, missing requirements, and cancellation. Unknown causes remain unknown rather than being labeled insufficient reasoning.

Acceptance: fixtures cover both providers, multiple usage events, missing/partial usage, cache accounting, errors and trailers, and no double counting. Reports show the full ticket cost including review and all attempts. Old reports remain readable. No learning or automatic selection yet.

## PR 3: Assessment and explainable initial routing

Start with deterministic features and rules: ticket scope, acceptance criteria, dependency structure, affected components, test availability, interface/data migration impact, uncertainty, and configured risk categories. Criterion count alone must not determine difficulty. Analyze a bounded snapshot of relevant repository context at the accepted base; record what was inspected and what is unknown.

If rules have low confidence, choose a conservative configured profile or mark the ticket as needing clarification. Optionally add a bounded, read-only model assessor only where its expected value justifies its cost. It must follow the same managed process limits and cannot mutate the repository. Anvil validates its structured advice against trusted policy.

Ship shadow mode before rules mode. Show the chosen profile, rationale, uncertainty, and any budget constraint. Ticket shape and missing requirements may justify decomposition advice, but routing does not silently create or enlarge the backlog.

Acceptance: reproducible assessment fixtures, conservative handling of ambiguous/high-risk tickets, explicit override precedence, compatible-worker scheduling, bounded assessor failures, and identical shadow-mode execution to the baseline.

## PR 4: Bounded retries, escalation, and budget reservations

Implement the live-run retry subset of the recovery milestone before automatic escalation. Add schema versioning and a one-to-many attempts model, unique attempt IDs, explicit retry transitions, and rejection of stale results. Preserve previous attempts and artifact paths. Full restart/resume remains separate; interruption still terminates the run safely.

Initially permit one configured escalation after a remediable implementation rejection or attributable verification failure. Do not escalate on authentication failures, protocol incompatibility, unrelated baseline failures, ambiguous blockers, or mere reviewer process failure. Retry from a fresh worktree at the latest accepted base, with prior findings supplied as evidence. Initial implementation does not reuse unverified patches automatically.

Retain the ticket's worker slot and resource reservations during escalation. Keep the first escalation path within that worker's agent; changing providers during retry is deferred. Other workers may continue only while ownership and integration rules remain valid. Exhausted or non-retryable failures retain the existing stop-and-cancel behavior.

The coordinator atomically reserves run/ticket budget before dispatching assessment, work, and review; reconcile reservations against reported usage and do not release an in-flight reservation early. Bound attempts, processes, duration, and supported turn/token controls. Currency estimates are soft ceilings unless the provider exposes enforceable caps; unknown-cost profiles must be rejected when a strict monetary cap is requested. Never promise an exact dollar ceiling from delayed usage reports.

Acceptance: simultaneous dispatch cannot over-reserve; stale attempts cannot integrate; successful escalation passes fresh exact-revision review/checks; exhausted budgets preserve accepted work; cancellation joins all processes; unknown costs and reviewer budget needs are handled explicitly. Exercise serial and mixed pools, including stale-base integration.

## PR 5: Retrospective evaluation and persistent learning

Produce a deterministic post-ticket evaluation from recorded evidence. Report observed sufficiency, rejection, escalation, or insufficient evidence. Do not declare that a model was optimal simply because it succeeded. A successful escalation is useful evidence but not proof that model choice caused the improvement: the retry also received new findings.

Build a separate versioned repository history store, imported idempotently from finalized run records using run/attempt/role identifiers. Keep original ledgers authoritative. Use a locked, transactional import and atomic immutable policy publication. Worker threads never write history. Interrupted and infrastructure-failed samples remain visible but are not treated as model-quality failures. Account for completed work canceled by another ticket separately.

Begin learning with interpretable cohort statistics rather than an additional optimizer model: comparable task features, acceptance rate, first-pass success, escalation rate, cost per accepted ticket, and latency. Track uncertainty and minimum sample requirements. Separate model versions, CLI versions, policy versions, and materially different repositories/workloads. Mark sparse or incomparable data as insufficient evidence.

Train candidate routing thresholds from completed runs and evaluate on held-out tickets. Explicitly report selection bias: historical routing alone cannot establish that unchosen models would have succeeded. Add bounded opt-in comparisons on low-risk benchmark tickets to obtain comparative evidence, preserving isolated worktrees and all acceptance gates. Never run every model on every production ticket by default.

Acceptance: repeat imports do not duplicate data; incomplete records remain incomplete; policies are reproducible and reversible; sparse histories retain the baseline; all failed-attempt costs count in aggregate cost per accepted ticket. No candidate becomes active merely by being trained.

## PR 6: Controlled adaptive rollout and documentation

Progress from off to shadow, fixed rules, recommended learned policies, and finally opt-in automatic promotion for subsequent runs. Automatic promotion requires configured minimum evidence and held-out quality/cost gates. No policy changes mid-run. Roll back to the frozen baseline if measured quality deteriorates; keep the rejected policy and its evaluation for inspection.

Before a live pilot, define the benchmark, minimum sample size, confidence method, allowed quality/completion regression, required cost improvement, and latency limits. Do not invent a universal acceptable quality-loss percentage. Include independent defect-oriented review or hidden acceptance tests where practical so the learner cannot optimize merely for an easy visible check suite. Longer-term escaped defects require explicit feedback linked to an accepted ticket; do not claim to detect them automatically.

Use bounded live trials with both Codex and Claude after deterministic tests pass. Report empirical savings and uncertainty; do not promise a savings percentage in advance. Live calls and comparative experiments require an explicit pilot budget and eligible model list.

Update README, entry skill and workflow reference, adapter documentation, examples, and roadmap to reflect only shipped capabilities. The entry skill should explain routing decisions and diagnostics; the runtime remains authoritative.

Acceptance: end-to-end trials demonstrate assessment, applied selection, implementation, recorded evaluation, and a justified later policy change. The full regression suite, schema/example checks, package checks, and CI pass. Published validation separates fake-process coverage from actual model evidence.

## Recommended delivery order

PR 0 (ticket status publication) -> PR 1 -> PR 2 -> PR 3 -> PR 4 -> PR 5 -> PR 6.

Ticket lifecycle visibility ships first in PR 0. The first adaptive-routing milestone is PRs 1–3: explicit model/effort control, trustworthy measurement, and explainable routing. The complete five-step requirement is delivered only after PRs 5–6 enable evidence-backed adaptation. PR 4 supplies bounded correction when an initial selection proves insufficient and must coordinate with the existing recovery roadmap.

Do not block initial profiles or measurement on full crash recovery, cross-project learning, native skill invocation, or an issue tracker. Do not call the initial routing release self-learning.
