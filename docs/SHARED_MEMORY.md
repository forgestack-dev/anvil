# Shared memory: consent before storage

Status: proposed specification, not implemented. It defines who may contribute to
and consume a shared memory corpus, at what scope, and what revocation means.
Nothing here is implemented, and no storage engine has been selected. Recorded
2026-09-17. The product split it serves is `docs/PRODUCT_BOUNDARY.md`; the
transport it rides on is `docs/CLOUD_SYNC.md`.

Shared memory is a paid service. It lives in `anvil-cloud`. This record is in the
open repository for the same reason `docs/CLOUD_SYNC.md` is: a contract stating
what may leave a user's machine is worthless if it lives only in the product that
benefits from the answer.

## 1. Consent is the feature, not storage

Surveyed memory engines -- Graphiti, Mem0, Letta, Cognee -- are storage and
retrieval layers. None models who was permitted to contribute a fact, who is
permitted to read it, or what happens when permission is withdrawn. Graphiti is
explicit that facts are invalidated and not deleted, and documents no access
control. That gap is this feature; the storage question is downstream of it and
comparatively easy.

Section 7 of `docs/PRODUCT_BOUNDARY.md` records that "shared memory" hides three
products. They carry different risk and cannot share one switch:

| Tier | What it is | What it exposes | Default |
| --- | --- | --- | --- |
| 0 Aggregate | Pooled routing policy weights | Nothing identifying | Opt-out |
| 1 Outcome | Model and effort tied to an accepted or rejected result | Pseudonymous | Opt-out, tenant-scoped |
| 2 Content | Conventions, failure patterns, review findings | Quotes the source | **Opt-in, always** |

Section 5 of `docs/CLOUD_SYNC.md` already states why tier 2 is different:
verification output and review findings are the most sensitive rows because they
quote the code under test.

## 2. Two axes, not a chain

A project is a set of tickets -- an epic or milestone -- and may span
repositories. It is therefore not a child of a repository, and the scopes do not
form a single containment chain.

| Axis | Levels | Answers |
| --- | --- | --- |
| Resource | tenant, repository, branch | What is the memory *about*? |
| Subject | tenant, user; tenant, project, ticket, attempt | Whose work *produced* it? |

## 3. The rule

**A fact may be contributed only if every scope it touches, on both axes,
permits it. Consumption is evaluated identically.** The effective permission is
the intersection; a scope may narrow what its parent allows and may never widen
it.

One evaluation covers the whole lattice, fails safe, and is auditable as a single
recorded decision. A run executing ticket T of project P, as user U, on branch B
of repository R, under tenant N, contributes and receives only what all six
permit.

Opt-in and opt-out are **two switches, not one**. *Contribute* and *consume* have
different defaults and different failure modes: a tenant may mandate contribution
for compliance while a user wants to read without writing. Section 5 of
`docs/CLOUD_SYNC.md` currently specifies only the contribute switch.

## 4. Mobility follows the tier

**A fact is bound to the narrowest resource scope its evidence touched.** This
is what makes a project spanning repositories safe.

- **Tier 2 is repository-bound.** Content-derived memory never crosses a
  repository boundary, whatever project the ticket belonged to. Crossing requires
  a tenant-granted ceiling *and* a project election naming both repositories, and
  is out of scope for a first release. Deferring it changes no schema.
- **Tiers 0 and 1 are tenant-bound** and travel across repositories freely,
  because they are not derived from source content.

A project is therefore a consent grouping, not a delivery mechanism. Within a
run, ticket-to-ticket context already exists as saved evidence; shared memory
must not become a second and weaker path for the same thing.

## 5. What is not a scope

**A worktree is provenance, never a consent scope.** It is created per attempt,
destroyed after, and AGENTS.md requires that old worktrees are never adopted on
recovery. Partitioning consent by worktree would forbid deriving across attempts
of one ticket, which destroys the most valuable memory the system produces: an
accepted and a rejected result on near-identical input, the asset section 10 of
`docs/PRODUCT_BOUNDARY.md` identifies. It would also fragment the corpus on every
`resume`.

