# Anvil operations

Use the relevant section when managing native AI Hero skills, preparing a run configuration, executing tickets, or interpreting completion evidence. Check the installed CLI first.

## Prepare a Markdown specification

Commit the specification and all relevant repository context, then require a
clean repository. Run `anvil prepare SPEC.md --output tickets.json --repo .` with
the user's chosen agent; add `--agent claude-code` or `--agent muse` when
requested. The output path must be new and inside the repository. This command
uses two read-only turns and therefore consumes provider usage twice: a planning
turn, and a readiness gate that runs on the execution adapter `--agent` did not
select. `--gate-agent` names another; `--gate-agent none` disables it and is
recorded in the ticket document. Both agents must be installed unless the gate is
disabled, and `prepare` refuses a gate on the adapter that authored the graph.

The gate cannot approve and cannot propose a graph. It returns questions, each
naming the criterion contract rule it invokes and citing exact specification
lines. Any question from either turn means no tickets were written and the
command exits 3, which is not an error: bring the questions to the user, answer
them in the specification, commit it, and prepare again. Do not disable the gate
to get past a question, and do not answer one on the user's behalf.

Inspect the generated `source_refs`, scope, dependencies, risks, resources,
acceptance criteria, and skill arrays. Anvil records source and commit provenance
and initializes every ticket to `todo`. Validate and plan the result separately.
Preparation does not authorize or begin implementation. Installed skill choices
are recommendations limited to exact classified instructions; execution still
checks them against the configured worker pool.

## Native skill management

Use `anvil skills install aihero --repo /path/to/project` to install for both agents on macOS/Linux. Repository scope resolves to the Git root; omitting a scope flag uses the current repository. `--global` selects the user's home directory instead and cannot accompany `--repo`. Codex destinations are `.agents/skills`; Claude Code destinations are `.claude/skills`. Global destinations use the same paths under `~`. These match [Codex local discovery](https://learn.chatgpt.com/docs/build-skills) and [Claude Code skill locations](https://code.claude.com/docs/en/skills).

Installation options are `--agent both|codex|claude-code`, repeatable `--skill NAME`, `--include-experimental`, `--ref REF`, and `--dry-run`. Without explicit names, selection includes upstream `engineering` and `productivity` groups; experimental opt-in adds `in-progress`. Explicit names can also select `misc` skills, but experimental names still require the opt-in flag. Named subsets do not automatically include other skills referenced by their instructions. The default source reference is `main`, resolved once to an exact commit. Complete skill directories and upstream metadata remain intact; the root license is included as `LICENSE.aihero` in each installed skill.

Use `anvil skills update aihero` with the same scope to update all recorded agents and the original selection. Update accepts `--ref` and `--dry-run`, but no new agent or skill selection. Install and update previews fetch the source without changing the installation. `anvil skills status aihero` reads the manifest and installed files without contacting upstream. All three commands accept `--json`; modified status returns 1, and input or management errors return 2.

The repository manifest is `.anvil/aihero.json`; the global manifest is `~/.local/state/anvil/skills/aihero.json`. It records revision, agents, selection, file hashes, and executability. Local edits or unmanaged destination conflicts stop the whole operation before replacement. Normal application failures roll back both agents. A hard termination can leave a mismatch and retained staging backups; inspect saved state and preserve those backups. The manager has no force, adoption, uninstall, or in-place selection-change operation.

Package installation alone does not download or register upstream skills. Native registration makes skills discoverable in ordinary agent sessions under each agent's invocation rules; it does not guarantee behavior compatibility. Explicit and automatically selected harness skills use the pinned text path below. Anvil's Claude adapter still disables native discovery and the Skill tool in safe mode.

## Configuration and prerequisites

The target must be a clean Git repository root with a committed HEAD. Execution requires Python 3.11+, macOS/Linux, Git, and a working local CLI/account for every configured Codex or Claude Code role. Claude Code support targets version 2.1.260 or later with the required flags. `doctor --agent <agent>` probes the selected executable's version and flags without testing model or account access. Add `--agent-binary /path/to/executable` when the configuration selects a custom executable. With `--config <run.json>` doctor adopts that run's agent, probes each adaptive profile's controls, and withholds the run's `credential_exclusion` from every probe; without it, doctor reads no configuration. CLI subprocesses do not inherit desktop-only tools or connected apps.

