---
name: anvil
description: Prepare ticket graphs, coordinate Codex, Claude Code, and Muse workers, inspect saved runs, and manage native AI Hero skills with the ForgeStack Anvil engineering harness. Use for specification or ticket burndown and Anvil operation; check installed capabilities before execution.
---

# Anvil

Use Anvil to turn a committed Markdown specification into reviewable tickets, or execute an existing ticket graph serially or with a coordinated Codex/Claude Code/Muse worker pool. The runtime owns preparation provenance, dispatch, shared-resource reservations, independent review, verification, local integration, and saved status. Separate skill-management commands install AI Hero skills for native agent sessions, explicit ticket selection, and opt-in deterministic selection for empty ticket skill arrays. Model-assisted selection, pause commands, and external ticket closure remain unsupported. Native resume supports interrupted runs created with the recovery protocol.

## Prepare or inspect work

Read the supplied spec or tickets and project instructions. Preserve scope, identifiers, and settled decisions. Each ticket needs an objective, dependency IDs, and acceptance criteria. Expose missing requirements instead of inventing a completed specification.

When adapting source material to JSON, use the shipped schema and retain the source relationship in the surrounding plan. Check `anvil --help` and `anvil --version`, then run `anvil validate <tickets.json>` and `anvil plan <tickets.json> --json`. These commands inspect input without implementing it. Dependency waves show possible parallelism; a configured worker pool also respects worker assignments and declared resource reservations.

For a committed Markdown specification, use `anvil prepare <spec.md> --output <tickets.json> --repo <root>` and read the preparation section of [references/workflow.md](references/workflow.md). Preparation uses one provider turn and writes a new file; preserve the user's selected agent and usage constraints. Review the generated scope, source references, dependencies, risks, resources, and skill choices before configuring execution. Never treat successful preparation as authorization to begin the generated work.

If Anvil is unavailable, inspect the checkout or installation instructions supplied by the user. Do not assume a checkout path, install an unrelated package named `anvil`, or register the entry skill in another project automatically.

## Manage native AI Hero skills

For installation, update, or inspection, read the native skill-management section of [references/workflow.md](references/workflow.md). Use `anvil skills install aihero`, `update aihero`, or `status aihero` with the user's repository or global scope. Installation defaults to both agents and stable engineering/productivity skills; preserve requested agent and skill selections, and require explicit experimental opt-in. Updates retain the recorded selection.

Use `--dry-run` when a change preview is requested or useful; it fetches upstream files but leaves the installation unchanged. `skills status` is offline. Report the selected revision, destinations, and any conflicts. Preserve local changes and retained backups when a conflict or interrupted update needs inspection. Do not bypass the manager with forced copying or delete an installation to change its selection without explicit scope to do so.

Repository installation enables explicit ticket `skills` requests and opt-in automatic selection for empty arrays. Execution validates and pins UTF-8 resources, checks their exact instruction hashes against Anvil's reviewed capability registry, then supplies compatible text directly in the worker prompt. It does not prove a compatible skill will succeed. Anvil's Claude worker keeps safe mode and its disabled Skill tool; prompt delivery does not change that boundary. Binary resources, oversized contexts, modified or unclassified instructions, and unavailable declared capabilities fail before model work, and Anvil never executes bundled scripts.

## Execute authorized work

Read [references/workflow.md](references/workflow.md) before preparing or running a configuration. Verify a clean, committed repository root and inspect its instructions, the ticket scope, the configuration, and every verification command or referenced script. Verification commands execute directly on the host. Choose meaningful project checks and keep execution within the user's existing authorization. If a necessary action is outside that authorization, first prepare the concrete configuration for review and explain the specific missing permission; do not ask again for work already authorized.

Confirm the installed CLI exposes `run`. Preserve the user's agent choices; an omitted top-level `agent` defaults to Codex. For mixed execution, use one `workers` pool under one supervisor; top-level `agent` selects its reviewer. Check the installed doctor's `worker_pools_available` capability before preparing pool execution. Probe each distinct configured agent/executable with `anvil doctor --agent <agent>`, passing `--agent-binary` for custom executables. The supported entry point is `anvil run <run.json>`.

Choose dependencies for prerequisite changes, `resources` for known shared work, and `exclusive: true` when a ticket must run alone. Keep the process cap within the user's intended concurrency and account capacity. Do not launch separate Anvil supervisors as a substitute for a pool. Use ticket `skills` only for names already present in a healthy repository-scoped AI Hero installation; read the ticket skill section of [references/workflow.md](references/workflow.md). Missing decisions, review findings, and terminal failures stop the entire run and cancel active peers.

Report the saved run directory and managed integration branch. Distinguish implemented, blocked, failed, interrupted, and still-pending work using recorded state. A worker claim is not acceptance: completion requires criterion evidence, approval for the exact integration revision, and passing checks. Local completion does not imply publication, merging into the user's branch, or external ticket closure.

For inspection, use `anvil status <run-directory> --json`. It reads state without resuming. A repeated `run` starts fresh; it does not recover earlier attempts. Preserve failed artifacts and candidate worktrees for inspection. For authorized continuation of an interrupted run, use `anvil resume <run-directory>` after reading the recovery guidance in [workflow.md](references/workflow.md). General failure retries, pause commands and live worker messaging remain unsupported.

## Ticket status and routing

When preparing JSON tickets, include `"execution": {"status": "todo"}`. Configure `ticket_status: true` when source updates are requested; the runtime owns subsequent status changes. Read [workflow.md](references/workflow.md) for routing, status reconciliation and bounded escalation. Preserve explicitly selected agents, profiles and budgets. Never infer completion from displayed status alone or activate paid benchmarks without authorization.
