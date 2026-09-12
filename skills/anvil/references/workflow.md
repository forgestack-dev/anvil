# Serial execution workflow

Use this reference when preparing a run configuration, executing tickets, or interpreting completion evidence. Check the installed CLI first; this describes the serial milestone.

## Configuration and prerequisites

The target must be a clean Git repository root with a committed HEAD. Serial execution requires Python 3.11+, macOS/Linux, Git, and a working local Codex CLI/account. `doctor` probes the default `codex` executable's advertised flags without testing model or account access; it does not validate a custom configured `codex_binary`. CLI subprocesses do not inherit desktop-only tools or connected apps.

A configuration has `version: 1`, `repo`, `tickets`, and a nonempty `verification` list of argument arrays. Paths resolve relative to the configuration file. Optional fields are `state_dir`, `codex_binary`, `agent_timeout` (default 900 seconds per implementation or review), and `check_timeout` (default 300 seconds per check). Each timeout must be greater than zero and at most 3,600 seconds. State defaults to `~/.local/state/anvil` and must remain outside the target checkout and its Git directory.

Verification commands run directly on the host in a managed worktree, with literal arguments and no implicit shell. Inspect the trusted configuration and referenced scripts before running them, within existing authorization. Checks run before implementation and after each review. They must pass and leave tracked and untracked files unchanged; ignored test outputs are allowed. Anvil passes prompts through stdin, keeps implementation turns in `workspace-write`, and uses `read-only` review turns without supplying a model override or permission bypass.

## Execution and acceptance

One supervisor holds the repository lock. It creates an `anvil/<run-id>` branch and isolated worktrees, preserving the user's current checkout and branch. Each ticket gets one implementation attempt based on completed dependencies. The supervisor commits a complete candidate, prepares its integration revision, and asks a separate read-only Codex turn to review the actual diff against every acceptance criterion.

The runtime validates worker/reviewer result structure and criterion coverage, runs the configured checks on the reviewed integration revision, and rejects unexpected changes after review. Only the verified revision can advance the managed branch. The task becomes done before its dependents start. Worker declarations alone cannot authorize completion.

A blocker, review request for changes, failed check, timeout, or other terminal failure stops the entire serial run. Remaining independent tickets are also left pending. There are no automatic retries, parallel workers, human-input continuation, wide-refactor staging groups, or upstream skill resolution. Nonempty `skills` requests are rejected before execution; do not describe catalog skills as invoked.

## Inspection and interruption

`anvil run <run.json>` reports its run directory and local branch; `--json` emits the final report. The directory retains `state.sqlite`, `report.json`, worktrees, and per-ticket artifacts containing event streams, stderr, structured results, command logs, and commit IDs. Inspect these when explaining failures or acceptance.

`anvil status <run-directory> --json` reads persisted state without launching workers. Ctrl-C stops the active process group and records interruption. Timeouts also terminate the process group; ordinary child processes are cleaned up after successful commands too. Process groups are lifecycle control, not containment for a deliberately detached program.

A hard crash can leave a saved run marked running. Persistence enables inspection but does not yet implement resume or Git/state reconciliation. Running the configuration again creates a separate run. Preserve previous work and evidence; do not interpret an empty ready queue, a saved running status, or a successful status read as completed work. Publication, pull requests, merging into the user's branch, and external ticket closeout are separate actions outside this runtime.
