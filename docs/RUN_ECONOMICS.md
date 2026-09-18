# Run economics and terminal classification

Specified, not implemented. Five slices, measured from this repository's own
runs. Recorded 2026-09-17 from the saved ledgers of the sixteen runs under the
state root dated 2026-09-16.

This milestone is numbered 8 but is not eighth in dependency order. Slices 4 and
5 extend milestones 9 and 10, which are implemented, and slice 4 needs the tree
restoration mechanism milestone 11 prototypes, which is not. Slices 1, 2 and 3
are unblocked and are the place to start.

- Classify an invocation's terminal reason from its own result event rather than from its process exit code.
- Record turn exhaustion and budget exhaustion as failure categories the escalation ladder and local history can read.
- Distinguish a billed cost from a subscription list estimate, and gate `max_budget_usd` on the former.
- Retain a turn-exhausted attempt's workspace rather than restarting it from the base revision.
- Aggregate the per-invocation usage a run already records into its report and the read-only dashboard.

Recorded 2026-09-17 from the saved ledgers of the sixteen runs under the state
root dated 2026-09-16: 33 invocations, 76,556,941 input-side tokens and 964,097
output tokens. [OBSERVED_LIMITS.md](OBSERVED_LIMITS.md) records the same runs
from the turn-budget side and this milestone is the accounting one; the two
measurements agree on the mechanism and neither is a fix.

Two relationships hold to within 4% across every measured invocation: cache
creation tracks the invocation's final context size, and cache reads track the
sum of its per-turn context sizes. Context size therefore appears in both
input-side terms and turn count multiplies the second, which is the same
orientation cost `OBSERVED_LIMITS.md` measures in turns, priced.

| Component | Tokens | Share of estimate |
| --- | --- | --- |
| Cache creation (1h) | 3,194,093 | 36.2% |
| Cache reads | 73,361,100 | 35.4% |
| Output, 54% of it thinking | 964,097 | 28.4% |
| Uncached input | 1,748 | 0.0% |

| Phase | Terminal reason | Invocations | Turns | Share of estimate |
| --- | --- | --- | --- | --- |
| worker | completed | 14 | 577 | 44.8% |
| worker | `max_turns` | 6 | 198 | 29.1% |
| review | completed | 11 | 289 | 17.7% |
| worker | `budget_exhausted` | 2 | 188 | 8.4% |

**Slice 1 — terminal classification.** `adapters/claude.py` raises on
`outcome.returncode` before `_extract_result` runs, so a turn-exhausted
invocation reaches the coordinator as `Claude Code execution exited with code 1`
and its `subtype` and `terminal_reason` are discarded with the stream. Every
failed attempt in the measured runs carries that one string and a null failure
category; the terminal reasons tabulated in `OBSERVED_LIMITS.md` were read out of
the raw event files by hand, because the ledger does not hold them. Parse the
result first, classify, then raise with the classification attached. The Codex
adapter's own terminal vocabulary needs the same treatment.

**Slice 2 — failure categories.** `store.py` admits only `review_rejection` and
`verification_failure`, so nothing from slice 1 can be recorded even once it
exists. Turn and budget exhaustion are not rejections: they are an invocation
that never produced a candidate to reject, and they say something about the
ceiling rather than about the candidate. Add them as their own categories and
keep them out of the disjoint evidence a policy gate reads.

**Slice 3 — cost basis.** Every measured invocation ran with
`apiKeySource: "none"` and `modelUsage[...].costBasis: "list"`: a Max
subscription, and a list-price estimate of what the tokens would have cost.
`telemetry.py` records that estimate as `provider_reported_estimate` without
distinguishing it from a billed call, and `routing.py` lets `max_budget_usd`
terminate an invocation against it. Two invocations ended that way — runs
`55bca6a2` at 118 turns and `3658a8d9` at 70 turns — discarding 188 turns of work
to enforce a ceiling on money that was never charged. `AGENTS.md` already states
that cost estimates are not invoices or hard budget caps; the implementation
contradicts the invariant. Split the recorded cost kind by basis and enforce a
dollar ceiling only where the basis is billed. This is also the seam any later
API-key support keys off, so it precedes that work rather than following it.

**Slice 4 — retaining an exhausted attempt.** `parallel.py`'s retry builds a
fresh worktree from the base and clears the attempt's claims and candidate, and
today even that does not run for this case: a `ProcessError` is caught in
`execution.py` and stops the whole run, so the eight exhausted invocations above
ended sixteen runs between them. Milestone 11 specifies restarting from a
rejected candidate's tree, and the mechanism it prototypes — restoring a tree
into a worktree left at the base — is the one this slice needs. The trigger is
what differs: an amend addresses bound findings on a candidate a reviewer
returned, while an exhausted attempt has no candidate and no findings, only a
tree and a ceiling it did not finish under. Decide what such an attempt is worth
before spending more on it — a raised ceiling on the same workspace, or the
partial tree offered to the configured checks, which under milestone 9 now run
before review and can reject it for free. Note that milestone 11 records the
saving as unmeasured for the same reason it is uncertain here: orientation is
roughly 80% of an invocation, and a resumed attempt may pay it again.

**Slice 5 — surfacing usage.** `telemetry.py` already parses cache creation,
cache read, output and thinking tokens per invocation and nothing aggregates
them: every number in this section came from re-reading saved event streams.
Milestone 10 ships the read-only server and its dashboard, so this slice extends
a contract that exists rather than proposing one — see [the serve
contract](SERVE.md), whose read version is the thing an added decomposition has
to move. Neither that contract nor `report.json` carries the component split or
the terminal reason today.

### What this does not establish

That reducing any component completes a ticket. The shares order the components
under one subscription's list prices; they are not spend, and a billed run would
reprice them. Slice 4 does not establish that a raised ceiling converges — the
turn-budget section of `OBSERVED_LIMITS.md` records that better navigation
changed how the budget was spent rather than whether it sufficed, and 118 turns
in run `55bca6a2` produced no candidate either. The decomposition is the Claude
adapter's; Codex reports incremental per-turn usage and was not installed on the
measured host. One day of one repository's own graph is the sample.

### Considered and set aside

Replacing the agent CLIs with a smaller harness or a direct provider loop, to cut
the fixed instruction and tool-schema context each invocation carries. Measured
here that floor is 443,239 tokens across 33 invocations, 13.9% of cache creation
and roughly 4.5% of the estimate, because these invocations run long enough to
amortize it. Under a subscription the exchange is worse than neutral: it converts
covered consumption into billed spend, and the two published systems surveyed for
this — a meta-harness over the same CLIs, and a single-shot review pipeline over
provider APIs — set no cache breakpoints at all, against an uncached input share
here of 0.0%. Revisit only alongside billed execution, and measure on this
repository's own graph rather than on published benchmarks.
