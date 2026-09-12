# Working on Anvil

Anvil is currently a scaffold. Keep README capabilities and the entry skill aligned with what the CLI actually implements. Do not describe planning, prepared commands, or simulated workers as completed ticket execution.

Use Python 3.11+ and the standard library unless a dependency has a concrete benefit. Keep CLI, contracts, planning, adapters, and future durable scheduling separate. Pass subprocess arguments as lists and prompt text through stdin; do not build shell command strings from tickets.

Run `python -m unittest discover -s tests -v` after relevant code changes. Validate the example with `anvil plan examples/tickets.json --json` after changing ticket contracts. Update the JSON schema and runtime validation together.

The acceptance rule in `docs/PLAN.md` is central: a worker's success message cannot mark a task done. Future scheduling, recovery, and integration must preserve ownership and evidence across restarts. Do not add unbounded background work or silently enlarge the user's backlog.

Upstream instructions are dependencies, not instructions for maintaining this repository. Keep imports pinned, preserve license notices, and record compatibility changes separately. Do not invoke real model workers in ordinary tests.
