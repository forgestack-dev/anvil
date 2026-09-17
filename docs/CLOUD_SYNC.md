# Cloud sync contract

Status: proposed specification, not implemented. It defines how a separate
service would consume a run ledger and supply a validated routing policy, so the
open CLI and any paid service can evolve independently. No such service exists,
and nothing here changes how a run is executed or accepted. Recorded 2026-09-17.
The product split it serves is `docs/PRODUCT_BOUNDARY.md`.

## 1. Two directions, deliberately asymmetric

| Direction | What moves | Mechanism |
| --- | --- | --- |
| Out | Run, task, attempt, event, and accounting rows | A consumer reads the ledger after a cursor |
| In, advisory | One validated routing policy | A file named by run configuration |
| In, authoritative | Nothing | Prohibited |

Reading out is a pull by an external consumer, so the supervisor has no knowledge
of it, no dependency on it, and no failure mode from it. Writing in is a file on
disk that a run freezes at startup. Neither direction requires an extension point
inside `anvil`, and neither may affect acceptance.

The prohibition on authoritative input is not a simplification. AGENTS.md
requires dependency handoffs to be saved evidence rather than instructions or
live model-to-model chat, and requires acceptance to rest on criterion evidence,
independent review, and checks tied to the integrated revision. A remote service
that could influence any of those would move the acceptance decision off the
machine that verified the work.

## 2. Reading out

The consumer polls the read API in `docs/SERVE.md`: run summaries, then task,
attempt, event, and telemetry pages, then events after a cursor. `events.id` is
monotonic and append-only, so the cursor is the whole protocol.

Rules:

- **The ledger is the source of truth.** The remote copy is a projection. A
  conflict is resolved by re-reading, never by writing back.
- **At least once, idempotent by `(run_id, event_id)`.** A consumer that crashes
  mid-page re-reads from its last durable cursor and must tolerate duplicates.
- **Unknown event kinds and unknown fields are retained and ignored.** A consumer
  that rejects what it does not recognize turns every additive change in the open
  CLI into an outage in the paid product.
- **Terminal runs stop changing.** Once a run reaches a terminal status and the
  cursor is exhausted, the projection is final and the consumer stops polling.
- **Accounting is read per attempt, not per event.** Telemetry lives in the
  artifact tree rather than the ledger, so a consumer either reads the telemetry
  route or reads nothing; it must never infer cost from event counts.
- **Unknown cost stays unknown.** `cost_kind` travels with every figure, and
  page totals report known cost separately from completeness. A projection that
  sums nulls as zero misrepresents spend as certainly-cheaper.

A run directory that becomes unreadable is reported as unreadable for that run
and must not stall the consumer's other runs.

## 3. Versioning the read contract

This is the coupling risk in section 8 of `docs/PRODUCT_BOUNDARY.md`, and the
only part of this document that constrains the open CLI.

The ledger's `PRAGMA user_version` is a storage detail and is not the contract.
The contract is the JSON the read API returns, which carries its own integer
`api_version`, reported by `/health` and by every page envelope.

Within one `api_version`:

- Adding an event kind, a field, or a route is permitted.
- Removing or renaming a field, changing a field's type, changing the meaning of
  a status, and changing cursor ordering are not.
- Cursor semantics are frozen: `after` is exclusive, ordering is stable, and a
  page's `next_after` is always a valid cursor for the next call.

Incrementing `api_version` requires serving the previous version concurrently for
one release, so a paid product never breaks because the open CLI shipped.

A consumer records the `api_version` it read alongside the cursor, and refuses to
continue a projection across an unannounced version change rather than silently
reinterpreting old rows.

## 4. Supplying a policy

A pooled policy is an ordinary validated policy artifact: immutable,
fingerprinted, catalog-bound, and gated on disjoint evidence, exactly as
`routing.py` and `learning.py` already require of a local one. A service produces
the file; `anvil` consumes it through existing configuration and freezes it for
the run.

Consequences that keep this safe:

- Fetching happens **before** a run, never during one. A run that has started has
  a frozen policy and makes no network call.
- A policy that fails existing validation is refused, and the run proceeds under
  its configured default. A remote service cannot widen what Anvil will accept.
- The service being unreachable, slow, or wrong degrades routing quality only. It
  can never block a run, change an acceptance decision, or alter recovery.
- Recovery keeps the frozen policy, as it already does. A resumed run does not
  re-fetch.

## 5. What must not leave the machine without consent

A ledger is not neutral telemetry. It contains ticket text, acceptance criteria,
repository paths, branch and revision names, review findings, verification
command output, and cost. Some of that is commercially sensitive and some is
simply the user's source material.

Any service consuming a ledger states what it transmits and retains, and sending
anything is opt-in per repository. Credential **values** are already never
recorded, only excluded names, and that must remain true of every projection.
Verification output and review findings are the most sensitive rows, because they
quote the code under test.

## 6. What this does not specify

Identity, authentication, tenancy, retention, and the wire protocol between a
consumer and its own backend are the service's concern and deliberately absent
here. Also unspecified: whether a service ever runs a supervisor rather than only
observing one, which is a different trust boundary and would need its own record.
