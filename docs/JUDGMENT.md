# Calibrated judgments over review evidence and criterion shape

Status: proposed specification, not ratified. Nothing here is implemented. It
specifies one new module and two consumers of it, plus the offline studies that
must precede either consumer enforcing anything. Recorded 2026-09-18.

## 1. Outcome and scope

Two places in Anvil decide something by matching keywords against ticket text,
and one place decides nothing at all where a decision is needed.

This document specifies `judgment.py`, a bounded client for a System One model
that returns calibrated probability distributions over answer sets the caller
defines, and two consumers:

- **A. The concession check** (§6), in the acceptance path. It detects a review
  that marked an acceptance criterion satisfied without the means to know, which
  is the one measured correctness defect this repository has recorded.
- **B. The intake gate** (§7), in `anvil prepare`. It gives
  [CRITERIA.md](CRITERIA.md) rules 1, 3, 4 and 6 an enforcement point they have
  never had, at a precision the rejected keyword approach could not reach.

Both rest on one taxonomy (§5). They do **not** share a corpus: §11.1 inventories
the saved runs and finds the intake half validatable today and the acceptance
half blocked on labels the ledger cannot supply.

**In scope:** the client module, the two question sets, their compositions,
configuration, freezing and recovery, credential isolation, the calibration
study, and the acceptance matrix.

**Out of scope, named here so the boundary is legible:** routing rank in
`routing.assess`, automatic skill selection in `skill_runtime`, escalate/retry
choice in `adaptive_runtime.next_profile`, and orientation context filtering.
Each is a plausible consumer of the same module; none has a measured defect
behind it, and the first is a cost optimization whose enforcement posture
differs enough to warrant its own record. See §15.

**What this is not.** Not a replacement for `verify()`; checks are exit codes.
Not a participant in acceptance; see the invariant in §4.2. Not a generative
step anywhere: the model emits no text, and no output of it reaches a prompt.

## 2. The measured defect

Run `51fec4cd`, attempt 3 — attempt `3747d86a` in the saved ledger. The reviewer
returned `satisfied: true` for all three criteria of
`apply-credential-exclusion`, on a candidate that fails its own two new leakage
tests and still launches the agent binary through an unexcluded
`routing.preflight` probe.

Its verdict was `request_changes`, not `approve`. That is not incidental: the
satisfied map and the verdict are independent, the defect is in the map, and
§6.1 scopes the check accordingly.

Two of those criteria — "leakage tests prove excluded variables are absent" and
"existing serial and parallel execution tests remain green" — are settled by
running the suite. The reviewer has Read, Glob and Grep and cannot run anything.
It conceded them from the diff.

`evidence.validate_result` cannot see this. It checks that the acceptance map is
complete, that findings name a criterion and a resolvable location, and that a
success carries no unresolved findings. Attempt 3 satisfied every one of those:
the map was well formed and the claim inside it was false. The defect is not in
the shape of the result. It is in the relationship between what a criterion
requires and what the evidence offered.

That relationship is a judgment, made in seconds by a knowledgeable reader, over
two short texts. It is the shape of problem a System One model is for.

[CRITERIA.md](CRITERIA.md) states the authoring rules that would have prevented
it and records, in its own words, that they are "authoring guidance that no
command enforces." It also records why the obvious enforcement was rejected:
measured 2026-09-17, a keyword rule for open-set criteria fired on **36 of the
128 criteria** then in this repository's backlog, including three tickets that
are done. That is a precision problem, and a calibrated probability with a
tunable threshold is the direct answer to it.

## 3. Existing constraints and extension points

