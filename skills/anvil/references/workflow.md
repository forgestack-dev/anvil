# Serial execution workflow

Use this reference when preparing a run configuration, executing tickets, or interpreting completion evidence. Check the installed CLI first; this describes the serial milestone.

## Configuration and prerequisites

The target must be a clean Git repository root with a committed HEAD. Serial execution requires Python 3.11+, macOS/Linux, Git, and a working local CLI/account for Codex or Claude Code. Claude Code support targets version 2.1.260 or later with the required flags. `doctor --agent <agent>` probes the selected executable's version and flags without testing model or account access. Add `--agent-binary /path/to/executable` when the configuration selects a custom executable; `doctor` does not read the configuration. CLI subprocesses do not inherit desktop-only tools or connected apps.

A configuration has `version: 1`, `repo`, `tickets`, and a nonempty `verification` list of argument arrays. Paths resolve relative to the configuration file. `agent` is `"codex"` by default or `"claude-code"`; one selection applies to both implementation and review. Optional `agent_binary` selects a trusted executable, defaulting to `codex` or `claude`. Executable paths containing a slash resolve relative to the configuration file. Legacy `codex_binary` is accepted only for Codex and cannot accompany `agent_binary`.

Other optional fields are `state_dir`, `agent_timeout` (default 900 seconds per implementation or review), and `check_timeout` (default 300 seconds per check). Each timeout must be greater than zero and at most 3,600 seconds. Claude also has a limit of 32 agentic turns per invocation. State defaults to `~/.local/state/anvil` and must remain outside the target checkout and its Git directory.

Verification commands run directly on the host in a managed worktree, with literal arguments and no implicit shell. Inspect the trusted configuration and referenced scripts before running them, within existing authorization. Checks run before implementation and after each review. They must pass and leave tracked and untracked files unchanged; ignored test outputs are allowed. Anvil passes prompts through stdin and supplies no model override or permission bypass.

Codex implementation uses `workspace-write`; Codex review uses `read-only`. Claude implementation has `Read`, `Glob`, `Grep`, `Edit`, and `Write`; Claude review has only `Read`, `Glob`, and `Grep`. Neither Claude role has Bash. The supervisor supplies the review diff and runs checks. Claude can write tests but cannot execute them; do not turn its inspection evidence into a claim of test execution. Changes that require an implementation-time command may need another workflow.

Claude runs in safe mode, preserving configured account/model selection while disabling ordinary hooks, skills, plugins, MCP, and automatic `CLAUDE.md` loading. Prompts explicitly direct it to read applicable repository `AGENTS.md` and `CLAUDE.md` files. Managed policy hooks and trusted authentication helpers may still run. Tool restrictions do not establish an operating-system sandbox. Ordinary agent customizations and AI Hero skills are not loaded by this adapter.

## Execution and acceptance

One supervisor holds the repository lock. It creates an `anvil/<run-id>` branch and isolated worktrees, preserving the user's current checkout and branch. Each ticket gets one implementation attempt based on completed dependencies. The supervisor commits a complete candidate, prepares its integration revision, and asks the selected agent to review the actual diff against every acceptance criterion in a separate turn with read-only tools.

The runtime validates worker/reviewer result structure and criterion coverage, runs the configured checks on the reviewed integration revision, and rejects unexpected changes after review. Only the verified revision can advance the managed branch. The task becomes done before its dependents start. Worker declarations alone cannot authorize completion.

A blocker, review request for changes, failed check, timeout, or other terminal failure stops the entire serial run. Remaining independent tickets are also left pending. There are no automatic retries, parallel workers, human-input continuation, wide-refactor staging groups, or upstream skill resolution. Nonempty `skills` requests are rejected before execution; do not describe catalog skills as invoked.

## Inspection and interruption

`anvil run <run.json>` reports its run directory and local branch; `--json` emits the final report. The directory retains `state.sqlite`, `report.json`, worktrees, and per-ticket artifacts containing event streams, stderr, structured results, command logs, and commit IDs. Inspect these when explaining failures or acceptance.

`anvil status <run-directory> --json` reads persisted state without launching workers. Ctrl-C stops the active process group and records interruption. Timeouts also terminate the process group; ordinary child processes are cleaned up after successful commands too. Process groups are lifecycle control, not containment for a deliberately detached program.

A hard crash can leave a saved run marked running. Persistence enables inspection but does not yet implement resume or Git/state reconciliation. Running the configuration again creates a separate run. Preserve previous work and evidence; do not interpret an empty ready queue, a saved running status, or a successful status read as completed work. Publication, pull requests, merging into the user's branch, and external ticket closeout are separate actions outside this runtime.
