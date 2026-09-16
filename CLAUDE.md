# Navigating Anvil

`AGENTS.md` states the invariants this repository must preserve. Read it. This
file is the map: where things live, so you can open two or three files instead
of exploring. Neither file replaces reading the code you are about to change.

## Running tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

`anvil` is not installed in most local interpreters, so a bare
`python3 -m unittest discover -s tests` fails with 30 import errors rather than
a useful result. CI installs the package first (`pip install -e .`) and omits
the prefix; locally, prefer `PYTHONPATH=src`. The suite is about 394 tests and
takes roughly five minutes. There are no third-party dependencies and no test
runner beyond the standard library.

Validate ticket and run contract changes with `anvil validate` and
`anvil plan examples/tickets.json --json`.

## Module map

All source is in `src/anvil/`. Sizes indicate where the complexity is.

| Area | Module | What it owns |
| --- | --- | --- |
| Entry | `cli.py` | argparse subcommands: `validate`, `plan`, `doctor`, `run`, `resume`, `status`, `prepare`, `skills`, `route`, `tickets`, `routing` |
| Input | `config.py` | `RunConfig`, `WorkerConfig`, and `from_document` validation of `run.json` |
| Input | `planning.py` | `TaskGraph.load`, dependency validation, `waves` |
| Input | `contracts.py` | `ContractError` and shared contract helpers |
| Ledger | `store.py` | `RunStore`: append-only numbered events, `snapshot()`, and the static read-only `RunStore.read()` |
| Execution | `execution.py` | Serial run loop, baseline and per-candidate `verify()` |
| Execution | `parallel.py` | Worker-pool coordinator; the largest module |
| Git | `workspaces.py` | `Repository`, `RepositoryLock`, worktrees, `commit_candidate`, `prepare_integration`, `advance_branch` |
| Processes | `processes.py` | `run_process`, process groups, timeouts |
| Processes | `environment.py` | `managed_environment()` |
| Recovery | `recovery.py` | `resume` reconciliation against branch and ledger |
| Status | `ticket_status.py` | `Publisher`, the `after_commit` hook, local JSON ticket metadata |
| Agents | `adapters/claude.py`, `codex.py`, `muse.py` | Per-agent invocation, flags, result parsing |
| Skills | `skill_management.py`, `skill_runtime.py`, `skill_source.py` | AI Hero install, pinning, upstream fetch |
| Intake | `preparation.py` | `prepare`: Markdown specification to validated tickets |
| Adaptive | `adaptive_runtime.py`, `learning.py`, `routing.py`, `telemetry.py` | Profiles, escalation, model routing, usage |

Contracts live in `schemas/tickets.schema.json` and `schemas/run.schema.json`;
change the schema and its runtime validation together. Runnable configurations
are in `examples/`. Tests are `tests/test_<area>.py`, one module per area.

## Orientation by task

- **Changing what a run records:** `store.py` first. Events are append-only and
  monotonically numbered; add an event kind rather than mutating past rows.
- **Changing Git behavior:** `workspaces.py`. The supervisor owns Git entirely;
  worker turns never commit, branch, or push.
- **Changing how agents are invoked:** the relevant `adapters/` module.
  Claude Code turns have bounded file tools and no Bash, and a hard 32-turn
  limit per invocation (`MAX_TURNS` in `adapters/claude.py`); the supervisor
  supplies the review diff and runs verification on the host.
- **Changing run inputs:** `config.py` plus `schemas/run.schema.json`, together.
- **Adding a subcommand:** `cli.py` wires argparse to a module function; keep the
  logic in its own module.

## Conventions

New code is a new module under `src/anvil/`, standard library only, matching the
surrounding style: narrow public functions, dataclasses for frozen inputs,
`ContractError` for input the user can fix. Pass subprocess arguments as lists
and prompt text through stdin; never build shell strings from ticket content.
Tests go in the existing `tests/test_<area>.py` for that area, using fake agents
and real temporary Git repositories rather than live models.
