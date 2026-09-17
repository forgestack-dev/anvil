# Anvil

**A coding agent cannot mark its own work done.**

Anvil is ForgeStack's engineering harness for taking specifications and tickets
through coding agents to accepted, verified work. Its central rule is that a
worker's success message proves nothing: a ticket is done only when it carries
criterion evidence, an independent review, and passing checks tied to the exact
revision that was integrated, and only after the managed branch advances to it.

Agents are run in parallel because tickets are independent, not as the point.
The point is what happens between a finished turn and a merged change.

## Current status

Anvil executes a JSON ticket graph serially or with a **coordinated pool of Codex, Claude Code, and Muse workers**. Workers implement ready tickets in isolated Git worktrees and never commit, branch, or push. One supervisor owns the SQLite ledger and integration queue, runs the required checks on each change atop the latest accepted branch, independently reviews the changes that pass them, and advances that branch only after acceptance evidence, independent review, and verification pass. Every acceptance decision and its evidence is recorded in an append-only ledger that outlives the run, so what was accepted, on what revision, and on what grounds stays answerable afterwards. Muse turns are fulfilled by the operator running Anvil through a staged handoff rather than a local CLI; see [agent adapters](docs/AGENT_ADAPTERS.md).

Isolated parallel workspaces are widely available now. The separation Anvil adds
is between authorship and acceptance: the agent that wrote a change never
establishes that it is finished, and review runs against the integrated revision
rather than the one the worker reported.

It also validates ticket graphs, prepares committed Markdown specifications as reviewable JSON tickets behind an adversarial readiness gate, previews dependency waves, checks local prerequisites, reads saved run state, serves that state read-only over local HTTP, and installs or updates AI Hero skills in both agents' native directories. Tickets can explicitly select installed AI Hero text skills or opt into deterministic runtime selection; Anvil pins their exact files, checks reviewed tool and interaction requirements, and supplies compatible instructions to Codex, Claude, or Muse implementation turns. A run may name environment variables its work must not see; Anvil withholds them from every process it starts, including the probes it runs and the Git commands the supervisor issues, and records the names without ever recording the values. Native resume continues interrupted runs created by this version. Pause commands, general failure retries, model-assisted skill selection, and issue-tracker closeout remain planned. Package installation does not register skills or modify an application repository automatically.

Prepare a specification before execution:

```sh
anvil prepare SPEC.md --output tickets.json --repo .
anvil validate tickets.json
anvil plan tickets.json --json
```

[examples/SPEC.md](examples/SPEC.md) is a small specification written to the criterion contract: every requirement states how it is decided, the absence requirement names the complete set it holds over, and the headings are the lines a prepared ticket cites in `source_refs`. Copy it into the repository you want worked on and commit it there; a specification must be committed inside its target repository.

Preparation is two read-only turns from the selected Codex, Claude Code, or Muse adapters. One converts the specification into a ticket graph. The second is a gate: it runs on the adapter the first did not use, sees the graph and the specification but not the turn that wrote them, and can only ask questions -- it has no approval verdict, so an empty result records the absence of an objection rather than readiness. `--gate-agent` selects it, and `--gate-agent none` disables it and records that in the ticket document; `prepare` refuses a gate on the adapter that authored the graph.

A question from either turn means no tickets are written, and `anvil prepare` exits `3` rather than the `2` it uses for errors, because a question is not an error. Each names the criterion contract rule it invokes and cites exact specification lines. Answer them by editing and committing the specification, which changes the recorded source hash and makes the next preparation traceably a different input.

Pin both turns to explicit models when the defaults are not what you want. `--model`/`--effort` select the planning turn's and `--gate-model`/`--gate-effort` the gate's; each pair is given together, and `prepare` refuses a gate that matches the planning turn's adapter or its stated model.

```sh
anvil prepare SPEC.md --output tickets.json --repo . \
  --agent claude-code --model claude-opus-5 --effort high \
  --gate-agent codex --gate-model gpt-5-codex --gate-effort medium
```

