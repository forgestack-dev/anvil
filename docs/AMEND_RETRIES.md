# Amend retries

Status: implemented. Specified against `main` at `373e7c1`, where the mechanism
in section 3 was prototyped against a real repository; that prototype is now
the path a retry takes, and its result is reproduced below. Section 11 records
what was built and the two decisions the implementation had to make that the
specification left open. The measurements this argues from are in
[OBSERVED_LIMITS.md](OBSERVED_LIMITS.md), and the acceptance flow it extends is
[ACCEPTANCE.md](ACCEPTANCE.md). Section 10 still holds: the saving is a
plausible one, not a measured one.

## 1. Outcome

A rejected candidate used to be discarded whole. When review asked for a change
the candidate was two lines from making, the run paid a full implementation
turn to produce those two lines, starting from an empty worktree and
rediscovering everything the rejected attempt already got right.

An amend retry starts the replacement attempt from the rejected candidate's
tree and asks the worker to address the findings, rather than to implement the
ticket again. Everything downstream is unchanged: the result is still one commit
on the accepted base, still independently reviewed, still checked.

## 2. What the measurement says

Run `51fec4cd`, attempt 4: a `demanding` profile spent 95 turns and $6.81 on
`apply-credential-exclusion`, produced a green candidate, and was rejected on
one finding at `src/anvil/cli.py:173`. The human fix `04f270d` applied to that
same file is a two-line hunk. Under today's retry the next attempt would have
begun from nothing.

What that does **not** establish is the size of the saving. Section 1 of
`OBSERVED_LIMITS.md` measured roughly 80% of an invocation going to orientation
rather than implementation, and an amend pays orientation too. The saving is
bounded by the implementation share, not by the whole attempt. It may also be
larger than that bound suggests, because a worker handed the design already made
has less to orient over. Neither is measured, and section 10 says how to settle
it.

## 3. Mechanism

`commit_candidate` refuses a worktree whose `HEAD` is not the base, and requires
the result to be a single commit with the base as its only parent. An amend
therefore cannot check out the rejected candidate. It restores the candidate's
*tree* into a worktree whose `HEAD` stays at the base:

```sh
git worktree add --detach <path> <base>
git -C <path> read-tree -u --reset <candidate>
```

`commit_candidate` then squashes the amended tree into one commit on the base,
exactly as it does for a fresh attempt. Prototyped at `373e7c1` against a
candidate that modified one file, deleted another, and added a third:

| Property | Result |
| --- | --- |
| Worktree `HEAD` after `read-tree` | still the base |
| Working tree | matches the candidate, including its deletion |
| Amended commit's parent | the base, single |
| `git diff base..amended` | `D gone.txt`, `M keep.txt`, `A new.txt` |

No change to `commit_candidate`, `prepare_integration`, or the phase machine was
required. The new code is the worktree preparation,
`Repository.create_amended_worktree`, and the choice of when to use it,
`parallel._amend_source`.

## 4. The constraint that decides correctness

`retry` rebases the replacement attempt onto the coordinator's *current*
accepted base, and that base advances whenever a peer integrates. A rejected
candidate's tree is
base-at-the-time plus its own changes. Restoring that tree onto an advanced base
and committing it would revert every change the peer contributed in between,
silently and with a passing single-parent check.

**An amend is therefore permitted only when the accepted base is unchanged since
the rejected attempt began.** Otherwise the retry falls back to today's fresh
worktree. This is a guard, not an optimisation to relax later: the failure it
prevents is a supervisor discarding accepted work, which no review would catch
because the diff against the new base looks deliberate.

Cherry-picking the candidate onto the advanced base first would widen the case
and introduces conflict handling in a path that currently has none. It is out of
scope here and named in section 10.

## 5. When an amend applies

| Failure | Retry before | Retry now |
| --- | --- | --- |
| `review_rejection` with bound findings | fresh worktree | amend, if the base is unchanged |
| `verification_failure` | fresh worktree | amend, if the base is unchanged |
| `unlisted_requirement` | no retry (stage 3) | unchanged: no retry |
| `undeclared_site` | no retry (stage 4) | unchanged: no retry |
| worker `blocked`, budget or process failure | no retry | unchanged: no retry |

A verification failure is the better case of the two, not a worse one: the
candidate is red against a command whose output the supervisor holds, and the
work to do is bounded by that output rather than by a reviewer's prose.

The attempt cap is unchanged. `store.retry_attempt` permits
`min(max_attempts, 2)` attempts per ticket, so an amend consumes the one retry a
run has; it does not add an attempt.

## 6. Escalation

A fresh-worktree retry escalates to the next-ranked profile, because the
evidence that the attempt failed is read as evidence that the profile was
insufficient. An
amend contradicts that reading: the profile produced a candidate that review
accepted on every criterion but one, and the remaining work is smaller than the
work already done.

An amend keeps the profile that produced the candidate. That is a second
accessor beside `next_profile` — `Session.amend_profile`, which applies the
same attempt cap and returns the current profile rather than the next rank —
not a change to the policy: the escalation allowance is untouched and remains
available if the amend itself is rejected, which is the point at which
capability is genuinely in question.

This is the part of the design most likely to be wrong, and it is cheap to
reverse: escalating an amend costs money rather than correctness.

## 7. Evidence

The amended attempt records, in its `routing_decision` message, that it is an
amendment and which candidate it amended. The ledger already retains that
candidate under `refs/anvil/candidate/<run>/<attempt>`, so the provenance
resolves rather than naming a hash a prune could take.

