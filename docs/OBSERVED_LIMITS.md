# Observed execution limits

Measurements from running this repository's own ticket graph through Anvil.
They are evidence, not specification, and each section records what it does not
establish. Nothing here is a fix.

## The Claude adapter turn budget

Recorded 2026-09-16 against `main` at `293ad94`, from four real invocations.

### What happened

Four Claude Code implementation turns were dispatched against tickets in
`tickets/delivery-dashboard.json`, on this repository as the target. Every turn
that produced a result ended the same way.

| Run | Ticket | Turns | Reads/searches | Edits | Cost | Terminal reason |
| --- | --- | --- | --- | --- | --- | --- |
| `bc5a107b` | `strip-integration-credentials` | 33 | 29 | 6 | $2.08 | `max_turns` |
| `bc5a107b` | `ledger-query-layer` | — | — | — | — | interrupted by the sibling failure |
| `bbc8fc75` | `ledger-query-layer` | 33 | 29 | 6 | $4.23 | `max_turns` |
| `78817f4a` | `ledger-query-layer` (after adding `CLAUDE.md`) | 33 | 33 | 8 | $2.88 | `max_turns` |

No ticket reached independent review or verification. The baseline suite passed
before each run, so the target tree was green every time.

### What the numbers say

Roughly 80% of each invocation went to orientation rather than implementation.
The third run is the informative one: `CLAUDE.md` was added specifically to cut
orientation cost, and the worker did find it on its fourth tool call and go
straight to the right module. Reads still rose. The recovered turns were spent
reading the 712-line specification the ticket's `source_refs` point at, and
locating the state root. Cost fell 32%, which is real, but completion did not
follow. Better navigation changed how the budget was spent, not whether it
sufficed.

Ticket size was not the binding constraint either. `ledger-query-layer` is four
acceptance criteria, `risk: low`, no dependencies, and one new module. It failed
identically to a ticket touching five files. Orientation cost is roughly fixed
per invocation, so splitting a ticket in half pays it twice: workers share no
context and each one re-reads the same modules from scratch.

Two structural facts drive this. Claude turns have no Bash, so every fact about
the tree costs one turn, and `--no-session-persistence` means no invocation
benefits from any earlier one.

### What has not been established

Whether a higher ceiling completes these tickets. Whether a cheaper model needs
more turns for the same work, which would raise the failure rate while lowering
the cost of each failure. Whether the Codex adapter behaves differently on the
same graph; it was not installed on the measured host. All four runs used
`claude-opus-5`, inherited from the CLI default, because the run configurations
carried no `adaptive` block; see [ADAPTIVE_ROUTING.md](ADAPTIVE_ROUTING.md) for
the profile, `review_profile`, and budget fields that were not exercised here.

Note also that each accepted ticket costs a second full invocation for
independent review. The costs above are failed implementation turns alone and
understate a completed ticket.

### Status

The delivery and dashboard tickets are being implemented directly rather than
through `anvil run`. Anvil executing its own ticket graph remains untested for
this workload, and these measurements are the reason. Revisit with the routing
controls configured before concluding anything about the ceiling itself.

Saved run directories under the state root retain the full event streams.

## Tickets that must prove a negative

Recorded 2026-09-16 against `main` at `f215d74`, from one ticket and five
supervised attempts.

`apply-credential-exclusion` asked for a configured set of environment
variables to be withheld from every managed subprocess, and for tests proving
the values do not escape. Five attempts spent about $18.76 and merged nothing.

| Attempt | Profile | Worker | Review | Outcome |
| --- | --- | --- | --- | --- |
| 1 | economy | 33 turns, $0.66 | — | hit a turn ceiling the configuration had raised but routing dropped |
| 2 | economy | 70 turns, $2.05 | — | budget exhausted |
| 3 | standard | 102 turns, $3.29 | 48 turns, $1.03 | changes requested: the field was undocumented |
| 4 | demanding | 95 turns, $6.81 | 31 turns, $0.91 | changes requested: `doctor --config` reached an unexcluded probe |
| 5 | standard, criteria enumerated | 118 turns, $4.01 | — | budget exhausted |

### Correction, 2026-09-17

This section originally read: "Attempts 3 and 4 produced complete, correct
wiring. Review rejected both, and each rejection named a launch site the
acceptance criteria had not listed." Both sentences are wrong, and the reading
built on them -- that a reviewer could always find one more site, so the work
could not converge -- is not what the ledgers record.

The saved runs settle it. Attempts 3 and 4 are in run `51fec4cd`; attempts 1, 2
and 5 are runs `5abe55ea`, `3658a8d9` and `55bca6a2`. Both candidate commits are
still in this repository's object store, so each was extracted into a detached
worktree and run against the run's own recorded verification command,
`env PYTHONPATH=src python3 -m unittest discover -s tests`.

