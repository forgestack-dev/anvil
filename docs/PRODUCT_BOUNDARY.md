# Product boundary: open CLI, paid multi-party services

Status: proposed decision record, not ratified. It states a recommended split
between the open-source CLI and paid services, and the architecture that follows
from it. Nothing here is implemented, and no repository, license, or pricing
commitment has been made. Recorded 2026-09-17.

## 1. The rule

Everything required to run Anvil correctly on one machine stays open. Anything
that requires other people -- other machines, other users, or other runs' data --
is a paid service.

The rule is chosen because a user can predict which side a feature lands on
before asking. "Shared" is the literal description of what is sold, so the paid
tier does not read as a hostage situation. A boundary users can derive themselves
is worth more than one that maximizes short-term capture.

## 2. What stays open

Ticket contracts, planning and waves, the execution ledger, serial and worker-pool
execution, Git workspace ownership, all agent adapters, verification, recovery and
`resume`, skill management, local adaptive routing and local policy learning, the
read-only server in `docs/SERVE.md`, and the local dashboard in section 11 of
`docs/DELIVERY_DASHBOARD_PLAN.md`.

The open half must be a complete, defensible tool for one engineer on one machine.
An open half that degrades into lead generation is the failure mode in section 9;
moving `resume`, an adapter, or acceptance behind a paywall would fail this test
immediately.

The local dashboard is explicitly open. It is not competition for a paid UI; it is
the demonstration that sells one.

## 3. What is paid

| Service | Why it cannot be self-hosted trivially |
| --- | --- |
| Pooled routing policy | Requires a corpus larger than one repository produces |
| Cross-machine and team run state | Requires hosted storage and identity |
| Secrets custody, SSO, audit trails | Bought for compliance reasons, not technical ones |
| Hosted execution | Requires operated infrastructure |
| Collaborative UI | Sold as part of the above, never as the product itself |

## 4. The UI is not the moat

The ledger is an open SQLite schema, and `anvil serve` publishes paginated JSON
and an exact-resume event stream over it. That read API was built in roughly one
day. Once it is public, a competing dashboard is a weekend's work for anyone who
wants one.

Charging primarily for a UI would place revenue on the most replicable layer in
the system, and open-sourcing the read API would fund competitors' integration
work. The UI is a surface for paid services, not a paid service.

## 5. Pooled routing policy is the moat

`learning.py` roots routing history at `repo.common_dir / "anvil-routing"`: one
corpus per repository. AGENTS.md requires policies to be immutable,
repository/catalog-bound, and gated on disjoint evidence. Those gates are correct
and strict, which means a single repository may never accumulate enough disjoint
cohorts to promote a policy at all. The capability exists and is data-starved by
construction.

A corpus pooled across an organization removes that ceiling, and a solo
self-hoster cannot reproduce it, because the constraint is data rather than code.
The existing design is already the right shape to sync: policies are immutable,
fingerprinted, catalog-bound, and gated, so a pooled policy is a validated
artifact rather than a live dependency.

This is the one place where the product gets better because other people use it.

## 6. Architecture: two repositories, no plugin system

`anvil` is the open CLI, including the local dashboard. `anvil-cloud` is private
and contains the paid services and the collaborative UI. A third repository for
the UI is premature.

**No memory plugin architecture.** There is exactly one known consumer of such an
API, so its shape would be guesswork. More seriously, AGENTS.md is largely a set
of rules about who may write what, and a plugin boundary near the ledger or the
acceptance path converts "a worker's success message cannot mark a task done"
from an invariant into an assumption about third-party code. The extension point
would sit exactly where the correctness guarantees live.

**The seam already exists and is a sync protocol.** The ledger is append-only and
monotonically numbered, and `docs/SERVE.md` defines bounded reads with an exact
cursor. That is change-data-capture. `anvil-cloud` consumes it by polling events
after a cursor, with no changes to `anvil` and no plugin machinery.

## 7. Shared memory, split by direction

"Shared memory" hides three products: pooled routing policy, cross-run
specification context, and team-visible run history. Only the first has a moat.
Whichever is meant, the two directions are not equally hard:

| Direction | Mechanism | Cost |
| --- | --- | --- |
| Out: events, spend, outcomes | Read the ledger after a cursor | Built; read-only by construction |
| In, advisory: pooled policy, prior outcomes | A run configuration input | Small; `Policy` already loads by fingerprint and freezes for a run, so a fetched policy is a file on disk |
| In, authoritative: anything affecting acceptance | None | Must not exist; AGENTS.md requires evidence to be saved artifacts, not instructions |

The whole cloud integration on the `anvil` side is therefore fetching a JSON
policy before a run and letting a separate process read the ledger. That is a
configuration field and an existing API, not an extension system.

## 8. The coupling risk is the schema, not the repositories

If `anvil-cloud` consumes the ledger, the schema becomes a contract that cannot
change casually, and `PRAGMA user_version` is currently 2. Version the read API
deliberately so that adding an event kind to the open CLI does not break the paid
product. This is the integration problem worth designing before either product
ships; repository layout is not.

## 9. Failure mode to watch

Open core fails when the open half becomes a stub that exists to generate leads.
Anvil's open half must be worth adopting by someone who will never pay. Keeping
supervision, acceptance, recovery, adapters, and local routing open, and selling
only the multi-party layer, satisfies that. Any proposal to move an existing local
capability behind the paywall should be treated as a violation of this record
rather than a pricing adjustment.

## 10. What this record does not decide

Licensing and contributor agreements are in `docs/LICENSING.md` and are more
urgent than anything here. Also undecided: pricing and packaging, whether pooled
policy is org-scoped or cross-customer and what disclosure that requires, the
identity model, the hosted-execution trust boundary, and whether `anvil-cloud`
ever runs a supervisor rather than only observing one.