The worktree is recorded on each fact so revocation can trace it to the exact
turn that produced it, and nothing more.

**No memory propagates between worktrees during a run.** Live cross-worker
sharing is model-to-model chat under another name, which AGENTS.md prohibits when
it requires dependency handoffs to be saved evidence. Section 1 of
`docs/CLOUD_SYNC.md` already prevents it by making the inbound direction a file
frozen at startup; this record states it as an intended property rather than
leaving it emergent.

## 6. Acceptance is the promotion gate

Memory is written at branch scope and promoted to repository scope only when the
managed branch advances.

Memory learned on a branch that never advanced is memory from work that was not
accepted. Promotion therefore reuses the gate the supervisor already enforces,
and an abandoned branch's memory expires with it. Nothing promotes into project
scope: the axes do not meet, and project-scoped memory is written at project
scope from the start.

## 7. Revocation means erasure

Withdrawing consent removes prior contributions and everything derived from them.
Invalidating a fact in place is supersession, not erasure, and does not survive an
offboarding or an erasure request.

Three requirements follow:

- **Every fact carries full provenance**: tenant, user, repository, branch,
  project, ticket, attempt, source agent, and the originating `(run_id,
  event_id)`. The ledger is append-only and monotonically numbered and the
  projection is already idempotent on that pair, so the chain exists for free.
- **Derivation never crosses a consent boundary.** Derived artifacts are computed
  within one scope and unioned at retrieval. This keeps enforcement a partition
  filter rather than a per-row entitlement check, and bounds the blast radius of
  an erasure to the scope it occurred in.
- **Derived artifacts are retired and re-minted, never mutated.** A routing
  policy is already immutable, fingerprinted, catalog-bound, and gated on
  disjoint evidence. Erasure removes the contributor's rows and mints a
  successor. A successor that fails the gate leaves the tenant on its configured
  default, which is the behavior section 4 of `docs/CLOUD_SYNC.md` already
  specifies for a refused policy. A run that froze the retired policy keeps it,
  as recovery already requires.

Erasure reaches the ledger projection too. A projection holding raw rows is
itself contributed data, not a cache.

## 8. Identity

Anvil has no user identity and must not acquire one. Managed commits carry a
constant `Anvil <anvil@localhost>` identity, deliberately, so that a user's Git
identity is not required; the ledger has no user column.

**The subject of the sync credential identifies the user.** One run has one
supervisor, on one machine, with one operator, so attribution is per run and
never per commit. Enforcement is split accordingly:

| Concern | Where enforced | Why |
| --- | --- | --- |
| Content exclusion | The open CLI, at `anvil serve` | Section 5 of `docs/CLOUD_SYNC.md` means nothing unless the machine withholds the rows |
| Identity attribution | `anvil-cloud`, at ingest | The open CLI has no identity model and must not grow one |

An ingest that cannot resolve a credential subject to a roster entry fails
closed. A run whose configuration withholds rows never transmits them at all.

## 9. Tenancy is a partition, not a toggle

No memory of any tier crosses a tenant boundary. Tenancy is the storage boundary,
enforced beneath the application, and is not a permission anyone can grant.

This settles the disclosure question left open in section 11 of
`docs/PRODUCT_BOUNDARY.md` and narrows the moat claim in its section 5 honestly:
pooled memory beats a single repository's corpus because one organization has
many repositories, not because many organizations share one corpus. The value
therefore scales with tenant size, and a small tenant may still not clear the
disjoint-evidence gates. Pooled policy is an enterprise capability; small tenants
need a different reason to pay.

Memory crosses **agent** boundaries freely within a tenant. Facts learned in a
Codex run may reach a Claude run. The source agent is recorded on every fact
regardless, so a tenant that later arrives with a vendor restriction is a
configuration change rather than a re-architecture.

