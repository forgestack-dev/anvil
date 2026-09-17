# Markdown specification preparation

`anvil prepare` turns one committed UTF-8 Markdown specification into a strict,
reviewable JSON ticket graph. Preparation and implementation are separate
commands: a successful preparation writes tickets but never starts a run.

```sh
anvil prepare SPEC.md --output tickets.json --repo .
anvil prepare SPEC.md -o tickets.json --repo . --agent claude-code
anvil prepare SPEC.md -o tickets.json --repo . --agent muse --timeout 1800
anvil prepare SPEC.md -o tickets.json --repo . --gate-agent none
```

The repository must be clean, the specification must be a regular file inside
that repository, and both it and the repository state must already be committed.
The output must be a new path inside the repository with an existing parent.
Anvil refuses to overwrite a file. Model artifacts default to
`~/.local/state/anvil/preparations`; `--artifact-root` selects another location.

The selected adapter gets one read-only planning turn and may inspect repository
files. Codex uses its read-only sandbox, Claude receives only its bounded read
tools, and Muse uses the operator handoff. `--agent-binary` selects a trusted
custom executable. This turn consumes the selected provider's normal usage.

Anvil accepts the response only when it is a valid acyclic version 1 graph with
at most 100 tasks, or a set of questions the specification must answer first. Every task must include observable acceptance criteria,
`source_refs` that exactly match lines in the specification, risk, resource reservations, exclusivity, and an explicit skill
array. The supervisor adds `execution.status: todo`, source SHA-256, repository
HEAD, generator version, timestamp, and planning agent. It then validates the
final document and atomically writes it. Invalid output remains only in the model
artifact directory.

## The readiness gate

A preparation is two read-only turns, not one. After the planning turn produces a
graph, a second turn judges it against the specification and the criterion
contract in [CRITERIA.md](CRITERIA.md). It runs on the execution adapter
`--agent` did not select, so `--agent codex` gates on `claude-code` without a
flag, and `--gate-agent` selects another. `prepare` refuses a gate on the
authoring adapter: a turn that judges its own graph is not an independent one.

The gate cannot approve and cannot propose a graph. It returns questions the
specification must answer, each naming the contract rule it invokes and citing
exact specification lines, which are validated the same way ticket `source_refs`
are. An empty result records the absence of an objection, not an endorsement.

Any question from either turn means no tickets are written. `anvil prepare` exits
`3`, distinct from the `2` it uses for contract and process errors, and prints
each question with its rule and citations. Answer them by editing and committing
the specification, which changes `provenance.source_sha256` and makes the next
preparation traceably a different input; there is nowhere else an answer would
be durable.

The gate's availability is probed before the planning turn is dispatched, so a
missing second agent costs a subprocess rather than a planning turn. When only
one agent is installed, pass `--gate-agent none`; the preparation is then
recorded with `"gate": null` rather than silently gated by its own author.

## Skill recommendations

Preparation reads the repository's managed AI Hero installation without network
access. The planning prompt receives only installed skills whose exact
`SKILL.md` hashes exist in Anvil's compatibility registry and whose requirements
can be satisfied by at least one supported worker. It also receives each skill's
compatible agents. Unclassified installed skills are reported and omitted.

This is bounded recommendation, not autonomous skill discovery. The generated
`skills` arrays remain visible for review, and execution applies its own exact
compatibility preflight against the configured workers.

After reviewing the output, run `anvil validate` and `anvil plan`. Create or
select a run configuration separately when the proposed scope and dependencies
are accepted.
