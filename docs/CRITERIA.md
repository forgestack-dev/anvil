# The criterion contract

Status: an authoring standard, not a validator. The readiness gate in the last
section is implemented in `preparation.py` as of 2026-09-17; nothing about its
effect on run outcomes has been measured, and rules 1 through 6 remain authoring
guidance that no command enforces. The measurements this rests on are in
[OBSERVED_LIMITS.md](OBSERVED_LIMITS.md); the acceptance ordering it assumes is
in [ACCEPTANCE.md](ACCEPTANCE.md).

## Why the criterion is the unit

Anvil never adjudicates a specification. It adjudicates one criterion at a time:
a reviewer returns a per-criterion satisfied map, `verify()` returns an exit
code, and `store._require_evidence` binds both to one integration revision. A
ticket is complete when its criteria are individually decided. Everything a
richer ticket document could carry -- personas, metrics, non-goals -- is read
once by a worker and never decided by anything.

So the property worth standardizing is not what a ticket contains. It is whether
each criterion has a decision procedure, and whether the role asked to decide it
can run that procedure.

Run `51fec4cd` is the evidence. Attempt 3's reviewer marked all three criteria
`satisfied: true` on a candidate that fails its own two new leakage tests and
still launches the agent binary through an unexcluded `routing.preflight` probe.
Two of those three criteria -- "leakage tests prove excluded variables are
absent" and "existing serial and parallel execution tests remain green" -- are
decided by running the suite, and the reviewer has Read, Glob and Grep and
cannot run anything. It conceded them from the diff. The third criterion was
decidable by reading the diff, and attempt 4's reviewer decided it correctly, at
`src/anvil/cli.py:173`, naming the same two-line hunk the human fix `04f270d`
later applied.

Every field of that ticket was populated. The defect was that two of its three
criteria were routed to a role that could not decide them.

## The contract

**1. Every criterion names its decision procedure.** One of three kinds, and the
author should be able to say which without thinking hard:

| Kind | Decided by | Example |
| --- | --- | --- |
| `check` | the run's configured verification commands | "the unittest suite passes with the new leakage tests present" |
| `inspection` | reading a named region of the candidate diff | "`routing.preflight` receives `exclude` at every call site listed below" |
| `artifact` | a named path's existence or content at the integration revision | "`README.md` documents the `credential_exclusion` field" |

A criterion that fits none of the three is not a criterion. It is a hope.

**2. One criterion, one role.** A `check` criterion is decided before review
reaches it (Stage 1) and the reviewer is told only that the checks ran. An
`inspection` or `artifact` criterion is the reviewer's to decide. A criterion
whose truth requires the reviewer to execute something violates the contract at
authoring time, and no prompt fixes it: the measured reviewer obeyed its prompt
and conceded anyway.

**3. An absence names the set it holds over.** "No X anywhere" is not decidable
by either role, because the set is settled once per review. Name the launch
sites, the call sites, the modules. Expect the enumeration to be most of the
design work, and expect it to be incomplete: the human's own rewritten
enumeration for `apply-credential-exclusion` omitted `cli.py`, which is the site
that ended the run. The contract does not promise completeness. It promises that
an omission costs one attempt and returns as a named file, rather than as an
unbounded argument.

**4. One criterion, one claim.** No conjunctions. The acceptance map is
per-criterion and boolean; a criterion joining two claims with "and" cannot be
half-satisfied, so a reviewer must either concede the true half or reject the
false one without saying which. Split it.

**5. Criteria are frozen for the run.** A requirement discovered during review
is not a criterion of this ticket. Under Stage 3 it stops the ticket for its
author and is recorded as a proposal; it must not be appended to a retry prompt,
because that enlarges the ticket's effective scope while its frozen criteria are
unchanged.

**6. Prefer a criterion the worker cannot grade itself.** Stage 1 catches a test
that cannot pass, for free. A test that passes vacuously is caught only by a
reviewer reading it, and `04f270d` records a green suite containing exactly such
a test. Where a `check` criterion rests on tests the same turn authored, pair it
with an `inspection` criterion over the test bodies.

## The measured ticket, rewritten

`apply-credential-exclusion` as it ran, and as this contract would have it:

| As written | Kind | Problem | Rewritten |
| --- | --- | --- | --- |
| "leakage tests prove excluded variables are absent" | check | routed to a role that cannot run it; conceded | unchanged as a criterion, but decided by Stage 1 before review, and paired with an inspection criterion naming the two new test bodies |
| "existing serial and parallel execution tests remain green" | check | same | same |
| "configured variables are withheld from every managed subprocess" | inspection over an unnamed set | rule 3 | "the configured exclusion reaches each of: `execution.py:42`, `parallel.py:163` and `:169`, `routing.py:211` and `:221`, `adapters/claude.py:230`, `adapters/codex.py:188`, and the `routing.preflight` calls at `cli.py:173` and `:182`" |

