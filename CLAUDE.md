# Navigating Anvil

`AGENTS.md` states the invariants this repository must preserve. Read it. This
file is the map: where things live, so you can open two or three files instead
of exploring. Neither file replaces reading the code you are about to change.

## Running tests

```sh
tools/run-tests.py                                  # about 50 seconds
PYTHONPATH=src python3 -m unittest discover -s tests # the same tests, serially
```

`anvil` is not installed in most local interpreters, so a bare
`python3 -m unittest discover -s tests` fails with import errors rather than a
useful result. CI installs the package first (`pip install -e .`) and omits the
prefix; locally, prefer `PYTHONPATH=src`, which `tools/run-tests.py` sets for
you. The suite is about 465 tests: roughly 60 seconds through the parallel
runner, roughly three minutes serially. Both dispatch the same modules and
assertions. There are no third-party dependencies, and the runner is standard
library like everything else.

Validate ticket and run contract changes with `anvil validate` and
`anvil plan examples/tickets.json --json`.

## Module map

All source is in `src/anvil/`. Sizes indicate where the complexity is.

| Area | Module | What it owns |
| --- | --- | --- |
| Entry | `cli.py` | argparse subcommands: `validate`, `plan`, `doctor`, `run`, `resume`, `status`, `prepare`, `skills`, `route`, `tickets`, `routing`, `serve` |
| Input | `config.py` | `RunConfig`, `WorkerConfig`, and `from_document` validation of `run.json` |
| Input | `planning.py` | `TaskGraph.load`, dependency validation, `waves` |
| Input | `contracts.py` | `ContractError` and shared contract helpers |
| Ledger | `store.py` | `RunStore`: append-only numbered events, `snapshot()`, and the static read-only `RunStore.read()` |
| Ledger | `queries.py` | Bounded paginated reads over one ledger for a long-lived consumer: run summaries, tasks, attempts, events after a cursor |
| Execution | `execution.py` | Serial run loop, baseline and per-candidate `verify()` |
| Execution | `parallel.py` | Worker-pool coordinator; the largest module |
| Git | `workspaces.py` | `Repository`, `RepositoryLock`, worktrees, `commit_candidate`, `prepare_integration`, `advance_branch` |
| Processes | `processes.py` | `run_process`, process groups, timeouts |
| Processes | `environment.py` | `managed_environment()` |
| Recovery | `recovery.py` | `resume` reconciliation against branch and ledger |
| Serving | `serve.py`, `assets/` | Read-only local HTTP over the ledger and telemetry, plus the packaged dashboard page; loopback-only, GET-only |
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
  monotonically numbered; add an event kind rather than mutating past rows. The
  ledger is a write-ahead log database so a read-only observer never blocks the
  supervisor; under a rollback journal a reader could fail a live run. Keep that
  mode sticky, and treat the `-wal` and `-shm` sidecars as part of the ledger.
  Reading a ledger recreates its shared-memory index, so a read needs a writable
  directory even though it never changes the ledger.
- **Changing Git behavior:** `workspaces.py`. The supervisor owns Git entirely;
  worker turns never commit, branch, or push.
- **Changing how agents are invoked:** the relevant `adapters/` module.
  Claude Code turns have bounded file tools and no Bash; the supervisor supplies
  the review diff and runs verification on the host. Turns per invocation
  default to `MAX_TURNS` in `adapters/claude.py` and a run configuration can
  raise or lower that with `agent_turns`. A run may also supply an `orientation`
  file, whose text reaches implementation and review prompts verbatim; this file
  is usually it, so keep it accurate.
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