| Constraint | Source | Consequence here |
| --- | --- | --- |
| Standard library only | AGENTS.md, `test_invariants.py::StandardLibraryOnly` | `urllib.request` + `json`; no vendor SDK |
| No live models in ordinary tests | AGENTS.md | every call injectable; suite makes no network call |
| A worker's success cannot mark a task done | AGENTS.md, `docs/PLAN.md` | judgments may only tighten (§4.2) |
| Only the coordinator writes ledger or Git | AGENTS.md | judgment I/O runs off-thread; results are handed to the coordinator to record (§6.4) |
| Every launch names its environment | AGENTS.md, `test_environment.py` | the API key is a credential-exclusion default (§9) |
| Policies immutable, fingerprinted, frozen for a run | AGENTS.md, `routing.py` | thresholds freeze exactly as routing policy does (§8.2) |
| Open CLI, paid multi-party services | [PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §1 | capability open and BYOK; calibration paid (§10) |

Three extension points already exist and are reused rather than added to:

- `RunStore.record_message(task_id, attempt_id, kind, body)` persists a
  coordinator message as a numbered ledger event. `routing_decision` already
  uses it. Judgments use the same path with kind `judgment`.
- `details["failure_category"]` on a stop, as `unlisted_requirement` and
  `undeclared_site` are set in `execution.py` and `parallel.py`.
- `preparation.gate_schema` and `_QUESTION_KINDS` already carry the vocabulary
  the intake gate needs; `undecidable` is rule 1 and `unbounded` is rule 3.

No new store, no plugin boundary, no schema version bump beyond adding one
optional configuration object.

## 4. The judgment contract

### 4.1 Module shape

`src/anvil/judgment.py`. A frozen `Judge` dataclass carrying mode, endpoint,
model, timeout, thresholds and an injectable transport; three question
constructors (`noul`, `choice`, `score`); and one method.

```
Judge.ask(state, questions) -> dict | None
```

**Pinned model, never an alias.** `jev-1.13.0`, not `jev-latest`. An alias moves
when a release ships and would silently invalidate every threshold calibrated
against it. The response reports the versioned ID that answered; it is recorded
with every judgment.

**`None` means proceed.** An unreachable service, a missing key, a timeout, or a
malformed response all return `None`, and every caller has a deterministic path
for that case. Judgments never block a run on network availability: `anvil
doctor` must work offline, the suite must not reach the network, and nothing
here decides acceptance.

**Modes mirror `routing`.** `off` makes no call. `shadow` calls, records, and
changes no outcome. `enforce` may act. Default is `shadow`.

Shadow is not a debugging affordance. It is the corpus-collection mechanism: a
run in shadow writes exactly the evidence a later threshold set is calibrated
against, at no risk to its own outcome. §11 depends on this.

### 4.2 The tighten-only invariant

**A judgment may only ever convert an acceptance into a stop. It may never
satisfy a criterion, approve a candidate, relax a gate, or widen a scope.**

This is a correctness property and a security property at once.

Correctness: AGENTS.md requires completion to rest on criterion evidence,
independent review, and passing checks tied to one integration revision. A
calibrated probability is not a decision procedure and must not be treated as
one. The concession check runs only on items already marked `satisfied: true`,
so its worst outcome is a ticket that stops.

Security: ticket files sit inside the target repository and are worker-writable
when `ticket_status` is off, and the model's own documentation states plainly
that it does not treat its input as hostile by default. A judgment that can only
tighten cannot be steered into widening anything, whatever text an attacker puts
in a ticket or a diff. A judgment that could relax a gate would be an injection
surface with no mitigation available at this layer.

Every composition specified below is checked against this rule in §13.

### 4.3 Question construction

Questions follow the model's documented failure modes rather than general
prompting habit:

- **State carries only what the questions name.** Accuracy falls as unrelated
  detail grows. The concession state is two short texts and nothing else.
- **Instructions name state fields directly.** Indirection costs accuracy, so a
  question says `` `criterion.text` `` rather than "the requirement above."
- **One claim per question.** Conjunctions are decomposed and combined in code,
  which is also [CRITERIA.md](CRITERIA.md) rule 4 applied to ourselves.
- **Criteria are an extension of the instruction,** never in tension with it.
- **No counting, no arithmetic, no date ordering, no interpolation between score
  levels.** All are documented weaknesses; all belong in code.

## 5. The shared procedure taxonomy

Both consumers classify a criterion by the procedure that would settle it. The
taxonomy is [CRITERIA.md](CRITERIA.md) rule 1, reworded into plain language
because the model reads literally and the contract's own terms are ambiguous
English.

| Answer | Rubric supplied to the model | CRITERIA.md kind |
| --- | --- | --- |
| `execution` | Settled by running a command, a test suite, or the program, and reading what it reports | `check` |
| `reading` | Settled by reading the changed source code | `inspection` |
| `artifact` | Settled by checking that a named file exists or contains particular text | `artifact` |
| `none` | Cannot be settled by any of the above | (rule 1 violation) |

One taxonomy, two uses: at intake it says whether a criterion has a decision
procedure at all, and at review it says which role could have decided it. The
same calibration corpus therefore serves both, which is the reason §1 scopes
them into one document.

## 6. Application A: the concession check

### 6.1 When it runs

After a review returns and passes `evidence.validate_result`, for **every
acceptance item marked `satisfied: true`, whichever verdict the review
returned**.

An earlier draft of this section scoped the check to approvals only, on the
reasoning that a rejection is already refused unless it names a criterion and a
resolvable location. The ledger inventory in §11.1 shows that scoping would have
missed its own motivating example: the one confirmed concession in the corpus,
run `51fec4cd` attempt `3747d86a`, returned `request_changes` while marking all
three criteria satisfied. The verdict and the satisfied map are independent, and
the defect lives in the map.

This is not a duplicate of the Stage 3 conceded-rejection path, and the
distinction matters because the two have opposite remedies:

| | `evidence.concedes` (Stage 3) | The concession check |
| --- | --- | --- |
| Fires on | reject + every criterion satisfied | any criterion satisfied without means to decide |
| Reads | the shape of the map | the relationship between criterion and evidence |
| Diagnosis | the ticket is missing a requirement | the review is unreliable |
| Remedy | stop for the author, `unlisted_requirement` | re-review at rank+1 (§6.4) |

Run `3747d86a` is currently diagnosed as `unlisted_requirement` — an authoring
fault — when the record in [CRITERIA.md](CRITERIA.md) shows the satisfied map
itself was false. Where both fire, the concession check takes precedence,
because a map produced without the means to decide is not evidence that the
ticket omitted a requirement.

There is still no symmetric false-reject check: nothing here can move an item
from `satisfied: false` to true, or turn a rejection into an approval. The
asymmetry is what makes this safe to add.

### 6.2 State and questions

State is an object with exactly two named fields:

```json
{"criterion": {"text": "..."}, "evidence": {"text": "..."}}
```

Five questions, evaluated in parallel against that one state:

| Key | Type | Asks |
| --- | --- | --- |
| `procedure` | choice | Which procedure would settle `criterion.text`? (§5) |
| `reports_execution` | noul | Does `evidence.text` report what happened when something was run? |
| `reports_reading` | noul | Does `evidence.text` describe code the writer states they saw? |
| `reasons_about` | noul | Does `evidence.text` argue what the code would do, rather than report an observation? |
| `specificity` | score | Names no place / a file or module / a function, class or line |

Nothing asks whether the criterion holds. That is the reviewer's job and the
checks' job. These ask only what would settle it and what was offered.

### 6.3 Composition

```
conceded = procedure.choice == "execution"
       and procedure.confidence >= thresholds.procedure_min
       and reports_execution.noul < thresholds.reports_execution_max
```

Read against §2: *"existing serial and parallel execution tests remain green"*
classifies as `execution`; the reviewer marked it satisfied; the evidence
reported no command output. Caught.

A second, weaker signal is computed and **recorded only, never enforced**: a
criterion classified `reading` or `artifact` whose evidence neither read
anything nor reports an observation, and which reasons about behavior instead.
It is reported so §11 can measure whether it is worth a threshold at all.

### 6.4 Outcome

In `enforce` mode, a detected concession **discards the review and re-dispatches
one fresh review at the next rank up**, via `Policy.escalation(review_profile)`.

A concession is a defect in the review, not in the candidate. The worker's output
may be entirely correct, so re-running the worker would punish the wrong role and
spend the most expensive invocation to do it. The worker is never re-run by this
path.

If the escalated review concedes the same criterion again, or no higher-ranked
profile exists for the reviewer's agent, the ticket **stops for its author** with
`failure_category` `conceded_criterion`. It does not escalate a worker and does
not reach a retry prompt, for the same reason `unlisted_requirement` does not: no
retry of the stated criteria addresses a review that could not decide them.

Accounting and threading:

- The re-review consumes one further invocation and must be reserved through
  `Session.reserve` before dispatch, so a run at its invocation ceiling or soft
  budget declines the re-review and stops for the author instead of overrunning.
- `telemetry.py` gains `conceded_criterion` in its known failure-category set
  alongside `unlisted_requirement` and `undeclared_site`.
- In the worker pool, the HTTP call runs on the worker thread that already holds
  the attempt; only the returned result is handed to the coordinator, which
  records it and owns the dispatch decision. The coordinator never blocks on
  network I/O.

### 6.5 Recording

One `judgment` message per assessed item, carrying the model version, every
answer with its full probability distribution and confidence, the thresholds in
force, the composed booleans, and the mode. Probabilities are recorded in full
and not just the collapsed confidence, because §11 needs the distribution to
choose thresholds and a later threshold set must be re-derivable from saved
runs without re-calling the service.

## 7. Application B: the intake gate

### 7.1 Where it sits

Inside `anvil prepare`, after the planning turn produces a candidate graph and
alongside the existing adversarial gate. It reads criteria only; it is not a
third model turn in the existing sense and has no tasks branch.

### 7.2 Questions

One call per criterion. State is the criterion text plus its ticket's title and
objective, which are needed because rule 3's "set the criterion does not close"
is often named in the objective.

| Key | Type | Rule | Asks |
| --- | --- | --- | --- |
| `procedure` | choice | 1 | Which procedure would settle this? (§5) |
| `unbounded_absence` | noul | 3 | Does it assert that something is absent everywhere, without listing the places? |
| `joins_claims` | noul | 4 | Does it join two claims that could be separately true? |
| `self_gradable` | noul | 6 | Could the change's own author make this true without changing behavior? |
| `specificity` | score | — | How specifically does it name what must be true? |

### 7.3 Two-tier composition

Each signal is compared against two thresholds:

```
confidence >= block_min  -> emit a needs_clarification question; exit 3
confidence >= note_min   -> annotate the criterion; emit the graph
otherwise                -> record the raw answers only
```

This is the direct answer to the 36-of-128 result in §2: the question is no
longer whether a rule fires, but how certain the judgment is, and certainty is
tunable against labeled outcomes where a regex is not.

**Before the calibration study reports, `block_min` is 1.0**, which is
unreachable and makes the gate annotate-only. Enforcement is unlocked by
evidence, not by shipping.

Blocking questions use the existing shape exactly: `kind` is `undecidable` for
`procedure == "none"` (rule 1) and `unbounded` for `unbounded_absence` (rule 3);
rules 4 and 6 annotate and do not block, because neither makes a criterion
undecidable and both are cheaply fixed by a human reading the annotation.
`source_refs` is validated against `source_lines` as today, so a question must
cite a real line of the specification.

Whole-document refusal is unchanged: a document carrying questions writes no
tickets. Per-ticket partial emission remains the open question CRITERIA.md
already records, and is not resolved here.

### 7.4 Annotation

An annotated criterion carries its procedure classification, flagged rules, and
confidences into the emitted ticket, recorded in provenance rather than in the
criterion string. Nothing downstream reads it to make a decision; it exists for
the human reading the graph and for §11. Making a worker or reviewer prompt read
it would convert an advisory signal into an instruction, which §4.2 forbids.

## 8. Configuration, freezing, and recovery

### 8.1 Run configuration

One optional object in `run.json`, validated in `config.py` and
`schemas/run.schema.json` together, as AGENTS.md requires:

```json
"judgment": {
  "mode": "shadow",
  "policy": "judgment-policy.json",
  "endpoint": "https://api.typesafe.ai/v1/systemone",
  "timeout": 10.0
}
```

`mode` is one of `off`, `shadow`, `enforce`; omitted, the object is absent and
no judgment runs, preserving existing behavior exactly. `policy` names a
threshold set (§10); omitted, conservative built-in defaults apply and
`block_min` is 1.0. `endpoint` exists so a hosted or self-hosted deployment is a
configuration change rather than a code change.

`anvil prepare` takes the same settings as flags, since it has no run
configuration.

### 8.2 Freezing and recovery

A threshold set is frozen for a run exactly as a routing policy is: fingerprinted
and written to `judgment-frozen.json` in the run directory, with a
`judgment_frozen` ledger event recording the digest. Resume validates the digest
and reuses the frozen set; a policy that changed on disk mid-run does not alter
decisions already recorded.

Judgments themselves are never replayed on resume. A recorded judgment is
evidence about an attempt that already happened; a retired attempt's judgment is
retired with it, and a fresh attempt is judged fresh. This follows the existing
rule that candidates require fresh review and checks.

## 9. Credential isolation

`TYPESAFE_API_KEY` lives in the supervisor's environment. Without action it is
inherited by every agent turn, verification command, adaptive runner, preflight
probe, and Git subprocess — precisely the class of leak that
`test_environment.py::CanaryReachesNoLaunchedProcess` exists to catch, and
precisely what run `51fec4cd` was about.

The variable is therefore a **default contribution to
`config.credential_exclusion`**, not something an operator remembers to add. The
enumeration rule in AGENTS.md applies unchanged: the key must be withheld at
every launch site, and a new site that does not exclude it fails the suite.

A test asserts the key reaches no launched process, written against the existing
canary harness.

## 10. Product boundary

Resolved against [PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §1: a judgment call
requires a key and a network, not other people, so the capability runs correctly
on one machine and stays open.

| Layer | Side | Reason |
| --- | --- | --- |
| `judgment.py`, both question sets, conservative defaults, shadow mode, ledger events | **Open**, bring-your-own-key | §1: runs correctly on one machine. The user pays the model vendor directly. |
| Hosted inference — no key required, pooled rate limits, spend inside the subscription | **Paid** | Operated infrastructure and someone else's money. §3, "hosted execution." |
| Calibrated threshold sets trained on a pooled acceptance corpus | **Paid** | §5 exactly: requires a corpus larger than one repository produces. |
| Team-visible judgment history | **Paid** | §3, cross-machine run state. |

Two consequences worth stating plainly.

**The model is a commodity; the calibration is the asset.** A System One call is
inexpensive and available to anyone. What is not available to anyone is a
threshold set calibrated against criterion → evidence → verdict → checks →
branch-advanced tuples, which is the supervised-label asset
[PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §10 identifies as unreproducible by
observability vendors. A threshold set ships as an immutable, fingerprinted,
catalog-bound artifact through the mechanism §7 of that record already
specifies for pooled routing policy. No new product surface.

**Paywalling the capability would starve the asset.** Every run in shadow mode
produces the labeled tuples the threshold set is trained on. Gating the feature
would forgo that corpus to protect a negligible per-call cost, and — because the
concession check sits in the acceptance path — would ship the free tier the
reviewer behavior §2 measures as producing false accepts, which
[PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §9 names as an immediate test failure.

## 11. The calibration study

**No threshold is chosen in this document, and no consumer enforces anything
until this study reports.**

### 11.1 What the corpus actually contains

Inventoried 2026-09-18 against the state root at `~/.local/state/anvil`, read
from copies so nothing in the originals was touched.

| | Count |
| --- | --- |
| Saved run directories, all with readable ledgers | 35 |
| Runs with at least one review | 18 |
| Review results recorded | 46 |
| Review verdicts: `approve` / `request_changes` | 44 / 2 |
| Acceptance items across those reviews | 214 |
| Items marked `satisfied: true` / `false` | 213 / 1 |
| Distinct criterion texts in reviewed tickets | 210 |
| Acceptance criteria in the repository backlog (`tickets/`) | 138 |

**The outcome label is circular, and this is the finding that matters.** All 44
approved reviews belong to attempts that ended `done`. There are zero
exceptions, and there cannot be many: under the acceptance protocol an approved
review is one of the causes of `done`. Whether the attempt was accepted, whether
checks passed, and whether the branch advanced are therefore all downstream of
the judgment being evaluated, and none of them is an independent label for "this
review was right."

The consequence is that **the 213 satisfied-true items are unlabeled, not
negative.** An earlier draft of this section proposed measuring a false-positive
rate against "criteria from tickets that were accepted and whose branch
advanced." That number would have been computed against a variable the review
itself determines, and it would have looked excellent for reasons unrelated to
whether the check works.

**Confirmed positives: one.** Run `51fec4cd` attempt `3747d86a`, and it sits
inside a `request_changes` verdict, which is why §6.1 was rewritten. One positive
supports no recall estimate at all.

**The intake corpus is in better shape**, because criterion shape is labelable by
reading and needs no outcome. The 138 criteria in `tickets/` include the 128 in
`delivery-dashboard.json` that [CRITERIA.md](CRITERIA.md) measured the keyword
rule against, so the 36-of-128 baseline is directly reproducible and directly
comparable.

### 11.2 Two studies, not one

The inventory splits the study in two, with different feasibility.

**Study B — intake (§7). Runnable now.** Replay the §7.2 question set over all
138 backlog criteria. Label them by hand against rules 1, 3, 4 and 6; this is
bounded work a human can do in a sitting, and CRITERIA.md's existing analysis of
`apply-credential-exclusion` supplies worked examples of each rule. Report
precision and recall per rule against the hand labels, and the false-positive
rate against the 36-of-128 keyword baseline on the identical 128. Rules 3 and 4
are reported separately; their base rates differ enough that a pooled number
would hide both.

**Study A — acceptance (§6). Blocked on labels.** The corpus supplies one
positive and no usable negatives, so it cannot yield precision, recall, or a
false-positive rate today. Three ways forward, in the order I would try them:

1. **Hand-label a sample of the 213 items.** For each, decide from the criterion
   text, the evidence text, and the reviewer's tool set whether the review could
   have decided it. This is the same judgment the check makes, so it is
   self-consistent labeling rather than outcome labeling — weaker evidence, but
   honest about what it is.
2. **Mine subsequent human corrections.** Where a human commit fixed work an
   Anvil run had accepted — `04f270d` after `51fec4cd` is the known instance —
   the criteria that commit touches are candidate positives. This is the only
   genuinely independent label available, and it is rare.
3. **Collect forward in shadow mode.** Slices 5 and 6 write judgments without
   changing outcomes. Running them over new work accumulates the distributions a
   threshold needs, paired with whatever independent corrections follow. This is
   the reason shadow is the default and the reason §4.1 records full probability
   distributions rather than the collapsed confidence.

Route 3 is the honest answer and it is slow. It also means the acceptance-path
threshold is exactly the artifact §10 describes as paid: it cannot be derived
from one repository's history, because one repository's history does not contain
it.

### 11.3 Mechanics and the withdrawal condition

Both studies are offline scripts under `tools/`, not commands. They read ledger
copies, never the originals; they write raw distributions to a local file so a
later threshold can be re-derived without re-calling the service; and they
report cost and wall-clock at the observed criterion counts.

Study B runs first, before any module lands in `src/anvil/`. **If Study B does
not materially improve on 36-of-128, this specification is withdrawn**, and the
script should say so in its own results rather than leaving the conclusion to a
reader. Study A gates slice 7 only, and may remain open indefinitely; slices 5
and 6 are still worth landing in shadow, because they are how Study A ever
becomes possible.

## 12. Implementation slices and order

| # | Slice | Depends on | Lands |
| --- | --- | --- | --- |
| 1 | Study B (intake): replay §7.2 over the 138 backlog criteria, hand labels, comparison against the 36-of-128 baseline | — | `tools/`, results in §11 |
| 2 | `judgment.py`: `Judge`, question constructors, `ask`, injectable transport, modes | 1 | `src/anvil/judgment.py`, `tests/test_judgment.py` |
| 3 | Credential exclusion default and canary test | 2 | `config.py`, `environment.py`, `tests/test_environment.py` |
| 4 | Configuration: `judgment` object in `config.py` and `schemas/run.schema.json` together; freezing and resume validation | 2 | `config.py`, schema, `recovery.py` |
| 5 | Concession check in shadow: questions, composition, `judgment` ledger events, serial path | 2–4 | `execution.py`, `evidence.py` |
| 6 | Concession check in the worker pool: off-thread call, coordinator-owned recording | 5 | `parallel.py` |
| 7 | Concession enforcement: re-review at rank+1, reservation, `conceded_criterion`, telemetry category | 5, 6, and a passing **Study A** (§11.2) | `execution.py`, `parallel.py`, `adaptive_runtime.py`, `telemetry.py` |
| 8 | Intake gate, annotate-only (`block_min` 1.0) | 2–4 | `preparation.py` |
| 9 | Intake gate blocking tier | 8, and a passing **Study B** (§11.2) | `preparation.py`, `cli.py` |
| 10 | Threshold policy artifact: load, fingerprint, freeze, validate | 4, 7, 9 | `judgment.py`, `config.py` |

Slices 7 and 9 are gated on evidence, not on the preceding code being done, and
on *different* evidence. Study B is runnable today, so slice 9 has a path to
completion. Study A is blocked on labels the corpus does not contain (§11.1), so
slice 7 may stay open indefinitely — and slices 5 and 6 are still worth landing
in shadow, because they are the mechanism by which Study A becomes possible.

## 13. Acceptance and failure matrix

| Condition | Mode | Outcome | Tighten-only? |
| --- | --- | --- | --- |
| Service unreachable, no key, timeout, malformed response | any | proceed on the deterministic path; record the absence | yes — no gate changes |
| `mode: off` or object omitted | — | no call; behavior identical to today | yes |
| Concession detected | shadow | record only; attempt proceeds | yes |
| Concession detected | enforce | discard review, re-dispatch at rank+1 | yes — an accept becomes a re-review |
| Escalated review concedes again | enforce | stop for author, `conceded_criterion` | yes |
| No higher-ranked reviewer profile | enforce | stop for author, `conceded_criterion` | yes |
| Re-review unaffordable (invocation ceiling or soft budget) | enforce | stop for author; no overrun | yes |
| Review item marked `satisfied: false` | any | not assessed | yes — a false item is never revisited |
| Verdict `request_changes`, item marked `satisfied: true` | any | assessed; this is the one confirmed positive (§11.1) | yes |
| Both `evidence.concedes` and the concession check fire | enforce | concession check takes precedence; re-review rather than `unlisted_requirement` | yes — both outcomes are stops |
| Model asserts a criterion *is* satisfied | any | **ignored; no such composition exists** | enforced by §4.2 |
| Intake signal, confidence ≥ `block_min` | enforce | `needs_clarification`, exit 3, no tickets written | yes — refuses to emit |
| Intake signal, confidence ≥ `note_min` | shadow or enforce | annotate; graph emitted | yes — advisory only |
| Intake signal below `note_min` | any | raw answers recorded; graph emitted | yes |
| `block_min` 1.0 (pre-calibration) | enforce | gate is annotate-only | yes |
| Adversarial text in a ticket or diff | any | can at most cause a stop or a re-review | yes — §4.2 |

The last row is the one to re-check on every change. A composition that could
turn a judgment into an approval, a satisfied criterion, or a widened scope is a
defect in this specification, not a feature request.

## 14. What this does not establish

**That the concession check works.** One confirmed positive is one data point,
and §11.1 establishes that the saved corpus contains exactly one. The honest
state of the acceptance half of this proposal is a hypothesis about a single
recorded run, with no measurable false-positive rate available today.

**That the corpus can validate the acceptance half at all.** All 44 approved
reviews belong to attempts that ended `done`, because an approved review is one
of the causes of `done`. No outcome recorded in the ledger is independent of the
judgment being evaluated. Slice 7 is gated on labels that do not yet exist.

**That calibrated probabilities beat the reviewer being wrong less often.** A
better reviewer profile, or a prompt that states the reviewer's capabilities,
might reduce concessions more cheaply and was never tried. §11 should compare
against that baseline where saved runs allow it.

**That the intake gate improves outcomes.** CRITERIA.md already warns that
criterion shape is not ticket size and is not a model-capability problem:
attempt 5 ran the enumerated version, spent $4.01 and 118 turns, and produced no
candidate. A better-shaped ticket graph may not convert into accepted tickets.

**That the model's classifications are stable across versions.** Thresholds are
calibrated against one pinned model. A version change invalidates them, and the
recorded model ID is what makes that detectable rather than silent.

**That shadow mode is free.** It adds a network call per assessed item and a
ledger event per judgment. The cost is small but not zero, and §11 measures it.

**Nothing about the paid tier.** §10 states where the pieces land against the
existing product record. Pricing, packaging, whether pooled thresholds are
org-scoped or cross-customer, and what disclosure a cross-customer corpus
requires are all undecided, exactly as in
[PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §11.

## 15. Completion definition and later work

This milestone is complete when slices 1 through 10 are implemented, the §11
report is recorded in this document with its measured precision and
false-positive rate, and the acceptance matrix in §13 is covered by tests.
Completion means the two consumers behave as specified under a calibrated
threshold set — not that the underlying hypothesis about review quality has been
validated on live work, which requires runs this repository has not yet done.

Later consumers of `judgment.py`, in the order their evidence would justify:

- **Routing rank** (`routing.assess`): replace keyword lists with Scores at
  `feature_version` 2. A cost optimization rather than a correctness feature,
  with a different enforcement posture and an existing policy mechanism to
  integrate with; it warrants its own record.
- **Escalate / retry / stop** (`adaptive_runtime.next_profile`): today's
  escalation is positional and does not read why the attempt failed.
- **Automatic skill selection** (`skill_runtime._RULES`): a `judged` mode beside
  `rules`, requiring an amendment to
  [AUTOMATIC_SKILL_SELECTION.md](AUTOMATIC_SKILL_SELECTION.md), which currently
  promises the selector makes no model call.
- **Orientation context**: relevance-ranking paths and specification sections for
  `orientation_text`. Ranked last deliberately:
  [OBSERVED_LIMITS.md](OBSERVED_LIMITS.md) records that improving navigation cut
  cost 32% and completion did not follow, so this is a measurement to take, not
  a fix to ship.

Rule 6 of [CRITERIA.md](CRITERIA.md) — a criterion resting on tests the same
turn authored — needs the candidate diff as state rather than the evidence text,
so it is a separate question set and is not specified here.
