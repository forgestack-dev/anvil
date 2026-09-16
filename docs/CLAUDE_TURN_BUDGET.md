# Observed Claude adapter turn budget

Recorded 2026-09-16 against `main` at `293ad94`. These are measurements from
four real invocations, not a specification. `MAX_TURNS = 32` in
`src/anvil/adapters/claude.py` is unchanged; nothing here has been fixed.

## What happened

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

## What the numbers say

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

## What has not been established

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

## Status

The delivery and dashboard tickets are being implemented directly rather than
through `anvil run`. Anvil executing its own ticket graph remains untested for
this workload, and these measurements are the reason. Revisit with the routing
controls configured before concluding anything about the ceiling itself.

Saved run directories under the state root retain the full event streams.
