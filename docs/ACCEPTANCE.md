# The acceptance decision

Status: Stages 1 to 4 implemented.
The stage order was revised on 2026-09-17 after Stage 1 landed:
bounding and locating a rejection now precedes the conceded-rejection stop,
because the stop's trigger is vacuous until the acceptance map is mandatory.
Recorded 2026-09-17, from the saved ledgers of runs `5abe55ea`, `3658a8d9`,
`51fec4cd` and `55bca6a2`. The measurements and the correction they forced are
in [OBSERVED_LIMITS.md](OBSERVED_LIMITS.md).

## What the problem is not

The problem is not that independent review rejects good work. That reading was
recorded in `OBSERVED_LIMITS.md` and it did not survive the ledgers: of the two
rejections in run `51fec4cd`, one rejected a candidate that fails its own tests
and still launched the agent binary with excluded values present, and the other
named the exact two-line hunk the eventual human fix applied. Both rejections
were right to reject. The acceptance rule held and nothing wrong merged.

This matters because the obvious fixes for a reviewer that is "too strict" are
dangerous here. Designs that let a candidate merge when every listed criterion
is marked satisfied, or that demote a finding citing no criterion to a note,
both merge `a371221c`: the reviewer marked all three criteria satisfied on a red,
leaking candidate. A reviewer with only Read, Glob and Grep conceded "existing
tests remain green" from the diff alone, because it cannot run anything.

The unconstrained thing is not what a reviewer may reject on. It is what a
reviewer is asked to judge, and in what order.

## What the problem is

Four defects, each visible in the ledger.

1. **The cheapest and most trustworthy signal runs last.** `verify()` runs after
   the review, so a reviewer that cannot execute anything is asked to judge
   candidates the configured checks would already have rejected, and its
   per-criterion map is never falsified where it could have been. Attempt 3 cost
   $1.03 to review a candidate the run's own command rejects in 54 seconds.
2. **Every `request_changes` is one undifferentiated event.** A rejection that
   concedes every criterion and a rejection bound to an unmet criterion are
   indistinguishable to every consumer: both escalate the worker profile, both
   enter learning as worker insufficiency, and the retry prompt appends the
   finding as free text, enlarging the ticket's effective scope inside the run
   while its frozen criteria are unchanged.
3. **Findings carry no criterion and no location.** `REVIEW_SCHEMA.findings` is
   a bare string array, and `validate_result` accepts a rejection whose
   acceptance map is empty. Nothing downstream -- the run error, the published
   ticket reason, the retry prompt, telemetry -- can say which criterion failed
   or where.
4. **A criterion has nowhere to declare the set an absence must hold over.**
   `acceptance_criteria` is free text, so "every launch site" is settled once per
   review.

## The invariant

Independent review judges only candidates that already pass the configured
checks on the exact integration revision; a rejection names the criterion and
the location it fails; a rejection that concedes every criterion stops the run
for the ticket author with the unlisted requirement recorded, rather than
escalating a worker or enlarging the ticket; and nothing merges over a
reviewer's objection.

## Stage 1: checks before review (implemented)

`_PHASES` becomes `running, candidate, verified, reviewed, integrating, done`.
After `prepare_integration`, the supervisor runs the configured verification
commands on the integration revision, asserts the revision, and transitions
`verified`. Only then is the reviewer dispatched. A candidate that fails the
checks is never reviewed.

`store._require_evidence` needs no change: it compares `_PHASES` indexes, so
reordering the tuple reorders the evidence requirements with it. `verified` now
requires a candidate sha and a non-empty verification list; `reviewed`
additionally requires a review object. `record_rejection` and `retry_attempt`
accept `{candidate, verified}` in place of `{candidate, reviewed}`.

Recovery needs no protocol bump. `_accepted` compares `reviewed_sha`,
`verified_sha` and `integration_sha` as a set and reads no phase order;
`_candidate` refuses reuse on any recorded failure category or `request_changes`
verdict, which is unaffected. A run interrupted between `verified` and the
review resumes by re-verifying and re-reviewing the reused commit, as it already
does.

The reviewer is told that the configured checks have already run on this exact
revision, and nothing more: it is given no check records. Supplying passing
check output invites approving a criterion on an exit code, and `04f270d`
records a green suite that contained a test which could not fail.

Cost accounting changes with the order. A verification failure now happens
before any review is dispatched, so `Session.settle` must not charge the
reserved reviewer cost for a review that never started, or a run's soft budget
is consumed by invocations that never happened.

