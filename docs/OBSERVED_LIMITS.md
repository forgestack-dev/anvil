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

Attempts 3 and 4 produced complete, correct wiring. Review rejected both, and
each rejection named a launch site the acceptance criteria had not listed. The
ticket asked a candidate to prove that nothing leaks while enumerating only
some of the places it could, so a reviewer could always find one more and the
work could not converge. Rewriting the criteria to name every launch site made
the change larger rather than smaller: 39 edits against 25.

Model capability was not the constraint. Attempt 4 ran on Opus and failed the
same way attempt 3 did on Sonnet, at roughly twice the cost. A human
implemented the ticket directly in about half an hour.

### What this does not establish

One ticket is one data point, and the mechanism is a plausible reading of it
rather than a measured law. It does not show that every such ticket fails, that
a different review prompt would not converge, or where the boundary lies
between an open-ended negative and a bounded one.

### What to do with it

Prefer criteria a candidate can finish against. When a ticket's subject is an
absence, name the complete set the absence must hold over, and expect the
enumeration itself to be most of the design work. A criterion of the form
"no X anywhere" leaves a reviewer free to extend the scope after the work is
done, which no amount of model capability or budget resolves.