## 10. Contribution and consumption are reciprocal

A tenant consumes a tier only if it permits contribution at that tier. Payment
alone does not entitle consumption, and contribution alone does not substitute
for payment; both are required.

Five consequences keep the rule from doing damage it was not intended to do:

- **It is evaluated per tier.** Contributing outcome data entitles a tenant to
  pooled routing policy. It does not entitle it to content-derived memory, which
  is earned only by contributing content-derived memory.
- **It binds in one direction.** Consumption implies contribution; contribution
  does not imply consumption. The two switches of section 3 survive, so a tenant
  that mandates contribution for compliance while restricting what its own runs
  receive remains expressible.
- **It binds at the tenant and nowhere below.** An individual's withdrawal never
  counts against the tenant's entitlement. A privacy control that costs a
  colleague something is not one anyone exercises, and reciprocity evaluated per
  user would turn opting out into an act of disloyalty. Permission is measured
  over the scopes the tenant itself controls; a user's opt-out is not one of
  them, and a tenant cannot reach the same result by excluding every project it
  administers.
- **It measures posture, not volume.** The question is whether a tenant permits
  contribution, never how much has arrived. Policing quantity rewards generating
  junk, cannot be defined fairly across tenant sizes, and is redundant, because
  the disjoint-evidence gates already refuse to promote a policy that nothing
  supports. This is also the answer to cold start: a tenant that has contributed
  nothing yet consumes from its first run.
- **Erasure ends entitlement forward, not backward.** A tenant that erases its
  corpus under section 7 stops consuming, and the successors minted without its
  rows are the repair. Completed runs are not retroactively disentitled, because
  a finished run cannot be un-informed.

## 11. Calibration is the small-tenant read

The corpus supports two reads with very different appetites for the consuming
tenant's own data.

| Read | What it produces | The tenant's own data required |
| --- | --- | --- |
| Policy | A routing policy fitted for this tenant | Enough to clear the disjoint-evidence gates |
| Calibration | This tenant's position against a distribution | Enough to place a point |

Only the first is data-starved at small scale. Section 5 of
`docs/PRODUCT_BOUNDARY.md` records that a single repository may never accumulate
enough disjoint cohorts to promote a policy at all, and a tenant holding a handful
of repositories may not either. Calibration has no such floor: which ticket shapes
cost a multiple of the tenant's median, which repositories reject most often,
where effort was spent without reaching acceptance.

**Calibration is a report, not a run input.** It reaches a person through the paid
interface and never enters the inbound direction of section 1 of
`docs/CLOUD_SYNC.md`. Nothing in it is frozen for a run and it cannot influence an
acceptance decision, so it needs none of the guarantees that direction carries.
It is consumed under section 10 at the tier it reads, so a tenant withholding
outcome contribution does not receive outcome calibration.

**The comparison is intra-tenant.** Section 9 permits no memory of any tier to
cross a tenant boundary, and a statistic drawn across tenants discloses their data
however it is aggregated. Comparing a tenant against a cohort of other tenants
would be an exception to section 9 rather than an application of it, and
section 14 records it as undecided.

## 12. What the open CLI gains

A consent block in the run configuration and a ticket-level field in
`schemas/tickets.schema.json`, plus the withholding rule in section 8. Schema and
runtime validation change together, as AGENTS.md requires.

Nothing else. No identity model, no memory client, no plugin boundary. Section 6
of `docs/PRODUCT_BOUNDARY.md` rejects a memory plugin architecture because an
extension point near the ledger or the acceptance path converts an invariant into
an assumption about third-party code. Consumption remains a file fetched before a
run and frozen for it; the only change is that the file may carry more than a
routing policy.

### 12.1 Two decisions the slices rest on

