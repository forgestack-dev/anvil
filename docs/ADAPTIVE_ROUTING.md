# Ticket status and adaptive routing

These features are opt-in additions to version-1 run and ticket documents. Existing run configurations retain their original behavior. Run ledgers created by this release use schema version 2; historical ledgers remain readable. No execution resume or crash recovery is added.

## Publish status on local tickets

Add `"ticket_status": true` to your run configuration. Anvil updates each source JSON ticket's `execution` object with status, run/attempt identity, timestamps, worker (in pool runs), evidence directory and available commit IDs. When preparing new tickets, include `"execution": {"status": "todo"}`. Existing tickets do not need that field before the first run.

The visible sequence is `todo`, `in_progress`, `ready_for_review`, `in_review`, `verifying`, and `done`. Terminal alternatives are `blocked`, `failed`, and `interrupted`. A review queue is not a human approval pause. Done means independent review and checks passed for the exact revision advanced on Anvil's local integration branch, not a GitHub merge or deployment.

Only the supervisor publishes statuses. SQLite events form the durable outbox; publication saves prepared bytes and a cursor so an interrupted replacement can be replayed. A failed status write does not undo an accepted implementation. The final run report includes `ticket_publication`; `status_sync_pending` means the ticket document may be stale. Retry publication with:

```sh
anvil tickets sync /path/to/run-directory --json
```

This acquires the repository lock and publishes saved state; it never resumes work. A crashed run may still contain active statuses: reconciliation does not establish that a worker remains alive. An older run cannot overwrite a source now bound to a newer run.

Unexpected source edits are preserved and reported as conflicts. Restore the expected document after inspecting saved publication evidence before retrying; there is no force overwrite. For tracked tickets inside the target checkout, only execution-metadata differences from HEAD may be present at run start. Staged changes or edits to objectives remain errors. Workers cannot change the ticket control file, and generated status changes never enter implementation commits. Anvil does not rewrite Markdown tickets or external trackers in this release.

## Configure profiles

See [adaptive-run.json](../examples/adaptive-run.json). Profile names belong to the project; rank 0, 1, and 2 express the configured escalation order. They do not assert universal model rankings. Choose model identifiers your account supports and verify the model accepts the selected effort. The CLI preflight checks advertised controls and version, not entitlement or every model/effort combination. Invalid launch settings stop execution without silently falling back.

An `adaptive` object requires `profiles`, `defaults` (worker ID or agent name to profile), and `review_profile`. The review profile's agent must match the top-level reviewer. Each profile defines `agent`, `model`, `effort`, and `rank`. A ticket may specify `profile` and `risk` (`low`, `medium`, or `high`). Explicit profile overrides win; worker affinity still applies. A ticket profile requires an adaptive configuration; otherwise execution rejects it before launching work. No compatible worker is a preflight error. All settings are passed to a fresh invocation, with existing tool and permission boundaries retained.

- `off`: execute configured defaults or explicit ticket overrides.
- `shadow`: record recommendations but execute defaults/overrides.
- `rules`: apply deterministic recommendations.
- `adaptive`: apply a compatible validated local policy where evidence allows, otherwise use rules.

Assessment examines ticket text, dependency count, risk signals, and a bounded file/test path inventory at the accepted commit. It records uncertainty and limitations. It does not perform semantic code analysis or call an assessment model. Missing evidence prevents optimistic down-routing. Explicitly high-risk or ambiguous tasks use the highest appropriate configured rank. Preview without model calls:

```sh
anvil route /path/to/run.json --json
anvil routing report /path/to/run-directory --json
```