What this would have done to the measured run: attempt 3's candidate fails the
run's own verification command, so its review never runs. The ledger records
`verification_failure` naming the failing tests instead of `review_rejection:
README`. Escalation to `demanding` proceeds on a real defect; attempt 4 runs as
recorded, is green, and its review rejects on `cli.py:173`. Saved: $1.03 and,
more importantly, the false record.

## Stage 2: bound and located rejections (implemented)

A finding carries the criterion number it fails and a `file:line` that exists at
the reviewed revision, and **a rejection must return a full acceptance map**.
This is auditability and the input the later stages need; it is **not** a bound
on what a reviewer may reject on, because the criterion number is
reviewer-chosen. A reviewer determined to reject on documentation can bind it to
any criterion.

The measured run's error would have read `criterion 1 (src/anvil/cli.py:173):
pass exclude=config.credential_exclusion to routing.preflight` in the run error,
the published ticket and any retry prompt, instead of a 700-character paragraph.

### As implemented

`REVIEW_SCHEMA.findings` are objects carrying `criterion`, `finding` and
`location`. `validate_result` requires a full acceptance map for *both* verdicts
and checks each finding's shape, its criterion against the ticket, and its
location's form. The supervisor then resolves every location against the
reviewed revision with `execution.assert_located`, which refuses a path the
revision does not contain and a line past the end of a real file. Formatting
moved into `evidence.format_findings`, so the run error, the published ticket
reason and any retry prompt all read the same way.

Three decisions the stage description did not settle:

- **A location may name a directory.** A finding about something missing has no
  line to point at, and requiring one would push reviewers toward inventing a
  plausible number. A path alone is accepted; a line is checked when given.
- **A finding may cite a criterion the same review marks satisfied.** Refusing
  that would make Stage 3 unreachable, because a conceded rejection is exactly a
  finding whose criterion is satisfied, and it would arrive as a contract error
  instead of a question for the ticket's author. The contract permits it on
  purpose, and a test pins the behavior.
- **The coordinator decides every review verdict.** A non-adaptive reviewing
  thread used to reject on its own. It cannot resolve locations, because that is
  Git work a thread must not do, and its rejection cancelled the scope, which
  made the coordinator's own Git unreliable before it ever read the verdict. So
  the thread now validates shape and returns, and both pool modes reach the same
  coordinator branch as the serial path.

### Why this precedes the conceded-rejection stop

This stage and Stage 3 were originally ordered the other way round. They are not
independent, because `validate_result` in `evidence.py` is strict in one
direction only: an `approve` needs full criterion coverage, every criterion
satisfied and no findings, while a `request_changes` needs only a non-empty
findings list. Its acceptance map may be `[]`.

Stage 3 triggers on a rejection whose every criterion is marked satisfied. Over
an empty map that is *vacuously* true, so Stage 3 shipped first would fire on
rejections that assessed nothing at all -- stopping the run for the author on
precisely the signal that carries no assessment. Requiring the full map first
turns Stage 3's trigger into the positive assertion it is supposed to be.

## Stage 3: a conceded rejection stops the run (implemented)

When a reviewer returns `request_changes` with every criterion marked
`satisfied: true`, the requirement it names is not in the ticket. Escalating a
worker cannot satisfy it, and appending it to a retry prompt enlarges the
ticket's scope without changing its frozen criteria.

Such a rejection ends the ticket for the author: `blocked` with a distinct
failure category, the finding recorded as a proposal, and no escalation. Keyed on
the reviewer's own satisfied map, which is a positive assertion it makes against
its own verdict, not on a judgment about the finding's content -- and, after
Stage 2, a map it is obliged to fill.

This stage must not ship before Stage 1. Without checks-first it would have
stopped run `51fec4cd` after attempt 3 at $4.32 while calling "criteria met" a
candidate that fails its own tests and leaks credentials at the preflight probe.
Nor before Stage 2, for the reason above. Both orderings are load-bearing.

### As implemented

`evidence.concedes` is the test: a `request_changes` whose acceptance map is
non-empty and every entry satisfied. Stage 2 obliges a review to fill that map,
so this is a positive assertion the reviewer makes against its own verdict.
Both schedulers record `failure_category: "unlisted_requirement"` on the
attempt, stop with `blocked`, and skip the retry entirely -- in the pool the
conceded branch is taken *instead of* `retry`, so no escalation is reserved and
no finding reaches a retry prompt.

`evidence.rejection_reason` gives the author the framing rather than the
mechanism: "every acceptance criterion is satisfied, so this names a requirement
the ticket does not carry", followed by the bound and located finding.

Telemetry treats it as evidence about the ticket, not the worker.
`attempt_record` no longer counts a conceded rejection as an attributable
rejection, so its evaluation is `insufficient_evidence` and `learning` drops the
sample. Charging it to the profile would train routing on the ticket's
incompleteness, which is the one thing a stronger model cannot fix.

No new status and no schema change. A blocked run already exits 3, which is the
code `prepare` uses for a question, so a conceded rejection reaches a caller as
a question rather than as an error without inventing a second convention.

## Stage 4: declared sites (implemented)

A typed home for the enumeration `OBSERVED_LIMITS.md` already asks authors to
write, with paths checked to exist at the base revision and read verbatim by
both roles. It consumes the located findings Stage 2 produces. It bounds the
discovery cost of an incomplete enumeration: an undeclared site costs one
attempt and returns as a named authoring decision.

It cannot check completeness, and this is not a small caveat. The human's own
rewritten enumeration omitted `cli.py`, which is the site that ended the
measured run.

### As implemented

A ticket may carry `sites`: per criterion, the paths that criterion's claim
holds over. Criteria stay plain strings, so this is not the typed-criteria
design this document rejects; the enumeration sits beside them and is optional.
A criterion that declares nothing stays unbounded, which is every ticket written
so far.

`execution.assert_sites` resolves every declared path against the base revision
before a run directory or a ledger exists, so a stale or mistyped enumeration
raises like an invalid ticket graph rather than failing a started run. A site
therefore names a place that already exists: the enumeration is about where an
existing property must hold, not about files the ticket will create.

`evidence.undeclared_findings` consumes Stage 2's locations. When a rejection's
finding names a criterion that declared sites, and its location falls outside
them, the run records `failure_category: "undeclared_site"`, stops, and does not
escalate -- another attempt would work to the same declared set. A declared
directory contains the paths beneath it. As with a conceded rejection, telemetry
treats it as evidence about the ticket rather than the worker.

Both prompts carry the list verbatim, and both are told what it means: the
worker treats a criterion's sites as its full extent instead of searching for
more, and the reviewer reports what it finds outside them as a finding anyway,
so the supervisor can return it as a scope decision. Withholding it would trade
a bounded argument for a silent one.

The authoring side of this stage is rule 3 of [CRITERIA.md](CRITERIA.md), which
also proposes asking the question at intake, where `prepare` currently has no
way to decline.

## What no stage solves

- **Whether a site is in scope.** `anvil doctor --config` is not a run, yet the
  maintainer agreed with the reviewer that it belonged. No coordinator decides
  that. The plan makes the disagreement cost one attempt and arrive as a named
  file and criterion.
- **Enumerating the set an absence holds over.** This remains the design work.
- **Ticket size against one invocation's budget.** Attempt 5 spent $4.01 and 118
  turns on the enumerated ticket and produced no candidate. That is authoring and
  configuration.
- **Whether a reviewer fills its satisfied map honestly.** No schema checks
  relevance. Stage 1 makes the map falsifiable only in the direction the
  configured checks cover.
- **Worker-authored tests that cannot fail.** Stage 1 catches tests that cannot
  pass, for free. A test that passes vacuously is caught only by a reviewer
  reading it.
- **A rejected candidate is still discarded.** Attempt 4 was two lines short of
  the human fix, and no stage lets a run apply two lines to it. Amend retries are
  specified in [AMEND_RETRIES.md](AMEND_RETRIES.md), which consumes the bound and
  located findings stage 2 produces, and are not implemented.

## Rejected

- **Merging when every criterion is satisfied, demoting unbound findings to
  notes.** Merges `a371221c`.
- **Removing the reviewer's vote over machine-decidable criteria.** Breaks
  independent review and makes worker-authored tests self-grading.
- **Executable per-criterion commands carried in ticket files.** The ticket file
  sits inside the target repository and is worker-writable when `ticket_status`
  is off, so ticket content would become host argv.
- **Token assertions over the tree (`tree_lacks`).** Token presence in a file is
  not "this launch passes the filtered environment"; a comment satisfies it.
- **A lexicon refusing open-set tickets at validate.** Fires on 36 of 128
  criteria in this repository's own backlog, including three done tickets.
- **Feeding passing check records to the reviewer.** Invites approving a
  criterion on an exit code.
- **A cross-run grounds ledger.** Adopts prior-run review conclusions as binding
  inputs, against "old artifacts are never adopted".
- **Prompt-only fixes.** The measured prompt already said "findings only for
  actionable changes" and the attempt-3 reviewer obeyed it. Prompt text is not a
  mechanism.
