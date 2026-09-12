---
name: anvil
description: Prepare ticket graphs, run supervised serial Codex implementation, and inspect saved runs with the ForgeStack Anvil engineering harness. Use for specification or ticket burndown and Anvil operation; check installed capabilities before execution.
---

# Anvil

Use Anvil to turn a defined engineering objective into verifiable tickets and execute them serially. The current runtime supports JSON validation, dependency planning, bounded Codex implementation and independent review, verification, local integration, and saved status. It does not load AI Hero skills, run parallel workers, retry, pause/resume, or close external tickets.

## Prepare or inspect work

Read the supplied spec or tickets and project instructions. Preserve scope, identifiers, and settled decisions. Each ticket needs an objective, dependency IDs, and acceptance criteria. Expose missing requirements instead of inventing a completed specification.

When adapting source material to JSON, use the shipped schema and retain the source relationship in the surrounding plan. Check `anvil --help` and `anvil --version`, then run `anvil validate <tickets.json>` and `anvil plan <tickets.json> --json`. These commands inspect input without implementing it. Dependency waves show possible parallelism; the current executor processes them serially.

If Anvil is unavailable, inspect the checkout or installation instructions supplied by the user. Do not assume a checkout path, install an unrelated package named `anvil`, or register the entry skill in another project automatically.

## Execute authorized work

Read [references/workflow.md](references/workflow.md) before preparing or running a configuration. Verify a clean, committed repository root and inspect its instructions, the ticket scope, the configuration, and every verification command or referenced script. Verification commands execute directly on the host; the worker's Codex sandbox does not contain them. Choose meaningful project checks and keep execution within the user's existing authorization. If a necessary action is outside that authorization, first prepare the concrete configuration for review and explain the specific missing permission; do not ask again for work already authorized.

Confirm the installed CLI exposes `run` and inspect prerequisites with `anvil doctor`. The supported entry point is `anvil run <run.json>`. Nonempty ticket `skills` requests are rejected by this version; report the unavailable capability rather than silently removing an explicit request. Missing decisions, review findings, and terminal failures stop the entire serial run.

Report the saved run directory and managed integration branch. Distinguish implemented, blocked, failed, interrupted, and still-pending work using recorded state. A worker claim is not acceptance: completion requires criterion evidence, approval for the exact integration revision, and passing checks. Local completion does not imply publication, merging into the user's branch, or external ticket closure.

For inspection, use `anvil status <run-directory> --json`. It reads state without resuming. A repeated `run` starts fresh; it does not recover earlier attempts. Preserve failed artifacts and candidate worktrees for inspection. For unsupported parallelism, retries, pause/resume, or recovery, state the installed limitation and avoid substituting an untracked loop.
