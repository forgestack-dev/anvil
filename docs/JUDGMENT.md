# Calibrated judgments over review evidence and criterion shape

Status: proposed specification, not ratified. No code here is implemented. It
specifies one new module and two consumers of it, plus the offline studies that
must precede either consumer enforcing anything.

Study B has run; §11.4 records its result. It withdrew the intake gate's
blocking tier and slice 9 with it, and left the gate an annotator. Study A, for
the concession check in §6, is blocked on labels the ledger cannot supply.

**Nothing here ships in either product.** §10 records the decision: this is
internal, flag-gated, unsupported, and absent from the open CLI and the paid
service alike, with §10.4 recording the trigger to revisit that.

§11.5 amends §11.4: rule 3's result did not survive review of its own labels and
is now unresolved. Rules 4 and 6 are unaffected. Recorded 2026-09-18.

## 1. Outcome and scope

Two places in Anvil decide something by matching keywords against ticket text,
and one place decides nothing at all where a decision is needed.

This document specifies `judgment.py`, a bounded client for a System One model
that returns calibrated probability distributions over answer sets the caller
defines, and two consumers:

- **A. The concession check** (§6), in the acceptance path. It detects a review
  that marked an acceptance criterion satisfied without the means to know, which
  is the one measured correctness defect this repository has recorded.
- **B. The intake gate** (§7), in `anvil prepare`. It annotates criteria against
  [CRITERIA.md](CRITERIA.md) rules 4 and 6, where §11.4 measured the model well
  ahead of any keyword rule. It blocks nothing: rules 1 and 3 were specified as
  blocking conditions and both were withdrawn on the evidence in §11.4.

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

**Not a product.** §10 keeps this out of both the open CLI and the paid service.
What follows specifies a capability for this organization's own use, and the
slices in §12 are scoped accordingly.

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
| `asserts_absence` | noul | 3 | Does it require that something is absent? |
| `names_the_places` | noul | 3 | Does it list the places it holds over? |
| `joins_claims` | noul | 4 | Does it state two requirements that could be separately true? |
| `about_tests` | noul | 6 | Is it a statement about tests? |
| `tests_preexist` | noul | 6 | Do those tests already exist? |
| `specificity` | score | — | How specifically does it name what must be true? |

Rules 3 and 6 are two questions each, combined in code, because each is two
judgments and asking for both at once produced a different question than the one
intended. §11.4 records what that cost: rule 6 asked as one question scored F1
0.00 and scored 0.88 decomposed, on the same model and the same criteria.

`asserts_absence`, `names_the_places`, `about_tests` and `tests_preexist` carry
structured criteria — an object per outcome with a meaning and worked examples —
rather than a sentence. The examples are invented rather than drawn from the
backlog, so no criterion appears inside the question that judges it.

### 7.3 Annotation only

The gate emits no questions and blocks no document. Every signal is recorded on
the criterion it describes and the graph is written as it would have been.

An earlier draft specified a two-tier gate: a signal above `block_min` became a
`needs_clarification` question and exited 3, a signal above `note_min`
annotated. Both blocking conditions are withdrawn, because §11.4 measured them
and neither survives.

**Rule 1 never fires.** `procedure == "none"` was returned for 0 of the 138
criteria in this repository's backlog. Nothing in the corpus is undecidable by
all three procedures, so the condition has no observed instance to block on.
This one is not in doubt: no label of `procedure` was revised at any point.

**Rule 3 is unresolved and cannot be resolved from this corpus.** It first
measured as a loss — F1 0.69 against 0.67 for a keyword rule tuned on the same
labels, inside the run-to-run variance. A later labeling pass found the labels
inconsistent, and on corrected ones it measures as a win at 0.79 against 0.69.
§11.5 shows that every point of that improvement comes from corrections made
after seeing the model's answers, so neither number is usable and the question
is open.

A gate is not shipped on evidence that cannot be trusted, in either direction.
Withdrawing the blocking tier on an unresolved rule is the conservative error:
it costs an annotation that could have blocked, where the opposite costs a
`prepare` that refuses correct tickets on a signal nobody has validated. If a
blind labeling pass later settles rule 3 in the model's favour, this section is
the thing to revisit, and §11.5 says what that pass has to look like.