Note what the rewrite does not fix. Attempt 5 ran the enumerated version, spent
$4.01 and 118 turns, and produced no candidate at all. Criterion shape is not
ticket size, and neither is a model-capability problem: a human implemented the
whole ticket directly in about half an hour.

## What this contract is not

- **Not a validate-time lexicon.** Refusing open-set criteria at `anvil validate`
  is already rejected in [ACCEPTANCE.md](ACCEPTANCE.md): measured on 2026-09-17,
  the rule fired on 36 of the 128 criteria then in this repository's own backlog,
  including three tickets that are done. The backlog has grown since; the point
  is the proportion, not the pair of numbers. Rules 1 through 6 are authoring guidance, read by a human and by the
  planning turn. Only rule 3's enumeration has a proposed typed home, as Stage 4
  declared sites.
- **Not executable commands in ticket files.** A ticket file sits inside the
  target repository and is worker-writable when `ticket_status` is off. A
  per-criterion command field would make ticket content host argv, which the
  subprocess rule in AGENTS.md forbids.
- **Not a completeness check on the specification.** Nothing here asks whether a
  PRD has enough sections. Required fields produce text, not information: a
  planning turn will fill a required `non_goals` plausibly every time, which
  converts an obviously underspecified ticket into a superficially complete one.
- **Not a schema change.** The contract is carried today by
  `skills/anvil/SKILL.md`, which already states rule 3, and by the planning
  prompt in `preparation._prompt`.

## What this does not establish

The rewrite above was never run. The claim that routing a criterion to a role
that can decide it lowers the false-concession rate is a reading of one
corrected run, not a measurement. Rules 4 and 6 have no supporting run at all;
they follow from the shape of the acceptance map and from `04f270d`. Audit a
rejection by the method in [OBSERVED_LIMITS.md](OBSERVED_LIMITS.md) before
concluding that any of this changed an outcome.

## The prepare readiness gate

Rules 1 through 6 had no enforcement point, because `anvil prepare` had no way to
decline: one planning turn could emit tickets or raise `ContractError`, so an
underdetermined specification produced invented ones. The gate is that way. What
follows describes what `preparation.py` now does.

### Why prepare, and not run

Worker turns are bounded and non-conversational, the supervisor owns Git and the
ledger, and dependency handoffs are saved evidence rather than live chat. There
is nobody to ask mid-run. The mid-run analogue of a question already exists and
is Stage 3: a requirement the ticket does not carry stops the ticket for its
author. Intake is the only place a question can be asked before money is spent.

The answer has a natural home too. `prepare` requires the specification to be
committed and hashes it into `provenance.source_sha256`, so the only durable
place to record an answer is the specification itself. Answering a question means
editing and committing the spec, which changes the hash and makes the second
preparation traceably a different input. No new store is needed.

### What it replaced

`preparation.prepare` was all-or-nothing. One read-only planning turn returned a
document, `result_schema` constrained it to `{version, tasks}` with 1 to 100
tasks, and every other path raised `ContractError` and wrote nothing. There was
no representation for "this specification does not determine a ticket graph, and
here is what is missing," so the turn's only options were to emit tickets or to
fail, and it invented.

### The shape

**The result is a discriminated union.** `result_schema` carries a second branch
under `oneOf`, keyed by which of `tasks` or `questions` is present; `prepare`
re-checks that exactly one is there rather than trusting the model's structured
output. The
question object is given below, with the gate that produces it; the planning
turn may emit the same shape when the specification defeats it outright.

`kind` is the contract's own vocabulary, which is what keeps the gate from being
a general clarification prompt. `undecidable` is rule 1: a criterion can be
stated but no role can be named that decides it. `unbounded` is rule 3: the
criterion is an absence over a set the specification does not close. `missing`
and `ambiguous` are ordinary intake. `source_refs` is validated exactly as it is
for tasks today, against `source_lines`, so a question must point at a real line
of the specification and cannot be invented whole.

**Refuse partial emission.** A document carrying questions writes no tickets at
all, and a result carrying both branches is refused outright. Waves and dependencies are
derived from the whole document; emitting the unambiguous half would produce a
graph whose shape assumes answers nobody gave, and a written `tickets.json`
invites `anvil run`. This is the sharpest tradeoff in the design -- one
ambiguous paragraph blocks twelve good tickets -- and per-ticket partial
emission is the open question here. Whole-document is the safe default because
`prepare` already refuses to overwrite and writing nothing is its existing
failure behavior.