**Contribution consent is a read-time filter, not a frozen run input.** Reading
out is a pull by an external consumer, so the supervisor transmits nothing: the
ledger records the run in full, as the machine's own source of truth, and
`anvil serve` decides what may leave. Evaluating at read time is what lets a
withdrawal reach rows already written, which section 7 requires and which
freezing would defeat. The consume axis is unchanged and keeps the existing
mechanism, a file fetched before the run and frozen for it exactly as a routing
policy is.

The two evaluations combine the way section 3 combines scopes. A row leaves only
if the declaration its run recorded and the configuration in force at read time
both permit it, so a later withdrawal reaches rows already written and a later
broadening cannot expose what a run declined.

**Withholding adds a field and does not move `api_version`.** A withheld row is
served with its content omitted and a marker naming the scope that excluded it,
rather than dropped from the stream. Section 3 of `docs/CLOUD_SYNC.md` permits
adding a field within a version and forbids removing one, so this is additive. It
is also the better design: a gap in a monotonic identifier sequence is
indistinguishable from data loss, and a consumer cannot otherwise tell "nothing
happened" from "you may not see this". It follows the precedent in section 5 of
`docs/CLOUD_SYNC.md`, where excluded credential names are recorded and their
values never are.

### 12.2 Slices

**Slice 1 -- the consent block.** A run configuration carries a contribute and a
consume decision per tier. Absent, a run contributes nothing: section 1 makes
tier 2 opt-in, and a configuration that has never heard of shared memory must not
begin contributing because it was upgraded.

**Slice 2 -- per-ticket exclusion.** A ticket field withholds that ticket's rows.
It narrows and never widens, so a ticket cannot contribute what its run declined.

**Slice 3 -- the recorded declaration.** A new event kind records the effective
consent at run start, added as a kind rather than by mutating past rows. Recovery
retains it, as it already retains the ledger and immutable inputs. Without it the
read API knows only the current configuration and cannot honor what a run
declared at the time it ran.

**Slice 4 -- withholding at the read API.** `anvil serve` omits content for any
row either evaluation excludes and serves the marker in its place. Loopback-only
and GET-only are unchanged.

| Slice | Changed together | Tests |
| --- | --- | --- |
| 1 Consent block | `schemas/run.schema.json`, `config.py` | `tests/test_run_config.py` |
| 2 Ticket exclusion | `schemas/tickets.schema.json`, `contracts.py` | `tests/test_planning.py` |
| 3 Recorded declaration | `store.py` | `tests/test_store.py` |
| 4 Withholding | `serve.py`, `queries.py` | `tests/test_serve.py`, `tests/test_queries.py` |

Slices 1 and 2 change a schema and its runtime validation together, as AGENTS.md
requires. Slice 4 is the one that carries the guarantee: until it exists, a
declaration is a statement of intent that nothing enforces.

## 13. Storage consequences

The inbound direction is a pre-run frozen file, so the retrieval budget is the
time a run takes to start. Sub-100ms retrieval, streaming synchronization, and
live multi-agent shared state are requirements of a different product shape and
are not requirements here.

What the model does demand is a partition primitive strong enough to survive an
application bug, complete provenance, and hard deletion. Row-level security over
ordinary relational tables satisfies the first; the resource and subject axes are
columns, not a graph. The ledger's shape -- repository, branch, ticket, attempt --
is a tree that recursive queries traverse. A graph engine is not indicated by
anything in this record, and a bi-temporal store that invalidates rather than
deletes is disqualified by section 7.

## 14. What this does not decide

The storage engine. Retention periods and their interaction with erasure. The
extraction pipeline: what is worth remembering from a ledger, and which of it is
tier 1 rather than tier 2. Cross-repository tier-2 mobility, deferred by
section 4. Whether cross-tenant cohort calibration is worth an exception to
section 9, and if so under what minimum cohort size, cell suppression, and
restriction on who may define a cohort; without those a comparison against other
tenants is a deanonymization channel rather than a statistic. Hosted execution,
which moves the credential subject from an operator's machine to an API caller
and is a different trust boundary, as section 6 of `docs/CLOUD_SYNC.md` already
notes.