That leaves no blocking condition this document can defend, so `block_min` is
removed rather than set
unreachably. A threshold that exists but can never be crossed invites someone to
lower it later without re-running the study.

What remains is worth having. Rules 4 and 6 are where the model wins and
vocabulary has nothing to grip — 0.83 and 0.88 against 0.66 and 0.18 — and rule
6 reaches precision 1.00 on this corpus. A criterion flagged as resting on its
own author's tests is worth showing whoever is reading the graph, and showing is
all this does.

`anvil prepare` therefore keeps exactly the exit codes it has today. The third
code stays reserved for the existing adversarial gate, which is unaffected by
any of this.

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

**The object is refused unless `ANVIL_INTERNAL_JUDGMENT` is set in the
supervisor's environment** (§10.3). A run configuration carrying it without the
flag fails validation with a message saying the capability is internal, rather
than being silently ignored — a field that is quietly dropped teaches an
operator that it worked.

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

## 9. Credential isolation and outbound disclosure

### 9.1 The key must not reach a launched process

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

### 9.2 Criterion text and review evidence leave the machine

§9.1 is about a credential not escaping into a subprocess. This is the opposite
direction and was missing from earlier drafts of this document.

Every call sends real repository content to a third party. The intake gate sends
acceptance criteria, which on a private repository describe unreleased work. The
concession check sends criterion text together with a reviewer's evidence, which
quotes the diff. Anvil runs on private repositories by default and nothing else
it does makes a network request with repository content in it: `run`, `resume`,
`status` and `serve` are local, and the agent adapters send content to a
provider the operator has already chosen and configured. A judgment call would
be the first time Anvil originates an outbound disclosure of its own.

That is an operator's decision and it must be presented as one:

- **Off unless asked.** Absent configuration means no call, which §8.1 already
  specifies. This section makes it a disclosure requirement rather than only a
  default.
- **Say it where it is turned on.** The configuration field's documentation
  states what is transmitted, to which endpoint, and under whose terms — not
  that a judgment is "calibrated."
- **`anvil doctor` reports it.** An operator inspecting a configured run learns
  that this run will send criterion text off the machine, in the same place it
  learns which agent binary will be launched.
- **The ledger records what was sent.** §6.5 already records answers; it records
  the state that produced them for the same reason, so an audit after the fact
  can establish what left rather than infer it.
- **No second endpoint by configuration alone.** §8.1 makes `endpoint`
  configurable for a self-hosted deployment. That field must not become a way to
  redirect repository content somewhere unreviewed without the operator seeing
  it in `doctor`.

The vendor's own commitments — no training on user data, retention terms, zero
data retention for enterprise — are recorded in their Data Processing Agreement
and are theirs, not Anvil's. Anvil's obligation is to make the transmission
legible before it happens. A tool whose central claim is that acceptance rests
on evidence cannot be casual about shipping that evidence to someone else.

## 10. Internal use only

**This capability ships in neither product, for now.** It is not a feature of
the open CLI and it is not a feature of the paid service. It exists for this
organization's own work on Anvil, behind a flag that is off, undocumented as a
product capability, and unsupported.

The exclusion is time-limited by intent: §10.4 records the trigger for reopening
it, which is Jev leaving early access.

### 10.1 Why not the open CLI

[PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §1 would permit it: a judgment call
needs a key and a network, not other people, so by the stated rule it belongs on
the open side. Permitted is not the same as warranted, and three measured facts
say it is not warranted here.

The prize is smaller than the proposal assumed. §11.4 withdrew the blocking
tier; what survives is an annotator for two of four rules. That is worth having
and it is not worth a vendor dependency in the half of the product that must
stand alone.

The dependency is not substitutable. There is no second System One provider.
Anvil's agent adapters treat provider plurality as load-bearing — Codex, Claude
Code and Muse, with the default preserved across configurations — and this would
be the only place in the open CLI with one supplier and no alternative.

The service is in early access with no documented free tier. An open-source
feature most people who clone the repository cannot run is not a capability; it
is an advertisement, which is the §9 failure mode of that record read from the
other direction.

### 10.2 Why not the paid service

The earlier draft of this section proposed pooled threshold sets as the paid
artifact, on the argument that the model is a commodity and the calibration is
the moat. §11.4 undercut it. The thresholds that survived calibration belong to
rules 4 and 6, which annotate; a calibrated threshold for a gate that was
withdrawn has nothing to gate. The pooled-threshold product needs the concession
check in §6 to be real, and §11.1 established that this repository's ledger
cannot produce the labels to establish that it is. Selling calibration is
downstream of Study A, which has not run and may not be able to.

### 10.3 What internal means

The `judgment` configuration object is refused unless the supervisor's
environment carries an explicit internal flag. Configuration alone cannot turn
it on, so no published example, copied run file, or documentation snippet
enables it by accident.

- Not in `README.md`, not in `skills/anvil/SKILL.md`, not in `examples/`.
- Absent from `anvil doctor`'s ordinary output; reported only when the flag is
  set, and then per §9.2.
- No support commitment, and no compatibility guarantee across versions.
- `tools/study-b.py` is unaffected. It is a development script that reads the
  backlog and writes a report, not a run-time path, and it is already internal
  by being a tool rather than a module.

Two consequences to design against rather than discover. The schema and its
runtime validation must agree — `test_run_config.py::SchemaMatchesRuntimeValidation`
enforces that — so a field the schema describes and the runtime refuses without
an environment flag needs the refusal expressed where that test can see it.
And `test_invariants.py::DocumentedSubcommandTests` checks the CLI against the
README and entry skill, so an internal capability must add no subcommand.

**This is a public repository.** Internal here means unsupported and
unadvertised, not unpublished: the code, if written, is readable by anyone. The
alternative worth weighing is keeping it out of `src/anvil/` altogether and
leaving it as tooling, which is what it is today and costs nothing to continue.

### 10.4 When to revisit

**This is a deferral, not a permanent exclusion.** The intended end state is that
the capability opens to everyone once Jev leaves early access. Recording the
trigger matters more than recording the decision: without it, a temporary "no"
becomes a permanent one because nobody remembers what would have changed it.

General availability settles one of §10.1's three objections and leaves two
standing, so it is the moment to reopen the question rather than the answer to
it:

| Objection | Settled by general availability? |
| --- | --- |
| Early access, no documented free tier | **Yes.** This is the objection GA exists to remove. |
| The prize is an annotator, not a gate | No. Settled by Study A succeeding, or by the annotator proving useful in internal practice. |
| One supplier, no second source | No. Softened, though, by the capability costing nothing when absent: a vendor that disappears removes a feature and breaks no run. |

The second is the one to watch. If §11.4's annotation has not changed what
anyone writes or catches by the time GA arrives, opening it makes a feature
nobody uses available to more people, which is not an improvement. The evidence
for that is internal use between now and then, which is the reason to run it
internally rather than to shelve it.

The third is close to answered already. §4.1 returns `None` on any failure and
every caller has a deterministic path, so losing the vendor costs the annotation
and nothing else. That is a weaker dependency than the agent adapters, where
losing a provider costs the ability to run at all — and those are already open.

So the expected path is: GA arrives, internal use has shown whether the
annotator earns its place, and §10.1 is rewritten against
[PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §1, which already permits it. Nothing
in §9.2 relaxes on that path; an open capability that sends repository content
to a third party needs the disclosure more than an internal one does, not less.

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

### 11.4 Study B result, recorded 2026-09-18

`tools/study-b.py`, model `jev-1.13.0`, 138 hand-labeled criteria, three runs of
138 calls each. 414 calls, 0 failures, about 0.4 seconds per call, **$0.012
total**. Cost is not a consideration at this scale and should not be presented
as one.

| Rule | Role | Best keyword rule | Model | |
| --- | --- | --- | --- | --- |
| 1 — `procedure == none` | blocks | — | **never fires** | 0 of 138 |
| 3 — unbounded absence | blocks | **0.67** | 0.69 | inside noise |
| 4 — joins claims | annotates | 0.66 | **0.83** | model wins |
| 6 — self-gradable | annotates | 0.18 | **0.88** | model wins, precision 1.00 |

F1 against the hand labels. The keyword rule is not a guess at the lexicon
ACCEPTANCE.md rejected — that lexicon is not recoverable, since only its result
was recorded and 109 subsets of the obvious vocabulary fire on exactly 36 of the
128. It is instead the best rule found by exhaustive subset search **against the
same labels it is scored on**: an upper bound no honestly-written lexicon
reaches.

**The rejected lexicon was right to be rejected, and now there is a number for
it.** Scoring all 16 lexicons that fire on exactly 36 of the 128 gives precision
between 0.19 and 0.44 against 19 actual rule-3 violations. Of the 36 criteria it
flagged, roughly 7 to 16 were real.

**Question quality dominates model capability, and fails silently.** Rule 6
asked as one question — could the author satisfy this by writing a test —
scored F1 0.00: not merely mis-thresholded but inverted, ranking the clear cases
below the unclear ones. Nothing errored; the answers were well-formed and
confidently wrong. Split into two literal questions and combined in code, the
same model on the same criteria scored 0.88 at precision 1.00. This is the most
important operational finding in the study, and §4.1's shadow default exists for
it.

**Rule 3 is unresolved, and §11.5 records why it cannot be settled from these
labels.** The same decomposition applied to rule 3 moved it 0.41 to 0.69 against
a 0.67 baseline, a margin inside the run-to-run variance. That reading was
recorded here first as a loss, on the strength of `asserts_absence` at 0.95
returning precision 0.57 and recall 0.81 — the same two figures as `no+never`.
A later labeling pass found the labels behind it inconsistent, and correcting
them changed the result. It also destroyed the ability of this corpus to decide
the question. Read §11.5 before citing any rule 3 number from this document.

Rule 6 was a broken question hiding a real capability. Whether rule 3 is a real
capability with nothing to add, or another badly-asked question, is not
established.

A rule was drafted from the rule 3 result and is worth stating with its support
withdrawn, because the reasoning still looks sound and only the evidence is
gone: **where a signal can be written as a keyword list that roughly works, the
lexicon may already be the ceiling; where it cannot be described lexically, the
model has room.** Rule 6 supports the second half — vocabulary carries almost
nothing about whose tests a criterion rests on, and the model scored 0.88
against 0.18. The first half rested on rule 3 and no longer has evidence. Treat
it as a hypothesis worth testing against `routing.assess` and
`skill_runtime._RULES`, not as a finding that predicts their outcome.

**Calibration behaves as documented.** `procedure` accuracy rises 0.80, 0.84,
0.85, 0.91, 1.00 as the confidence floor rises 0.50 to 0.90, at coverage falling
from 62% to 12%. That monotonicity is what makes confidence-gating an
architecture rather than a hope, and it is the single strongest argument for the
concession check in §6, which leans on the same question.

**Run-to-run variance is about 0.02 F1**, measured from two runs of an unchanged
rule 3 question scoring 0.36 and 0.34. `tools/study-b.py` refuses to call a
margin under 0.05 a win.

### 11.5 The rule 3 labeling pass, and what it cost

Recorded 2026-09-18, after §11.4.

Reviewing the criteria where the model and the labels disagreed on rule 3
exposed an inconsistency in the labels themselves. CRITERIA.md rule 3 asks
whether an absence names **the set it holds over** — "name the launch sites, the
call sites, the modules." Several criteria of the form "performs no remote
writes or model calls" had been labeled compliant because the forbidden *kinds*
were listed. The kinds are not the places. Nine labels were corrected on that
reading, raising rule 3 positives from 21 to 30.

On the corrected labels the model scores 0.79 against a keyword baseline of
0.69: a margin of 0.10, twice the noise band, and the opposite of what §11.4
first recorded.

**That result is not usable, and the reason is worth more than the result would
have been.** The corrections were made after seeing which criteria the model
flagged. Reverting them selectively shows where the improvement comes from:

| Labels | Positives | Model | Keyword | Margin |
| --- | --- | --- | --- | --- |
| Original | 21 | 0.69 | 0.67 | +0.03 |
| Only corrections the model did **not** flag | 24 | 0.65 | 0.66 | **−0.00** |
| Only corrections the model **did** flag | 27 | 0.84 | 0.70 | +0.13 |
| All nine | 30 | 0.79 | 0.69 | +0.11 |

Every point of improvement comes from the corrections that moved a label toward
the model's answer. The three corrections made independently — criteria the
model never flagged, found by applying the rule evenly — move the margin to
zero.

The independence checks that felt sufficient at the time were not. Three of the
nine corrections were to criteria the model missed, and five of its eleven
disagreements were examined and rejected. Both are true, and the table shows
neither carries signal. **A disagreement review cannot validate itself by
counting how often it disagreed back.** The sensitivity analysis above is what
detects the problem; nothing short of it does.

So neither reading stands. The §11.4 claim that rule 3 loses rests on labels now
known to be inconsistently applied. The claim that it wins rests on labels
revised after seeing the answers. Rule 3 is unresolved and this corpus can no
longer resolve it, because the labels have seen the model's output and cannot
un-see it.

**What would settle it:** a blind pass over all 138 criteria against the
sharpened reading of rule 3, by someone with no access to the model's answers,
scored once. That is the independent human labeling §11.2 asks for, and rule 3
is now the place it is most needed.

**What is unaffected:** rules 4 and 6. No label of either was revised, in this
pass or after it, so 0.83 and 0.88 stand as §11.4 records them. The methodological
finding does not reach them; it reaches exactly the rule whose labels moved.

**What this cost in practice:** §7.3 withdrew the blocking tier partly on the
rule 3 result. That withdrawal stands, for the reason given there — rule 1 never
fires, and a gate is not shipped on evidence that cannot be trusted — but it is
now a decision under uncertainty rather than a decision on a measurement, and
§7.3 says so.

#### What the result does not establish

Every label is the author's, produced by a model, and at least one is probably
wrong: rule 6's remaining misses include a criterion that never mentions tests,
where the label is the more questionable half. The labels are not an independent
human judgment, which is what §11.2 asks for, so these numbers describe the
agreement between two models rather than agreement with ground truth. Three
criteria carry an external check — CRITERIA.md classifies them by hand, and all
three agree with the label — which is reassurance, not validation.

Rule 6's thresholds are the most overfit numbers here: two cutoffs swept
together against nine positives, in-sample. Its precision of 1.00 across 129
negatives is more trustworthy than its recall of 0.78.

Nothing here bears on §6. Study B measured intake only.

## 12. Implementation slices and order

| # | Slice | Depends on | Lands |
| --- | --- | --- | --- |
| 1 | Study B (intake): replay §7.2 over the 138 backlog criteria, hand labels, comparison against the 36-of-128 baseline | — | `tools/`, results in §11 |
| 2 | `judgment.py`: `Judge`, question constructors, `ask`, injectable transport, modes | 1 | `src/anvil/judgment.py`, `tests/test_judgment.py` |
| 3 | Credential exclusion default and canary test | 2 | `config.py`, `environment.py`, `tests/test_environment.py` |
| 4 | Configuration: `judgment` object in `config.py` and `schemas/run.schema.json` together, refused without the §10.3 environment flag; freezing and resume validation | 2, 3 | `config.py`, schema, `recovery.py` |
| 5 | Concession check in shadow: questions, composition, `judgment` ledger events, serial path | 2–4 | `execution.py`, `evidence.py` |
| 6 | Concession check in the worker pool: off-thread call, coordinator-owned recording | 5 | `parallel.py` |
| 7 | Concession enforcement: re-review at rank+1, reservation, `conceded_criterion`, telemetry category | 5, 6, and a passing **Study A** (§11.2) | `execution.py`, `parallel.py`, `adaptive_runtime.py`, `telemetry.py` |
| 8 | Intake gate, annotate-only: rules 4 and 6 recorded on each criterion | 2–4 | `preparation.py` |
| 9 | ~~Intake gate blocking tier~~ — **withdrawn**, §11.4 | — | — |
| 10 | ~~Threshold policy artifact~~ — **withdrawn**, §10.2: no pooled artifact without a paid tier to carry it | — | — |

Slice 9 is withdrawn rather than pending. Study B ran, both blocking conditions
failed, and §11.4 records why; the slice is kept in the table struck through so
a later reader finds the result rather than the gap.

Slice 7 remains gated on Study A, which is blocked on labels the corpus does not
contain (§11.1), so it may stay open indefinitely. Slices 5 and 6 are still
worth landing in shadow, because they are the mechanism by which Study A
becomes possible.

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
| Intake signal, rule 4 or 6, above `note_min` | shadow or enforce | annotate; graph emitted | yes — advisory only |
| Intake signal below `note_min` | any | raw answers recorded; graph emitted | yes |
| Intake signal, rule 1 or 3 | any | recorded, never acted on (§11.4) | yes |
| Any intake signal, at any confidence | any | **never blocks emission** | yes — the gate cannot refuse |
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
ledger event per judgment. §11.4 measures it at about 0.4 seconds and $0.00003
per criterion, which is small but not zero.

**That a question asked once is a question asked well.** §11.4's central result
is that the same model, on the same input, scored 0.00 and 0.88 on one rule
depending only on how the question was written — and that the bad version raised
no error. Any application of this module needs a labeled set before its answers
are trusted, not only before they are enforced.

**Nothing about the paid tier.** §10 states where the pieces land against the
existing product record. Pricing, packaging, whether pooled thresholds are
org-scoped or cross-customer, and what disclosure a cross-customer corpus
requires are all undecided, exactly as in
[PRODUCT_BOUNDARY.md](PRODUCT_BOUNDARY.md) §11.

## 15. Completion definition and later work

This is no longer a product milestone. §10 keeps it out of both offerings, so
there is nothing here to ship and no release it gates.

The work is complete when slices 1 through 8 are implemented — 9 and 10 are
withdrawn — and the acceptance matrix in §13 is covered by tests. Slice 1 is
done and its result is §11.4. A reasonable outcome is that nothing past slice 1
is ever built: the study is the part that produced knowledge, and it has.

Completion means the intake gate annotates as specified and the concession check
records in shadow. It does not mean the concession check enforces: slice 7 waits
on Study A, and §11.1 establishes that this repository's ledger cannot produce
the labels Study A needs, because every recorded outcome is downstream of the
review being judged. A milestone that completes with §6 still in shadow is the
expected outcome, not a shortfall.

Neither does completion mean the hypothesis is validated. §11.4 measured which
rules a model reads better than a regex. Whether annotating those rules changes
what an author writes, or what a run accepts, is unmeasured and needs live runs
this repository has not done.

Later consumers of `judgment.py`, if it is ever written, in the order their
evidence would justify. Each carries §10's posture: internal, flag-gated, and
not a product capability unless that decision is revisited with new evidence.

- **Routing rank** (`routing.assess`): not as a replacement classifier. §11.4's
  rule-3 result is a direct warning — `assess()`'s risk words are a lexicon, and
  a lexicon may already be its own ceiling. The shape worth trying instead is
  scores as *features* for the trainer `learning.py` already has, since this
  repository does hold labeled routing outcomes even though it does not hold
  labeled review outcomes. That is a different proposal and warrants its own
  record.
- **Escalate / retry / stop** (`adaptive_runtime.next_profile`): today's
  escalation is positional and does not read why the attempt failed.
- **Automatic skill selection** (`skill_runtime._RULES`): a `judged` mode beside
  `rules`, requiring an amendment to
  [AUTOMATIC_SKILL_SELECTION.md](AUTOMATIC_SKILL_SELECTION.md), which currently
  promises the selector makes no model call. Carries the same §11.4 caution as
  routing rank: six regexes over ticket text may already be at the lexical
  ceiling, and that is measurable before anything is built.
- **Orientation context**: relevance-ranking paths and specification sections for
  `orientation_text`. Ranked last deliberately:
  [OBSERVED_LIMITS.md](OBSERVED_LIMITS.md) records that improving navigation cut
  cost 32% and completion did not follow, so this is a measurement to take, not
  a fix to ship.

Rule 6 of [CRITERIA.md](CRITERIA.md) — a criterion resting on tests the same
turn authored — needs the candidate diff as state rather than the evidence text,
so it is a separate question set and is not specified here.