**The write path is untouched.** Both question branches return
`needs_clarification` before any provenance is built, so `atomic(output,
encoded(final))` and the second `TaskGraph.from_document(final)` still run only
on a graph that survived every check. A `needs_clarification` result names the
turn that raised it, so a specification that defeats the planning turn is
distinguishable from a graph the gate objected to.

**CLI.** `anvil prepare` has a third exit code: `0` prepared, `2` contract or
process error as before, `3` needs clarification, with the questions printed to
stderr or under `--json`, each with its rule number and cited specification line.
A distinct code matters because the caller is often a script or an outer agent,
and "I have a question" is not an error. `--gate-agent` and `--gate-binary` sit
alongside the existing `--agent` group.

### The gate is adversarial, and not the turn that authored the graph

A single planning turn can carry the union above, but a turn asked to both
produce a graph and decline to produce one grades its own output. That is the
same shape as the defect this document exists to describe: attempt 3's reviewer
filled a satisfied map over work it could not check, and the prompt told it not
to. Prompt text is not a mechanism.

So the gate is a second read-only turn, and it is constrained in three ways.

**It sees the specification and the graph, and not the planning turn.** The
planning turn's artifact directory, its reasoning and its own account of what it
found are withheld. It is given the spec bytes, the emitted tasks, and rules 1
through 6. Anchoring a gate on the author's explanation is how a gate learns to
agree.

**It can only ask.** The gate has no approval verdict and no `tasks` branch: its
result is a `questions` array, possibly empty. An empty array is the absence of
an objection, not evidence of readiness -- the same distinction `ACCEPTANCE.md`
draws when it refuses to merge on a satisfied map. Nothing downstream may read
an empty gate result as a quality signal.

**Every question is bound and located.** A question carries the rule it invokes
and a `source_refs` line that exists in the specification, both schema-enforced,
which is Stage 2's bound-and-located rejection applied at intake. This is the
counterweight to the obvious failure mode of an adversarial role: a turn told to
find problems finds them, and an ungated gate simply never lets a preparation
succeed. Binding a question to a numbered rule and a real line makes an
unfounded question visible as one.

```python
question = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "rule", "kind", "question", "source_refs"],
    "properties": {
        "id": identifier,
        "rule": {"type": "integer", "minimum": 1, "maximum": 6},
        "kind": {"type": "string",
                 "enum": ["missing", "ambiguous", "undecidable", "unbounded"]},
        "question": text,
        "source_refs": {"type": "array", "minItems": 1, "uniqueItems": True,
                        "items": text},
        "options": {"type": "array", "uniqueItems": True, "items": text},
    },
}
```

### A different adapter, and a different model

The gate should not run on the model that wrote the graph, and preferably not on
that vendor's model at all. Two turns of one model over one specification share
whatever the specification failed to make salient; that is the correlated
failure a second opinion is bought to avoid.

`--gate-agent` takes an `EXECUTION_AGENTS` member or `none`. The default is the
execution adapter that `--agent` did not select, so `--agent codex` gates on
`claude-code` and the reverse, without a flag. `--gate-model` and `--gate-effort`
select the gate's model explicitly, and `--model` and `--effort` do the same for
the planning turn; each pair is given together or not at all. Omitting them
leaves that turn on its adapter's default, as before.

**Availability is checked first.** `prepare` probes the gate selection *before*
the planning turn is dispatched, so a missing second provider costs a subprocess
rather than being discovered after the graph is paid for. A turn with no profile
gets `adapters.probe_agent`, the same probe `doctor` uses. A turn with a profile
gets `routing.preflight`, which additionally confirms the CLI advertises the
`--model` and effort controls that profile depends on; an adapter that silently
ignores a model flag would otherwise run the gate on whatever its default is
while provenance recorded the model that was asked for.

**Distinctness is declared and checked, not proved.** `_gate_selection` refuses
a gate on the authoring adapter, and `_distinct` refuses one whose model matches
the planning turn's, both before any turn is dispatched. Two adapters pointed at
one model are not a second opinion, and that is now a refusal rather than a
caveat -- but only when both models are stated, because a turn left on its
adapter's default has no identifier to compare. Anvil still cannot assert a
different *provider*: `validate_selection` accepts any model string matching its
identifier pattern, and an adapter's CLI can be pointed at another endpoint. The
provenance record states what was configured, which is the only honest claim
available.