A configuration has `version: 1`, `repo`, `tickets`, and a nonempty `verification` list of argument arrays. Paths resolve relative to the configuration file. `agent` is `"codex"` by default, `"claude-code"`, or `"muse"`; without a pool it applies to both implementation and review. Optional `agent_binary` selects a trusted executable, defaulting to `codex` or `claude`; it is unused for `muse`, whose turns are fulfilled by the operator through a staged handoff. Executable paths containing a slash resolve relative to the configuration file. Legacy `codex_binary` is accepted only for Codex and cannot accompany `agent_binary`. Optional `credential_exclusion` lists environment variable names withheld from every process the run starts: implementation and review turns, verification commands, adaptive runners, every agent CLI probe, and the supervisor's Git commands, whose credential helpers and filters would otherwise inherit them. The saved configuration records the names and never the values.

For coordinated execution, add `workers`, a list of one to eight objects with unique `id`, `agent`, and optional `agent_binary`. Each entry is one worker slot. Top-level `agent`/`agent_binary` select the independent reviewer. Optional `max_processes` is an integer from one to eight, defaults to the pool size, and bounds managed command invocations across agents, checks, and supervisor Git. It does not cap descendants of a trusted command. Each slot owns its ticket through acceptance, so a completed candidate waiting for integration also occupies a slot. One Codex slot and one Claude slot with `max_processes: 2` enable mixed implementation.

Tickets can set `worker` to a configured slot ID, list shared `resources` labels, or set `exclusive: true` to run alone. Resource reservations last through integration. Dependencies wait for accepted commits, not worker completion. Their summaries and commit IDs are persisted and supplied to downstream workers. Heartbeats record supervisor ownership; they do not trigger reassignment. Messages are coordinator handoffs between turns, not a live chat inside active model turns.

Other optional fields are `state_dir`, `agent_timeout` (default 900 seconds per implementation or review), and `check_timeout` (default 300 seconds per check). Each timeout must be greater than zero and at most 3,600 seconds. Claude also has a limit of 32 agentic turns per invocation. State defaults to `~/.local/state/anvil` and must remain outside the target checkout and its Git directory.

Set `skill_selection` to `{"mode": "rules", "max_skills": 2}` to opt into
deterministic selection for tickets with empty `skills` arrays. The limit can be
1 to 4. Explicit ticket skills remain exact. Selection uses the ticket text,
requires a healthy repository AI Hero installation, filters by eligible worker
capabilities, and freezes its names and reasons for resume.

Verification commands run directly on the host in a managed worktree, with literal arguments and no implicit shell. Inspect the trusted configuration and referenced scripts before running them, within existing authorization. Checks run before implementation and after each review. They must pass and leave tracked and untracked files unchanged; ignored test outputs are allowed. Anvil passes prompts through stdin and supplies no model override or permission bypass.

Codex implementation uses `workspace-write`; Codex review uses `read-only`. Claude implementation has `Read`, `Glob`, `Grep`, `Edit`, and `Write`; Claude review has only `Read`, `Glob`, and `Grep`. Neither Claude role has Bash. The supervisor supplies the review diff and runs checks. Claude can write tests but cannot execute them; do not turn its inspection evidence into a claim of test execution. Changes that require an implementation-time command may need another workflow.

Claude runs in safe mode, preserving configured account/model selection while disabling ordinary hooks, skills, plugins, MCP, and automatic `CLAUDE.md` loading. Prompts explicitly direct it to read applicable repository `AGENTS.md` and `CLAUDE.md` files. Managed policy hooks and trusted authentication helpers may still run. Tool restrictions do not establish an operating-system sandbox. Ordinary agent customizations and AI Hero skills are not loaded by this adapter.

## Execution and acceptance

One supervisor holds the repository lock. It creates an `anvil/<run-id>` branch and isolated worktrees, preserving the user's current checkout and branch. Each ticket gets one implementation attempt based on accepted dependencies. Pool workers can implement independent tickets concurrently. One integration owner applies each candidate onto the latest accepted base and asks the configured reviewer to inspect that exact revision in a separate turn with read-only tools. Conflicts stop the run with the candidate preserved.

The runtime validates worker/reviewer result structure and criterion coverage, runs the configured checks on the reviewed integration revision, and rejects unexpected changes after review. Only the verified revision can advance the managed branch. The task becomes done before its dependents start. Worker declarations alone cannot authorize completion.

