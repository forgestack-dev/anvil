---
name: anvil
description: Prepare and inspect a dependency-aware ticket plan for the ForgeStack Anvil engineering harness. Use for specification or ticket burndown planning and Anvil operation; check installed capabilities before attempting execution.
---

# Anvil

Use Anvil to turn a defined engineering objective into a verifiable ticket graph and inspect its dependency waves. The current scaffold supports JSON validation, dry-run planning, and a local Codex prerequisite check. It does not execute tickets, install AI Hero skills, or manage persistent worker loops.

## Prepare the work

Read the supplied spec or tickets and project instructions. Preserve scope, identifiers, and settled decisions. Each ticket needs an objective, dependency IDs, and acceptance criteria. Expose missing requirements instead of inventing a completed specification.

When converting source material to the current JSON format, write the prepared artifact in the user's chosen workspace and retain the source relationship in the surrounding plan. Use the schema and example shipped with Anvil. Do not claim that a new JSON field is supported without checking the current schema.

## Inspect a plan

Check `anvil --help` and the installed version. Run `anvil validate <tickets.json>`, then `anvil plan <tickets.json> --json`. These commands inspect input; they do not authorize or perform the engineering work. Report the dependency waves and any unresolved decisions. Parallel waves do not account for shared resource conflicts.

If Anvil is unavailable, inspect the checkout or installation instructions supplied by the user. Do not assume a local checkout path, download an unrelated package named `anvil`, or register another skill automatically.

For a request to execute, resume, or coordinate workers, check current CLI capabilities. This scaffold has no execution command. State that limit clearly and describe the next implementation milestone; do not substitute an untracked autonomous loop or mark tickets complete.

Read [references/workflow.md](references/workflow.md) when evaluating a proposed execution workflow, AI Hero integration, or what evidence completion requires. Its future design is not a list of currently available commands.
