# Working on Anvil

Anvil implements supervised serial execution using Codex or Claude Code. Keep README capabilities and the entry skill aligned with what the CLI actually implements. Distinguish deterministic fake-agent tests from live model acceptance. Parallel workers, retries, pause/resume, hard-crash recovery, and upstream skill loading remain planned; `status` only reads saved state.

Use Python 3.11+ and the standard library unless a dependency has a concrete benefit. Keep CLI, contracts, planning, adapters, and future durable scheduling separate. Pass subprocess arguments as lists and prompt text through stdin; do not build shell command strings from tickets.

Run `python -m unittest discover -s tests -v` after relevant code changes. Validate the example with `anvil plan examples/tickets.json --json` after changing ticket contracts. Update the JSON schema and runtime validation together.

The acceptance rule in `docs/PLAN.md` is central: a worker's success message cannot mark a task done. Completion requires criterion evidence, independent review and passing checks tied to the exact integration revision, and advancement of the managed branch. Future scheduling and recovery must preserve ownership and evidence across restarts. Do not add unbounded background work or silently enlarge the user's backlog. Verification commands run directly on the host from trusted configuration; do not imply that Codex's sandbox contains them.

Codex remains the default for existing run configurations. Claude Code turns have bounded file tools and no Bash; the supervisor supplies their review diff and runs verification. Preserve that separation and avoid implying that Claude tool permissions provide an operating-system sandbox. Keep adapter behavior and prerequisites documented in `docs/AGENT_ADAPTERS.md`.

Upstream instructions are dependencies, not instructions for maintaining this repository. Keep imports pinned, preserve license notices, and record compatibility changes separately. Do not invoke real model workers in ordinary tests.