Codex uses `--model` and `--config model_reasoning_effort=...`; Claude uses `--model` and `--effort`. See the [Codex configuration documentation](https://learn.chatgpt.com/docs/config-file/config-advanced) and [Claude CLI reference](https://code.claude.com/docs/en/cli-reference). Per-model support remains provider-specific.

## Bounded escalation and accounting

`max_attempts` defaults to 1 and permits at most 2. On an independent review rejection or failing integration checks, the coordinator can select the next higher-ranked profile for the same agent. It preserves the slot/resources, saves the rejected attempt, starts a fresh worktree at the latest accepted base, supplies prior findings, and repeats review and verification. A reviewer/check mutation, blocked worker, process/protocol failure, or baseline failure does not trigger escalation. Exhaustion preserves the existing stop-and-cancel behavior. A new run still starts fresh.

`max_invocations` defaults to 100 when adaptive configuration is present. The supervisor reserves worker and reviewer slots together before claiming a ticket. Counts are conservative, including reserved reviews that never run. Agent and verification timeouts continue to apply.

Optional `soft_budget_usd` requires `reserve_usd` on every profile. Reservations include work plus review and remain charged conservatively when usage is unavailable. This is an estimated admission limit, not a hard monetary cap; a single invocation can exceed its reservation. Claude profiles may additionally set `max_budget_usd`, passed to Claude's native print-mode cap. Codex profiles reject that field because the adapter has no corresponding dollar cap.

Invocation artifacts record requested settings, available reported model, CLI version, duration, and normalized usage. Missing reported effort stays unknown. Claude result costs are provider estimates; Codex costs require optional per-million-token prices (`input`, `cached_input`, `cache_write`, `output`, and a `version` string). These are not subscription invoices. Raw streams remain intact. Missing or malformed usage does not change acceptance but remains visible as unknown cost. Reports include failed attempts and reviews, not only successful work.

## Learning and rollout

Finalized adaptive runs import local evidence idempotently into `.git`'s common-directory `anvil-routing` history. Learning does not change the ledger's acceptance result. History errors appear in the final report and can be retried with `anvil routing import <run-directory>`.

Learning uses comparable task cohorts, disjoint input-based training/held-out splits, matching profile catalogs, retry limits, reviewer selection, verification commands and CLI versions, observed acceptance, total cost per accepted ticket, and latency. Repeated identical inputs cannot inflate independent sample counts. Unknown cost, mismatched reported model, injected test runners, and unclassified failures do not justify policy promotion. When the CLI does not report a model, comparison uses the explicitly requested profile and CLI version; actual model identity remains unconfirmed. A successful run demonstrates observed sufficiency, not that the chosen model was optimal. Historical comparisons retain selection bias.

To train, configure `learning` with explicit `min_samples` (at least 5 per profile in each split), `max_quality_loss`, `min_cost_improvement`, and `max_latency_ratio`. Fractions are between 0 and 1. The gate uses conservative 95% Wilson bounds for acceptance and checks costs/latency in both splits. A zero permitted quality loss is intentionally difficult to establish from finite samples. Set thresholds for your workload; the example does not enable automatic training or promotion.

```sh
anvil routing train /path/to/run.json
anvil routing promote /path/to/run.json POLICY_ID
anvil routing rollback /path/to/run.json
```

Policies are content-addressed and bound to those execution conditions. Evidence and policies from earlier accounting versions cannot justify promotion under the new contract. Canceled attempts and failures without an attributable review/check rejection remain insufficient evidence. Promotion changes future runs; a run freezes its loaded policy. `policy` can explicitly pin a validated ID. Rollback clears the active pointer; explicitly pinned configurations must also be changed. Optional `learning.auto_promote: true` trains after completed runs, promotes only policies passing all configured gates, and clears the active pointer when no candidate passes. This uses accumulated evidence rather than a claim of immediate drift detection.

For explicit comparisons on low-risk benchmark fixtures, use a serial config, at least two same-agent profiles, tickets marked `risk: low`, and an aggregate invocation budget:

```sh
anvil routing benchmark /path/to/benchmark-run.json --profile economical --profile standard
```

This starts real model calls. It forces one attempt, fixed profiles and a fixed reviewer, disables automatic promotion, and leaves each comparison on a separate local Anvil branch. It never enables experiments on ordinary tickets automatically. Benchmarks with tight currency limits need provider-enforced caps; the soft budget alone cannot guarantee spend. Full production savings and long-term escaped-defect detection require additional evidence; this release does not claim either.