Both models are then recorded in the ticket document, so the claim that one turn judged another's work is auditable afterwards:

```json
"model": "claude-opus-5",
"gate": {"agent": "codex", "model": "gpt-5-codex", "distinct_adapter": true, "questions": 0}
```


Preparation records the source hash, repository commit, and gate, requires a source reference on every ticket, initializes ticket status to `todo`, and writes the result atomically. It does not start implementation. See [spec preparation](docs/SPEC_PREPARATION.md) and the [criterion contract](docs/CRITERIA.md).

## Install and plan

Requires Python 3.11 or later. Serial execution requires macOS or Linux, Git, and an installed Codex CLI or Claude Code CLI with working account access for CLI-agent turns. Muse turns need no CLI: the operator running Anvil fulfills them through a staged handoff. Claude Code support targets version 2.1.260 or later with the required flags advertised by `doctor`. The runtime has no third-party Python dependencies; installation uses the build tools declared in `pyproject.toml`.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
anvil validate examples/tickets.json
anvil plan examples/tickets.json --json
anvil doctor
anvil doctor --agent claude-code
```

Validation and planning work without an agent CLI. `doctor` checks Git and the selected executable's version and advertised flags; it does not authenticate or make a model request. It defaults to Codex. To probe a custom executable, supply `--agent-binary /path/to/executable`. With `--config <run.json>` it adopts that run's agent, probes each adaptive profile's controls, and withholds the run's `credential_exclusion` from every probe it launches; without it, `doctor` reads no run configuration. The planner's dependency waves describe possible parallelism; actual dispatch also respects available workers and declared shared resources.

To use the CLI directly from a source checkout:

```sh
PYTHONPATH=src python3 -m anvil plan examples/tickets.json
```

## Install AI Hero skills for both agents

After installing Anvil, explicitly install the upstream skills into a repository:

```sh
anvil skills install aihero --repo /path/to/project
anvil skills status aihero --repo /path/to/project
anvil skills update aihero --repo /path/to/project --dry-run
anvil skills update aihero --repo /path/to/project
```

Installation defaults to both Codex and Claude Code, using the upstream
`engineering` and `productivity` groups. Each agent receives the complete selected
skill directories, their supporting files, and `LICENSE.aihero`. Instructions and
upstream metadata are preserved. Use `--agent codex` or `--agent claude-code` for
one agent, repeat `--skill NAME` to select particular skills, and explicitly add
`--include-experimental` for skills from `in-progress`. Explicit selection can
also name skills in `misc`. Named subsets do not automatically include other
skills referenced by their instructions. Skill management supports macOS/Linux;
repository scope uses Git.

Repository scope resolves to the Git root, including when launched from a
subdirectory. Codex discovers `.agents/skills`, while Claude Code discovers
`.claude/skills`. Anvil keeps separate managed copies for the two agents.
[Codex locations](https://learn.chatgpt.com/docs/build-skills),
[Claude Code locations](https://code.claude.com/docs/en/skills).

Omitting a scope flag uses the current repository. Use `--global` instead of
`--repo` for `~/.agents/skills` and `~/.claude/skills`. The installation manifest
is `.anvil/aihero.json` at the repository root, or
`~/.local/state/anvil/skills/aihero.json` for global scope. It records the exact
upstream commit, selected agents and skills, file hashes, and executable flags.

For global Claude Code skills, setting `CLAUDE_CONFIG_DIR` changes the destination
to `$CLAUDE_CONFIG_DIR/skills`. Codex and repository destinations stay the same.
Anvil records custom Claude destinations; use the same configuration directory
for subsequent install, update, and status commands. Changing or unsetting it
after a custom installation produces an error instead of redirecting ownership.
Existing default installations continue to use `~/.claude/skills`; these commands
do not migrate installations between profiles.

Install and update accept `--ref`, defaulting to `main`; the reference is resolved
once to an exact commit before files are downloaded. They also accept `--dry-run`,
which previews changes without changing the installation but still fetches the
upstream source. `status` checks saved files locally without network access.
All three commands accept `--json`; status returns exit code 1 for local
modifications, and input or installation errors return 2.

Updates use the agents and selection recorded at installation. Local edits or
unmanaged destination conflicts abort the operation for both agents. Anvil rolls
back ordinary application errors; a hard termination during file replacement
may require inspection of the saved manifest and retained backups. There is no
force, adoption, uninstall, or in-place agent/selection-change command.

These skills are available to normal Codex and Claude Code sessions under each
agent's discovery and invocation rules. A ticket `skills` array also selects
repository-installed skills for bounded harness execution. Anvil validates and
pins their text resources, then supplies the exact instructions in the worker
prompt so Codex, Claude, Muse, and mixed pools share one path. Binary resources
and oversized contexts fail before model work; bundled scripts are not executed.
The managed installation must not leave the target checkout dirty: commit it or
add its owned paths to the repository's ignore policy intentionally.
See [ticket skill execution](docs/SKILL_EXECUTION.md),
[automatic skill selection](docs/AUTOMATIC_SKILL_SELECTION.md), and
[upstream management](docs/UPSTREAM.md).

## Run tickets

Start from a clean repository root with at least one commit. Include untracked files when checking cleanliness. Anvil reads that committed revision as its baseline and creates separate worktrees plus an `anvil/<run-id>` branch; it does not switch or merge into your current branch. Only one Anvil supervisor can hold the repository lock at a time.

Create a run configuration using [examples/run.json](examples/run.json), [examples/claude-run.json](examples/claude-run.json), and the [configuration schema](schemas/run.schema.json):

```json
{
  "version": 1,
  "repo": "/path/to/project",
  "tickets": "tickets.json",
  "agent": "codex",
  "verification": [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]],
  "agent_timeout": 900,
  "check_timeout": 300
}
```

Paths resolve relative to the configuration file. Use verification commands appropriate to the project: they run once on the baseline and again on every reviewed integration revision. The baseline must pass, and checks must leave tracked files and untracked files unchanged; ignored test output is allowed.

To select reviewed AI Hero workflows for tickets with an empty `skills` array,
install the repository catalog and add `"skill_selection": {"mode": "rules",
"max_skills": 2}`. Explicit ticket selections remain exact. The deterministic
selector filters candidates by the eligible Codex, Claude Code, or Muse workers,
records its reasons, and freezes the result for resume. See the
[automatic selection contract](docs/AUTOMATIC_SKILL_SELECTION.md).

Set `agent` to `"claude-code"` to use Claude Code; omitting it preserves the Codex default. Optional `agent_binary` selects a trusted local executable, defaulting to `codex` or `claude` for the selected agent. Executable paths containing a slash resolve relative to the configuration file. Legacy `codex_binary` remains accepted for Codex configurations, but cannot be combined with `agent_binary` or used with Claude Code.

**Run configurations are trusted executable input.** Each verification entry is an argument array executed directly on the host in the managed worktree. Anvil does not sandbox these commands or interpret shell syntax. Inspect the commands and any scripts they invoke before running a configuration. Codex implementation turns use `workspace-write`; review turns use `read-only`. Claude implementation turns can read and edit files, while review turns have only file-reading and search tools. Claude turns have no Bash tool; the supervisor supplies the review diff and runs the trusted checks. Claude's tool restrictions do not provide an operating-system sandbox. See [agent behavior](docs/AGENT_ADAPTERS.md) for configuration and permission details. Anvil supplies no model override or permission bypass.

Managed Git, agent, and verification commands discard inherited `GIT_*` environment variables so Git uses the managed worktree. Other environment settings are preserved.

`agent_timeout` applies separately to each implementation and review turn; `check_timeout` applies to each verification command. Both must be greater than zero and at most 3,600 seconds. Claude Code also limits agentic turns per invocation, 32 by default; optional `agent_turns` (1 to 1,000) raises or lowers that limit and other adapters ignore it. Optional `credential_exclusion` lists environment variable names withheld from every managed subprocess a run launches: implementation turns, review turns, verification commands, adaptive per-attempt runners, every probe of an agent CLI, including the version and flag probes `anvil doctor` runs and both turns of `anvil prepare --config`, and every Git command the supervisor issues, so credential helpers, askpass programs and clean/smudge filters Git starts do not see them either. Names are recorded in the run's saved configuration; values never are. Do not list the variables the configured agent CLI authenticates with, since its turns would then fail to start; the set is for credentials the work itself has no reason to hold, as in [examples/withheld-credentials-run.json](examples/withheld-credentials-run.json). One exception is documented rather than silent: `anvil skills` resolves its repository with Git before any run configuration exists, so it has no exclusion set to apply. Optional `orientation` names a UTF-8 file of at most 64 KiB whose text is supplied verbatim in implementation and review prompts, so neither role spends turns locating repository context; review is told the text is navigation and never evidence for approving an unchecked criterion; see [observed execution limits](docs/OBSERVED_LIMITS.md). Optional `state_dir` must be outside the target checkout and its Git directory; its default is `~/.local/state/anvil`.

```sh
anvil run /path/to/run.json
anvil status /path/to/saved/run-directory --json
```

`run` prints progress and the saved run directory; add `--json` for a machine-readable final report. Running the same configuration again starts a new run. `status` reads existing state and **does not resume execution**.

For each ticket, Anvil:

1. Implements the ticket in a new worktree based on completed prerequisites.
2. Validates structured acceptance evidence and commits a complete candidate.
3. Prepares the integration revision and runs the configured checks on it. A candidate that fails them is never reviewed.
4. Independently reviews that exact revision in read-only mode, and advances the managed branch only to the reviewed and verified revision.
5. Records completion before starting a dependent ticket.

A worker's success message alone cannot complete a ticket. A blocker, requested review changes, failing check, process timeout, or other terminal failure stops the entire serial run, including independent tickets. Completed work remains on the managed branch; failed candidates, worktrees, logs, and state are preserved for inspection. Ctrl-C terminates the active process group and records interruption. Process groups clean up ordinary child processes; they are not a security boundary against deliberately detached programs.

The saved run directory contains `state.sqlite`, `report.json`, worktrees, and per-ticket artifacts: structured worker/review results, event streams, stderr logs, verification outputs, and associated commit IDs. Workspace and artifact directories use unique attempt IDs; saved attempts map them to the original ticket IDs. A hard crash may leave state recorded as running. Explicit native resume validates process quiescence and reconciles the ledger with the managed branch before continuing; see [recovery](docs/RECOVERY.md).

`state.sqlite` is a write-ahead log database. Its `-wal` and `-shm` sidecars belong to the ledger: copy them with it, because the ledger file alone cannot be opened. Reading a run recreates the shared-memory index when it is absent, so inspecting a run needs a writable directory even though the ledger itself is never modified.

`run` exits with 0 for success, 1 for failure, 2 for invalid input/setup, 3 for blocked work, or 130 for Ctrl-C. `status` exits with 0 when the saved state was read successfully, regardless of the run's recorded outcome. Anvil does not push, open a pull request, merge into a user branch, or close external tickets.

## Coordinated Codex and Claude workers

Add a `workers` pool to a run configuration. For example:

```json
{
  "version": 1,
  "repo": "/path/to/project",
  "tickets": "tickets.json",
  "workers": [
    {"id": "codex-builder", "agent": "codex"},
    {"id": "claude-builder", "agent": "claude-code"}
  ],
  "agent": "codex",
  "max_processes": 2,
  "verification": [["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]]
}
```

Use checks appropriate to the target project. Run this configuration with the
same `anvil run <run.json>` command. Each worker entry is one slot; a pool may
contain one to eight slots, including multiple slots using the same agent.
Each entry can supply its own trusted `agent_binary`. Top-level `agent` and
`agent_binary` select the independent reviewer for every pool candidate. Without
`workers`, that agent still handles both roles in the existing serial mode.

The shipped [pool configuration](examples/parallel-run.json) and
[three-ticket example](examples/parallel-tickets.json) target a fresh sibling
`anvil-demo` repository prepared as in the serial example below. The first two
tickets use different agents; the third depends on both. Running that configuration
launches real model turns; ordinary tests use private fake executables instead.

`max_processes` defaults to the pool size and limits concurrent managed command
invocations across agent turns, review, verification, and supervisor Git. It
does not limit how many descendants a trusted command creates. A slot keeps its
ticket until acceptance, bounding the candidate queue as well as implementations.

Tickets may set `worker` to a configured slot ID. Unassigned tickets use the
first available compatible slot. Shared `resources` labels prevent those tickets
from overlapping through implementation, review, and integration. Set
`exclusive: true` for a ticket that must run without any other ticket in flight.
For example, `"worker": "claude-builder", "resources": ["database-schema"]`.
Dependencies are dispatched only after their prerequisite changes are accepted;
their prompts include saved dependency handoffs and accepted commit IDs.

One integration owner applies each candidate onto the current accepted branch,
then reviews and verifies the resulting revision. Merge conflicts stop the run
with the candidate preserved. Any blocker, failure, or Ctrl-C cancels active
commands and waits for cleanup before releasing the repository lock. Other
claimed tickets become interrupted; undispatched tickets remain pending.

This is one coordinated run, not two supervisors operating on the same checkout.
The repository lock still rejects another run, including from a linked worktree.
There is no live conversation injected into an active worker turn, lease
reassignment, or automatic resume. Explicit native resume is available for interrupted runs; adaptive configurations also support bounded review/check escalation. See [coordination details](docs/PARALLEL_EXECUTION.md)
and [validation evidence](docs/VALIDATION.md). No live mixed-agent acceptance run
has been recorded.

## Small serial example

The [serial tickets](examples/serial-tickets.json) add a tested Python greeting function, then a command-line interface. They contain no skill requests. From a source checkout, prepare a new sibling repository:

```sh
mkdir -p ../anvil-demo/tests
touch ../anvil-demo/tests/__init__.py
printf '__pycache__/\n' > ../anvil-demo/.gitignore
git -C ../anvil-demo init
git -C ../anvil-demo add .gitignore tests/__init__.py
git -C ../anvil-demo commit -m "Initialize Anvil demo"
anvil validate examples/serial-tickets.json
anvil plan examples/serial-tickets.json
anvil run examples/run.json
```

Use a new `anvil-demo` directory and your existing Git identity. The example configuration targets that sibling repository and runs Python's unittest discovery. Its initial empty test suite provides a passing baseline; the tickets require tests for the new behavior. The final command launches real Codex turns and uses your configured model/account. To run this example with Claude Code instead, use `anvil run examples/claude-run.json` as the final command. The larger [planning example](examples/tickets.json) includes future skill requests and is for planning only.

## Tickets and entry skill

The input is JSON with `version: 1` and a nonempty `tasks` array. Each task has an `id`, `title`, `objective`, `depends_on`, and nonempty `acceptance_criteria`; see the [ticket schema](schemas/tickets.schema.json). Optional scheduling fields are `worker`, `resources`, and `exclusive`. The planner rejects duplicate IDs, missing prerequisites, self-dependencies, cycles, and malformed fields. Optional `skills` names select skills from the repository's healthy managed AI Hero installation. A run can instead opt into deterministic selection for empty arrays. The pinned source revision, selection origin and reasons, compatibility decision, and per-attempt delivery are recorded in the run ledger. A changed or unreviewed instruction hash fails before checks or model work; mixed pools dispatch a selected skill only to workers with its required capabilities.

The Codex entry skill lives at [skills/anvil/SKILL.md](skills/anvil/SKILL.md). Install the CLI first, then copy the complete `skills/anvil` directory into your chosen agent skill directory, inspecting any existing skill before replacing it. Python distributions include it under `share/anvil/skills/anvil` in the installation prefix. Package installation does not register the skill automatically.

See the [build plan](docs/PLAN.md), [implementation milestones](docs/ROADMAP.md), and [upstream integration notes](docs/UPSTREAM.md) for the broader design.

The proposed [delivery and dashboard milestone](docs/DELIVERY_DASHBOARD_PLAN.md)
covers GitHub/Bitbucket pull requests, Jira/Linear/GitHub issue synchronization,
and a local run dashboard. These integrations are specified, not implemented.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI tests Python 3.11 and 3.12, runs the example planner, and checks distribution packaging. Tests use real temporary Git repositories and local fake agent executables to exercise execution and failures without model turns or external publication. See [validation evidence](docs/VALIDATION.md) for the separate live Codex exercise and its reproduction instructions.

## Attribution

Anvil is an independent ForgeStack project. Its design builds on [Matt Pocock's AI Hero skills](https://www.aihero.dev/skills), including the experimental [`implement-spec` workflow](https://github.com/mattpocock/skills/blob/main/skills/in-progress/implement-spec/SKILL.md). The design reference revision is recorded in [upstream/aihero.lock.json](upstream/aihero.lock.json). Explicit skill installations record their own revision in the installation manifest; package installation does not fetch upstream skills.

Anvil is MIT licensed. Installed AI Hero skills retain their upstream MIT license and attribution.

## Ticket status and model routing

Opt into source JSON ticket status with `ticket_status: true`. Named model/effort profiles, shadow/rules/adaptive routing, usage reports, one bounded escalation, and local gated policy learning are available through `adaptive` configuration. Existing configurations retain their behavior. See [configuration and limitations](docs/ADAPTIVE_ROUTING.md) and [example](examples/adaptive-run.json). Currency reservations are estimates; live savings require workload evidence.

## Watch a run

```sh
anvil serve --state-dir ~/.local/state/anvil --port 8787
```

Serves saved and in-progress runs as JSON for a dashboard or other long-lived
reader: paginated run, task, attempt, and event pages, per-attempt token and cost
accounting, and a Server-Sent Events stream that resumes exactly from the ledger
event ID in `Last-Event-ID`. Every route is a read-only `GET`; the server never
writes Git, the ledger, or an artifact, and stopping it cannot stop a worker.

Cost figures are estimates labeled with their provenance, not invoices, and
absent accounting stays unknown rather than zero. Binding is loopback-only:
reach it from another device over an SSH tunnel. See [the serve
contract](docs/SERVE.md), and [the delivery dashboard
plan](docs/DELIVERY_DASHBOARD_PLAN.md) for the delivery, issue, and group views
that remain future work.

## Resume interrupted work

```sh
anvil status /path/to/run-directory --json
anvil resume /path/to/run-directory --json
```

Resume preserves the run ID, completed tickets, accepted branch and evidence. It
uses the saved configuration, requires unchanged ticket inputs, and repeats the
baseline checks. Unfinished work gets fresh ownership and workspaces. Eligible
candidate commits are reviewed and verified again; other tickets restart.

Only runs created with the native recovery protocol are supported. Failed or
blocked runs require operator diagnosis, and live orphaned process groups or an
unresolved spawn intent block recovery. Adaptive routing keeps its frozen policy,
CLI versions and cumulative budget; recovered runs are excluded from training.
See [the recovery contract](docs/RECOVERY.md) for boundaries and the difference
from the replacement-run scripts in `tools/recovery`.