**Muse is the strongest gate and the slowest.** A Muse gate turn is the operator
handoff: the questions come from whoever is running Anvil. Distinctness is total
and the mechanism already exists, but the preparation blocks on a human, which
is the correct default for a first specification and the wrong one for the
twentieth. A Muse turn takes no profile -- `EFFORTS["muse"]` is empty and
`validate_selection` rejects any selection for it -- so `prepare` refuses a gate
profile for Muse with that reason rather than an effort-list error.

**No silent self-gating.** If only one adapter probes, `prepare` does not quietly
gate on the authoring adapter. Either the operator passes `--gate-agent none`,
which is recorded, or the preparation fails with the probe error. A graph gated
by its own author and a graph not gated at all must not be indistinguishable
from a graph gated across providers.

### Provenance

The gate's outcome belongs in the ticket document, because a graph that was
gated and a graph that was not are otherwise identical files:

```json
"gate": {"agent": "claude-code", "distinct_adapter": true, "questions": 0}
```

`"gate": null` records an explicit `--gate-agent none`. `gate.model` and the
sibling `provenance.model` are written only when `--gate-model` or `--model` gave
one; a turn on its adapter's default records no model rather than inventing the
identifier it happened to resolve to, which would be exactly the unverifiable
claim this section exists to avoid. `questions` is constrained to `0` in both
`schemas/tickets.schema.json` and `contracts._validate_gate`, because a document
is only written when the gate raised none. Absent `gate` and `model` keys are
accepted, so documents prepared before either existed still validate.

### Cost

Two invocations per preparation instead of one, on the order of $1 to $4 each at
the rates in `OBSERVED_LIMITS.md`, against attempt-scale spends of $2 to $7 for a
ticket that does not merge. The gate turn is cheaper than the planning turn in
principle -- it reads a graph rather than a repository -- but nothing here
measures that. Two providers also means two sets of credentials on the host, and each adapter's
key is useless to the other. `prepare --config run.json` reads that run
configuration's `credential_exclusion` and withholds those variables from both
turns and from the availability probe that precedes them; `probe_agent` and
`routing.preflight` both take the set, and each adapter's `doctor` now launches
its version and flag probes under `managed_environment(exclude)`. Without
`--config` there is no run configuration and so no set to apply, exactly as
`anvil doctor` behaves. Worth
measuring; not worth asserting in advance.

### Tests

`tests/test_preparation.py`, using the existing injected fake runner, now
injected twice -- one planning runner and one gate runner, which also proves the
two turns are separately sourced:

- a gate returning `questions` writes no output file and reports
  `needs_clarification`, even when the planning runner returned a valid graph;
- a gate returning an empty array writes the graph, with `provenance.gate`
  recording the gate agent and `distinct_adapter: true`;
- a question citing no rule, a rule outside 1 to 6, or `source_refs` absent from
  the specification raises `ContractError` on the same path task refs do;
- a gate selection matching the planning adapter, or matching its stated model,
  is refused before any turn is dispatched, and `--gate-agent none` is accepted
  and recorded as `"gate": null`;
- each profile reaches the runner it selects, a profiled turn is preflighted
  rather than only probed, an unsupported effort or an unknown profile field is
  refused, and a Muse turn refuses a profile with the operator-handoff reason;
- a configured exclusion set reaches both runners and the probe, a malformed one
  is refused before any turn, and an empty one leaves the cheaper probe in place;
- the planning turn's artifact directory is not present in the gate prompt;
- with `--gate-agent none`, the tasks branch behaves exactly as it does today.

No live model, and no second provider on the test host: both runners are fakes,
as `prepare`'s existing `runner=` injection already allows.

### What the gate cannot do

It cannot tell a well-formed question from a fluent one. Binding a question to a
rule and a specification line makes an unfounded one visible to a reader; it does
not stop one being asked, and no schema checks relevance -- the same limit
`ACCEPTANCE.md` records for a reviewer's satisfied map.

It cannot check that an enumeration is complete. Rule 3 bounds an argument; the
human's own enumeration still omitted `cli.py`.

It cannot establish that two providers fail independently. Different vendors are
not independent estimators of what a specification left out, and nothing here
measures the correlation. Cross-provider gating is a reasonable prior, not a
result, and a same-vendor gate on a different model may recover most of the
benefit far more cheaply. That comparison is the first thing to measure.

It cannot prove the gate ran on the provider it claims. `provenance.gate` records
a configured adapter and model string, and both are operator-supplied.

It does not record the exclusion set. `run.json` already states it, and copying
variable names into a committed ticket file would duplicate a value that can
drift. The claim that a preparation withheld them is therefore not auditable from
the ticket document; it is auditable only from the configuration that was
passed.

And it moves cost earlier rather than removing it: a specification that produces
questions on every preparation is a specification the author has to write anyway,
which is the point, but it is not a saving until someone measures one.
