# Anvil

**Turn a spec into coordinated engineering work.**

Anvil is ForgeStack's engineering harness for working through specifications and tickets with coding agents. It combines an entry skill, a local runner, and adapters for Codex and Claude Code. Integration with the full AI Hero skill catalog is planned.

## Current status

Anvil executes a JSON ticket graph **serially** using Codex or Claude Code. It launches the selected agent in isolated Git worktrees, records results in SQLite, reviews the complete integration revision with a separate turn that has read-only tools, runs required checks, and advances a managed local branch only after acceptance evidence, review, and verification pass. Each run uses one agent for both implementation and review.

It also validates ticket graphs, previews dependency waves, checks local prerequisites, and reads saved run state. Parallel workers, retries, pause/resume, crash recovery, upstream skill loading, Markdown intake, and issue-tracker closeout remain planned. Requests for skills in an execution ticket are rejected rather than silently ignored. Nothing installs Anvil into an application repository automatically.

## Install and plan

Requires Python 3.11 or later. Serial execution requires macOS or Linux, Git, and an installed Codex CLI or Claude Code CLI with working account access. Claude Code support targets version 2.1.260 or later with the required flags advertised by `doctor`. The runtime has no third-party Python dependencies; installation uses the build tools declared in `pyproject.toml`.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
anvil validate examples/tickets.json
anvil plan examples/tickets.json --json
anvil doctor
anvil doctor --agent claude-code
```

Validation and planning work without an agent CLI. `doctor` checks Git and the selected executable's version and advertised flags; it does not authenticate or make a model request. It defaults to Codex. To probe a custom executable, supply `--agent-binary /path/to/executable`; `doctor` does not read run configurations. The planner's dependency waves describe possible parallelism, but this version executes one ticket at a time.

To use the CLI directly from a source checkout:

```sh
PYTHONPATH=src python3 -m anvil plan examples/tickets.json
```

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

Set `agent` to `"claude-code"` to use Claude Code; omitting it preserves the Codex default. Optional `agent_binary` selects a trusted local executable, defaulting to `codex` or `claude` for the selected agent. Executable paths containing a slash resolve relative to the configuration file. Legacy `codex_binary` remains accepted for Codex configurations, but cannot be combined with `agent_binary` or used with Claude Code.

**Run configurations are trusted executable input.** Each verification entry is an argument array executed directly on the host in the managed worktree. Anvil does not sandbox these commands or interpret shell syntax. Inspect the commands and any scripts they invoke before running a configuration. Codex implementation turns use `workspace-write`; review turns use `read-only`. Claude implementation turns can read and edit files, while review turns have only file-reading and search tools. Claude turns have no Bash tool; the supervisor supplies the review diff and runs the trusted checks. Claude's tool restrictions do not provide an operating-system sandbox. See [agent behavior](docs/AGENT_ADAPTERS.md) for configuration and permission details. Anvil supplies no model override or permission bypass.

Managed Git, agent, and verification commands discard inherited `GIT_*` environment variables so Git uses the managed worktree. Other environment settings are preserved.

`agent_timeout` applies separately to each implementation and review turn; `check_timeout` applies to each verification command. Both must be greater than zero and at most 3,600 seconds. Claude Code also has a limit of 32 agentic turns per invocation. Optional `state_dir` must be outside the target checkout and its Git directory; its default is `~/.local/state/anvil`.

```sh
anvil run /path/to/run.json
anvil status /path/to/saved/run-directory --json
```

`run` prints progress and the saved run directory; add `--json` for a machine-readable final report. Running the same configuration again starts a new run. `status` reads existing state and **does not resume execution**.

For each ticket, Anvil:

1. Implements the ticket in a new worktree based on completed prerequisites.
2. Validates structured acceptance evidence and commits a complete candidate.
3. Prepares the integration revision and independently reviews that exact revision in read-only mode.
4. Runs the configured checks, rejects changes after review, and advances the managed branch only to the verified revision.
5. Records completion before starting a dependent ticket.

A worker's success message alone cannot complete a ticket. A blocker, requested review changes, failing check, process timeout, or other terminal failure stops the entire serial run, including independent tickets. Completed work remains on the managed branch; failed candidates, worktrees, logs, and state are preserved for inspection. Ctrl-C terminates the active process group and records interruption. Process groups clean up ordinary child processes; they are not a security boundary against deliberately detached programs.

The saved run directory contains `state.sqlite`, `report.json`, worktrees, and per-ticket artifacts: structured worker/review results, event streams, stderr logs, verification outputs, and associated commit IDs. Workspace and artifact directories use unique attempt IDs; saved attempts map them to the original ticket IDs. A hard crash may leave state recorded as running; automatic reconciliation and resume are not implemented. Inspect preserved work before deciding how to proceed.

`run` exits with 0 for success, 1 for failure, 2 for invalid input/setup, 3 for blocked work, or 130 for Ctrl-C. `status` exits with 0 when the saved state was read successfully, regardless of the run's recorded outcome. Anvil does not push, open a pull request, merge into a user branch, or close external tickets.

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

The input is JSON with `version: 1` and a nonempty `tasks` array. Each task has an `id`, `title`, `objective`, `depends_on`, and nonempty `acceptance_criteria`; see the [ticket schema](schemas/tickets.schema.json). The planner rejects duplicate IDs, missing prerequisites, self-dependencies, cycles, and malformed fields. Optional `skills` names can be planned, but nonempty skill requests cannot yet execute.

The Codex entry skill lives at [skills/anvil/SKILL.md](skills/anvil/SKILL.md). Install the CLI first, then copy the complete `skills/anvil` directory into your chosen agent skill directory, inspecting any existing skill before replacing it. Python distributions include it under `share/anvil/skills/anvil` in the installation prefix. Package installation does not register the skill automatically.

See the [build plan](docs/PLAN.md), [implementation milestones](docs/ROADMAP.md), and [upstream integration notes](docs/UPSTREAM.md) for the broader design.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI tests Python 3.11 and 3.12, runs the example planner, and checks distribution packaging. Tests use real temporary Git repositories and local fake agent executables to exercise execution and failures without model turns or external publication. See [validation evidence](docs/VALIDATION.md) for the separate live Codex exercise and its reproduction instructions.

## Attribution

Anvil is an independent ForgeStack project. Its design builds on [Matt Pocock's AI Hero skills](https://www.aihero.dev/skills), including the experimental [`implement-spec` workflow](https://github.com/mattpocock/skills/blob/main/skills/in-progress/implement-spec/SKILL.md). The source revision is recorded in [upstream/aihero.lock.json](upstream/aihero.lock.json). No upstream skill code is currently vendored or loaded.

Anvil is MIT licensed. Future upstream imports must preserve their original license and attribution.
