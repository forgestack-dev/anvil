# Anvil

**Turn a spec into coordinated engineering work.**

Anvil is ForgeStack's engineering harness for working through specifications and tickets with AI Hero skills. The design combines a discoverable entry skill, a persistent local runner, and adapters for coding agents. Codex is the first agent target.

## Current status

This is the initial scaffold. It can validate a JSON ticket graph, preview dependency waves, check the local Codex CLI, and prepare a structured Codex invocation. Preparing an invocation does not launch it.

Worker execution, SQLite persistence, parallel scheduling, review/integration, recovery, and upstream skill installation are planned. There is no `anvil run` command yet. A dry-run plan is not evidence that any ticket has been implemented.

## Quick start

Requires Python 3.11 or later. The runtime has no third-party Python dependencies. Installation uses the build tools declared in `pyproject.toml`.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
anvil validate examples/tickets.json
anvil plan examples/tickets.json
anvil plan examples/tickets.json --json
anvil doctor
```

Validation and planning work without Codex installed. `doctor` checks Git and the Codex executable and its advertised flags. It does not authenticate, make a model request, or verify account access. Exit codes are 0 for success, 1 for missing/incompatible runtime prerequisites, and 2 for invalid input or usage.

To try the planner directly from a source checkout:

```sh
PYTHONPATH=src python3 -m anvil plan examples/tickets.json
```

## Tickets

The current input is JSON with `version: 1` and a nonempty `tasks` array. Every task has an `id`, `title`, `objective`, `depends_on`, and nonempty `acceptance_criteria`; `skills` is optional. See the [example](examples/tickets.json) and [schema](schemas/tickets.schema.json).

The planner rejects duplicate IDs, missing prerequisites, self-dependencies, cycles, and malformed fields. Its waves describe possible parallelism from dependencies, not an execution schedule or resource reservation. Skill names in tickets are requests only; this scaffold does not resolve or invoke them.

Markdown intake and spec-to-ticket generation are planned.

## Entry skill

The Codex skill lives at [skills/anvil/SKILL.md](skills/anvil/SKILL.md). It supports preparing and inspecting plans with the capabilities available in this version. Install the CLI first, then copy the complete `skills/anvil` directory into your agent's skill directory. Do not replace an existing skill without inspecting it.

Python distributions also include the skill under `share/anvil/skills/anvil` in the installation prefix. Installing this package does not automatically register the skill with an agent.

## Design

The intended runtime has a scheduler, isolated workers, and one integration lane. One worker gives sequential execution; multiple workers share a dependency graph and durable coordination state. A task becomes done only after acceptance evidence and verification of its integrated changes.

See the [build plan](docs/PLAN.md), [implementation milestones](docs/ROADMAP.md), and [upstream integration notes](docs/UPSTREAM.md).

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI tests Python 3.11 and 3.12, runs the example planner, and checks distribution packaging. Tests use local fixtures and fake command probes; they do not spend model tokens or publish external changes.

## Attribution

Anvil is an independent ForgeStack project. Its engineering workflow builds on [Matt Pocock's AI Hero skills](https://www.aihero.dev/skills), including the experimental [`implement-spec` design](https://github.com/mattpocock/skills/blob/main/skills/in-progress/implement-spec/SKILL.md). The source revision is recorded in [upstream/aihero.lock.json](upstream/aihero.lock.json). No upstream skill code is vendored in this scaffold.

Anvil is MIT licensed. Future upstream imports must preserve their original license and attribution.