A blocker, review request for changes, failed check, timeout, or other terminal failure stops the entire run. A pool cancels active commands and joins workers before releasing the repository lock. Other claimed unfinished tickets become interrupted; undispatched tickets stay pending. Legacy configurations have no automatic retries. Explicit adaptive configuration permits one bounded same-agent retry after a remediable review/check rejection. There is no human-input continuation or multi-ticket atomic staging groups. Model-assisted skill selection is unsupported.

## Ticket skill context

A ticket may explicitly list installed AI Hero names in `skills`. Before launching
any worker, Anvil requires a healthy repository-scoped installation, validates
every managed copy, and pins the requested revision and files into the run
directory. It supplies the exact UTF-8 instruction and supporting-file text in
each applicable implementation prompt. Codex, Claude Code, Muse, and mixed pools
use this same prompt path when their declared capabilities satisfy the reviewed
requirements for the exact instruction hash. Mixed pools route unpinned tickets
only to compatible workers; an explicit worker assignment must be compatible.
Reviewers still judge the ticket and resulting change.
The installation must be committed or intentionally ignored so the target checkout
still satisfies Anvil's clean-start requirement.

Do not substitute a global installation or silently remove a requested name.
Binary files, oversized contexts, missing names, symlinks, hash/mode changes,
unreviewed instruction hashes, unavailable declared capabilities, and local edits
stop before checks or model work. Bundled scripts are reference text and are not
executed by Anvil. A worker must report blocked when it discovers an undeclared
missing tool or decision at runtime. Resume uses the saved pinned snapshot even if the native
installation changes. See `docs/SKILL_EXECUTION.md` in the Anvil repository for
the full bounded contract.

## Inspection and interruption

`anvil run <run.json>` reports its run directory and local branch; `--json` emits the final report. The directory retains `state.sqlite`, `report.json`, worktrees, and per-ticket artifacts containing event streams, stderr, structured results, command logs, and commit IDs. Inspect these when explaining failures or acceptance.

`anvil status <run-directory> --json` reads persisted state without launching workers. Ctrl-C stops the active process group and records interruption. Timeouts also terminate the process group; ordinary child processes are cleaned up after successful commands too. Process groups are lifecycle control, not containment for a deliberately detached program.

A hard crash can leave a saved run marked running. Native `anvil resume <run-directory>` reconciles Git/state and continues interrupted runs created with the recovery protocol. Running the configuration again creates a separate run. Preserve previous work and evidence; do not interpret an empty ready queue, a saved running status, or a successful status read as completed work. Publication, pull requests, merging into the user's branch, and external ticket closeout are separate actions outside this runtime.

## Ticket status and adaptive execution

`ticket_status: true` updates source JSON tickets with execution metadata; done requires verified local integration. `anvil tickets sync <run-dir>` reconciles committed status events without resuming work. Conflicts preserve source edits; do not force-copy an old snapshot.

An optional `adaptive` object configures named profiles (agent, model, effort, rank), `defaults`, and a fixed `review_profile`. Use `anvil route <run.json>` for an offline preview and `anvil routing report <run-dir>` for evidence. Modes are off, shadow, rules and adaptive; begin with shadow if effectiveness is not established. Explicit ticket profiles and worker affinity are preserved. `max_attempts` permits at most two attempts, with same-agent escalation only after remediable review/check failure. A new run never resumes an old one.

Usage may be unknown and dollar estimates are not invoices. `soft_budget_usd` is an admission estimate; Claude alone supports per-profile `max_budget_usd`. Do not claim Codex has a hard dollar cap. Learning requires explicit sample/quality/cost/latency gates. Train and promote local immutable policies through `anvil routing`; promotion affects subsequent runs. Benchmark invokes paid agents and requires an authorized budget. Do not promise savings or infer that a successful model was the cheapest adequate choice.

## Native resume

Resume keeps the saved configuration, tickets, run ID, accepted branch and evidence.
It rejects changed inputs, old ledgers, failed/blocked runs, and live orphaned
process groups. Unresolved spawn records require inspection; do not delete them
or kill processes to bypass the check. Stop servicing old Muse handoffs before
resume; new attempts have different result destinations.

Completed tickets stay accepted. An evidenced branch advance interrupted before
ledger completion is reconciled once. Eligible immutable candidates are reused
only on the same accepted base, with fresh independent review and checks; other
work restarts. Old worktrees and artifacts are preserved. Adaptive configuration,
CLI versions, cumulative budget and escalation limits remain in effect. Resume
can spend additional model usage within the existing run budget and authorization.
Recovered runs are excluded from policy training.