**Attempt 3 (`a371221c`) was red and leaking.** The reviewer marked all three
criteria `satisfied: true` and requested changes on one finding: the new
`credential_exclusion` field was undocumented in the README, per AGENTS.md. But
the candidate fails its own two new leakage tests, in `test_execution` and
`test_parallel_execution`, with `worker produced an empty candidate`; and it
never touched `routing.py`, so `preflight` still built its probe environment
with a bare `managed_environment()`. That probe is reached from
`adaptive_runtime.Session.__init__`, a run path and not only `anvil doctor`, so
the agent binary was still launched with the excluded values present. Criterion
3 ("existing serial and parallel execution tests remain green") and criterion 2
("leakage tests prove excluded variables are absent") were both false. The
reviewer, which has Read, Glob and Grep and cannot execute anything, conceded
them from the diff alone.

**Attempt 4 (`e093e320`, integrated as `b51f97c3`) was green.** 441 tests pass.
The reviewer marked criterion 1 `satisfied: false` and named
`src/anvil/cli.py:173`, where `doctor --config` calls `routing.preflight`
without `exclude`. That is the same two-line hunk the human fix `04f270d`
applied, and the maintainer agreed the site was in scope. The rejection was
bound to a listed criterion, at a real location, and it was correct.

So neither rejection named an unlisted launch site. One rejected a red
candidate on the wrong grounds and conceded three criteria it could not check;
the other was right. The acceptance rule held, and nothing wrong merged.

The cost splits differently, too. Attempts 1, 2 and 5 spent $6.72 between them
and produced no candidate at all: those are profile budget and ticket size, not
acceptance. Of the $12.04 in run `51fec4cd`, the only spend the acceptance path
could have avoided is attempt 3's $1.03 review of a candidate the configured
checks would have rejected for free.

Model capability was not the constraint. Attempt 4 ran on Opus and failed the
same way attempt 3 did on Sonnet, at roughly twice the cost -- though note that
escalation raised only the worker profile: both reviews ran on Sonnet. A human
implemented the ticket directly in about half an hour.

### The auditing method

A rejection is auditable after the fact, and this correction exists because
nobody had audited one. The candidate commit is in `details.candidate_sha` on
the attempt, the reviewed revision is the `integration` message's
`integration_sha`, the verification command is `config.verification` in the run
row. Extracting the commit into a detached worktree and running that exact
command distinguishes a reviewer that was right from one that was wrong, which
no amount of reading the review text can do.

Do this before drawing a conclusion from a rejection. The reading corrected
above stood for a day and pointed at a fix that would have merged `a371221c`.

The commits are not kept, which nearly cost this correction its evidence. Anvil
records `candidate_sha` and `integration_sha` in the ledger but holds no ref to
either, so once a run ends a rejected attempt's commits are unreachable and an
ordinary `git gc` destroys them; the three cited here were 25 hours old and
already listed by `git prune --dry-run --expire=now`. They are pinned as
annotated tags so this section stays checkable:

| Tag | Revision |
| --- | --- |
| `evidence/apply-credential-exclusion/attempt-3-candidate` | `a371221c` |
| `evidence/apply-credential-exclusion/attempt-4-candidate` | `e093e320` |
| `evidence/apply-credential-exclusion/attempt-4-reviewed` | `b51f97c3` |

```sh
git worktree add --detach /tmp/replay evidence/apply-credential-exclusion/attempt-3-candidate
cd /tmp/replay && env PYTHONPATH=src python3 -m unittest discover -s tests
```

Pinning by hand was a stopgap. A run now names every candidate and integration
revision it creates under `refs/anvil/<kind>/<run id>/<attempt id>`, so a
rejected attempt survives the run that discarded it and this section's method
works without anyone remembering to pin anything. The refs are evidence and
never an input: no acceptance, rejection, retry or recovery decision reads one.
Bounding their growth is `reap-candidate-refs` in
`tickets/candidate-retention.json` and is not implemented.

### What this does not establish

One ticket is one data point, and the mechanism is a plausible reading of it
rather than a measured law. It does not show that every such ticket fails, that
a different review prompt would not converge, or where the boundary lies
between an open-ended negative and a bounded one.

### What to do with it

Prefer criteria a candidate can finish against. When a ticket's subject is an
absence, name the complete set the absence must hold over, and expect the
enumeration itself to be most of the design work. This advice stands, but note
what it did not do here: the rewritten ticket that enumerated the launch sites
still omitted `cli.py`, and attempt 5 then exhausted a standard budget before
producing any candidate. Enumeration bounds the argument; it does not make the
ticket completable in one invocation.

[CRITERIA.md](CRITERIA.md) turns the advice above into a contract: every
criterion names the role that decides it, and an absence names the set it holds
over.

The mechanism this section now records is different, and it is in the ordering
rather than in the criteria. The configured checks run *after* independent
review, so a reviewer that cannot execute anything is asked to judge candidates
the checks would already have rejected, and its per-criterion map is never
falsified where it could have been. See [ACCEPTANCE.md](ACCEPTANCE.md).