`telemetry.attempt_record` gains no new failure category. An amend that is
itself rejected is an ordinary `review_rejection`; what changes is that the
routing sample carries the amendment, so a later analysis can ask whether
amended attempts are accepted more often than fresh ones.

## 8. The prompt

The worker prompt for an amend states three things the fresh prompt does not:
the working tree already holds a candidate for this ticket; the findings below
are what an independent review, or the output of the configured checks, asked
to change; and the ticket's criteria are unchanged and still all apply. The last
is load-bearing. A worker told only to address findings will not re-check the
criteria the findings do not mention, and the amended candidate is reviewed
against all of them.

Findings reach the prompt in the bound and located form stage 2 produces, so the
worker is given `criterion 1 (src/anvil/cli.py:173): ...` rather than a
paragraph. A fresh-worktree retry still appends them as free text. Both go
through stdin with the rest of the prompt; no ticket or review text is ever
built into a command string.

## 9. Tests

In `tests/test_adaptive.py`, using fake agents and real temporary repositories:

- An amend retry after a review rejection starts from the rejected candidate's
  tree: the second worker sees the first's files, and the accepted commit
  contains both the original work and the amendment.
  (`test_an_amend_retry_starts_from_the_rejected_candidates_tree`)
- The amended commit has the base as its single parent, and the branch advances
  to it. (`test_the_amended_candidate_is_one_commit_on_the_base`)
- A rejected attempt whose base advanced in the meantime falls back to a fresh
  worktree, and the peer's change survives in the accepted tree. This is the
  test that matters, and it is written so it fails against a version that always
  amends: its second case replaces `_amend_source` with one that ignores the
  base, and asserts the opposite outcome — the peer's file deleted from the
  accepted tree by a commit whose single parent is the new base.
  (`test_an_amend_is_refused_once_a_peer_has_advanced_the_base`)
- An amend keeps the profile rather than escalating, and the escalation
  allowance is still available to the attempt after it.
  (`test_an_amend_keeps_its_profile_and_spends_no_escalation`)
- The amended attempt's `routing_decision` names the candidate it amended, and
  that revision resolves through the retained ref.
  (`test_the_amended_attempt_names_a_candidate_that_resolves`)
- A verification failure amends from the candidate the checks rejected, and an
  amend that is itself rejected stays an ordinary `review_rejection`.
- Existing retry and escalation tests pass unchanged. One shared fixture did
  change: `test_execution.FakeRunner` counted `value.txt` up from whatever its
  worktree held, which in an amended worktree is its own previous attempt. It
  now counts from the base commit it was given, so the fake worker models a
  target set by the ticket rather than by leftover state.

The tree restoration itself is covered where it lives, in
`tests/test_workspaces.py::WorkspaceTests::test_an_amend_worktree_holds_a_candidate_tree_at_the_base`,
which reproduces the section 3 table against a real repository.

## 10. What this does not establish, and what is out of scope

One data point argues for this, and it is the same run every other stage argues
from. Whether amending lowers cost or raises acceptance is unmeasured. The
measurement that would settle it is a live exercise recorded in
`OBSERVED_LIMITS.md`: run a ticket that provokes a bounded rejection twice, once
with each retry shape, and compare turns, cost, and outcome. Until then this is
a plausible saving, not a demonstrated one.

Out of scope, and each its own decision: cherry-picking a candidate onto an
advanced base so an amend survives peer integration; amending more than once;
letting a reviewer request an amend explicitly rather than inferring it from the
failure category; and reusing a candidate across runs, which
`docs/RECOVERY.md` deliberately refuses.

One invariant deserves explicit mention rather than silence. `AGENTS.md` says
old artifacts and worktrees are never adopted and candidates require fresh
review and checks. An amend adopts neither an artifact nor a worktree: it reads
one immutable commit's tree, and the commit it produces is reviewed and checked
from scratch like any other. The sentence governs recovery, where the risk is
adopting evidence; the risk here is adopting *work*, and the guard for it is
section 4.

## 11. What was built

Four pieces, all reached from the coordinator's existing `retry`:

| Piece | Where |
| --- | --- |
| Worktree at the base holding the candidate's tree | `Repository.create_amended_worktree` |
| When an amend is permitted (section 4) | `parallel._amend_source` |
| The kept profile (section 6) | `Session.amend_profile` |
| The prompt (section 8) and the decision evidence (section 7) | `parallel.run_parallel.retry` |

The specification left two things open that the implementation had to settle.

**An amend does not require escalation headroom.** `next_profile` returns
`None` both when the attempt cap is spent and when the profile is already the
top rank, so before this a top-ranked profile got no retry at all. Reusing that
gate for amends would have excluded run `51fec4cd`'s `demanding` profile — the
one case section 2 argues from. `amend_profile` therefore applies only the
attempt cap. A run whose profile has no next rank now gets one amend where it
previously got nothing; a fresh-worktree retry is unchanged and still requires
a rank to escalate to.

**An amend does not survive recovery.** A resumed run never adopts a worktree,
so the replacement attempt is re-dispatched from an empty one with the ordinary
fresh prompt. That is a lost saving, not a lost guarantee: the resumed attempt
is an ordinary attempt, judged the same way. Carrying an amend across a resume
would mean restoring a tree the interrupted run chose, which is the
cross-run candidate reuse `docs/RECOVERY.md` refuses.
