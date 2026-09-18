# Read-only run server: bounded milestone

## Contract

`anvil serve [--state-dir DIR] [--host HOST] [--port PORT] [--max-streams N]`
exposes saved and in-progress runs as JSON over HTTP. It binds `127.0.0.1` by
default on port 8787, accepts `0` to take an available port, prints the resolved
URL, runs in the foreground, and exits on Ctrl-C. Stopping it cannot stop a worker, advance a
branch, or delete evidence.

Every route is `GET` and strictly read-only. The server never writes Git, the
ledger, or the artifact tree, and never runs an agent or a verification command.
That is the ownership invariant, not caution: only the coordinator writes Git or
SQLite, and a dashboard that can cancel a task is a second writer.

The server is started by the operator as its own process. It is never spawned by
`run` or `resume`; a supervisor that also served HTTP would be unbounded
background work.

## Relationship to the delivery dashboard plan

`docs/DELIVERY_DASHBOARD_PLAN.md` section 11 specifies a larger dashboard as
slice D5, behind delivery manifests, issue synchronization, and group indexes.
This milestone implements the run-evidence subset of that read API that needs
none of those, and deliberately keeps its decisions:

- Bind `127.0.0.1`; LAN and public binding remain outside this release.
- Default 100 rows per page, maximum 500.
- `GET` only. No run, resume, sync, merge, or mutation endpoints.
- Missing token and cost data is unknown; estimates are labeled as estimates.
- Restrict run discovery to the configured state root.

Not in this milestone, and still owned by that plan: delivery, issue, and group
views; artifact excerpts by opaque ID; and the session token exchange required
before any non-loopback binding.

## Two sources, because telemetry is not in the ledger

`queries.py` reads the ledger: runs, tasks, attempts, events. Spend is not there.
`MeasuredRunner` writes each invocation to the artifact tree instead:

    <run_dir>/artifacts/<attempt_id>/{worker,review}/invocation.json

The only existing aggregation is `adaptive_runtime.report()`, which is gated on
`config["adaptive"]` and consumes a full unbounded snapshot. Neither property
suits a polling consumer.

The server therefore reads both sources: the ledger through `queries.py`, and one
`invocation.json` pair per attempt on the current page. This keeps the server
genuinely read-only, works on runs recorded before this milestone, and stays
bounded because pages are bounded. Folding telemetry into the ledger as a new
event kind would make the join cheaper but would only help later runs; revisit it
if attempt counts make the per-page file reads measurable.

Absent telemetry is `unknown`, never zero. Telemetry for a role that never
started is a real `0` with `cost_kind` `not_started`. That distinction lives in
`telemetry.attempt_record`, which `report()` and the server both call, so the
rule has one implementation.

## Routes

Paginated routes accept `after` and `limit` and return the `queries.py` envelope
`{"items": [...], "next_after": ...}`, with `api_version` added by the HTTP layer.
Every JSON body carries `api_version`, including errors, so a consumer never has
to infer which contract it is reading; `docs/CLOUD_SYNC.md` governs how it
changes. The library functions in `queries.py` are unversioned and unchanged: the
wire format is the server's concern, not theirs.

| Path | Source | Cursor |
| --- | --- | --- |
| `/` and `/app.css`, `/app.js` | Packaged assets | — |
| `/health` | — | — |
| `/api/runs` | `queries.list_runs` | run ID |
| `/api/runs/{id}` | `queries.run_summary` | — |
| `/api/runs/{id}/tasks` | `queries.run_tasks` | task position |
| `/api/runs/{id}/attempts` | `queries.run_attempts` | attempt rowid |
| `/api/runs/{id}/events` | `queries.run_events` | event ID |
| `/api/runs/{id}/telemetry` | `telemetry.run_telemetry` | attempt rowid |
| `/api/runs/{id}/stream` | `queries.run_events` | event ID |

`/telemetry` pages over attempts on the same rowid cursor as `/attempts`, so a
consumer joins the two pages by `attempt_id` without a second ordering. Each item
carries `attempt_id`, `task_id`, `status`, `decision`, `failure_category`,
`evaluation`, `invocations`, `cost_usd`, and `duration_seconds`. The page carries
`known_cost_usd`, `cost_complete`, `cost_kind`, and `evaluation_note`: a total
that omits unknown costs, and the labels that stop it reading as an invoice.

Status codes split by origin rather than by message text. The HTTP layer bounds
`limit` and `after` itself, before any query opens a database, and rejects what
it cannot parse as `400` with a `ContractError`. Because parameters are already
bounded, a `StoreError` escaping a query can only mean the ledger is missing,
uninitialized, or unreadable, so it becomes `404`. Anything else is `500` with no
traceback in the body.

`run_telemetry` lives in `telemetry.py` rather than `queries.py`: it is
accounting that happens to read the ledger, and `queries.py` keeps the SQL.
It resolves routing decisions and review starts through
`queries.attempt_messages`, one indexed pass per page.

## Event streaming

`events.id` is `INTEGER PRIMARY KEY AUTOINCREMENT` and `run_events(after=N)`
returns strictly greater IDs in order, so the ledger already provides the cursor
a stream needs. `/stream` emits Server-Sent Events whose `id` is the event ID:

    id: <events.id>
    event: <events.kind>
    data: {"id": ..., "kind": ..., "task_id": ..., "attempt_id": ...,
           "from_status": ..., "to_status": ..., "details": {...}}

A stream opens with an unidentified `meta` event carrying `api_version`, and a
terminal run's stream closes with an unidentified `end` event. Neither carries an
`id`, so neither disturbs resumption. The `end` event is a named event rather
than a comment because `EventSource` cannot observe a comment: a client watching
a finished run would otherwise reconnect to it forever.

`EventSource` resends the last delivered ID as `Last-Event-ID` on reconnect. The
handler prefers that header over the `after` parameter, so resumption is exact
and needs no server-side session state: the ledger is append-only and
monotonically numbered.

Streams are bounded. The handler drains `run_events` until `next_after` is null,
then sleeps about a second. It writes a comment line roughly every fifteen
seconds so idle connections survive intermediaries. It closes once the run status
is terminal and the drain is empty, so a finished run's stream ends rather than
idling. Concurrent streams are capped by `--max-streams` (default 8) and requests
past the cap are refused with `503`.

Polling `/events` remains supported and equivalent. A consumer that cannot use
SSE loses nothing but latency.

## The page

`anvil serve` also serves a single read-only page at `/`, from packaged assets in
`src/anvil/assets`: a run list, per-run dependency waves, per-attempt spend, and
the event timeline, updating live from the stream. It is plain HTML, CSS, and
JavaScript with no build step, no package manager, and no external origin, as
section 11 of the delivery plan requires.

The list is ordered newest first and carries each run's start time, and the page
selects the newest run on load, so the run worth watching is the one already
open. That ordering is applied in the page over the rows it has loaded, not in
the cursor: section 3 of [CLOUD_SYNC.md](CLOUD_SYNC.md) freezes cursor ordering
within an `api_version`, and `created_at` is already on every row. A run whose
ledger could not be read has no start time and sorts last. `run_summary` also
reports `run_dir`, an added field, so a reader can reach the artifacts,
worktrees and raw event streams the ledger only points at.

Assets are served from a fixed allowlist of three filenames rather than by
joining a request path, so there is no traversal surface. They carry a
`Content-Security-Policy` of `default-src 'none'` with `'self'` for script,
style, and fetch, because the page renders ticket text, review findings, and
repository paths from the ledger. Every value reaches the document through
`textContent`; nothing builds markup from ledger content.

The page renders accounting the way the API reports it: `cost_kind` accompanies
every figure, a role that never started shows as a real zero, genuinely absent
accounting shows as `unknown` rather than zero, and a page total states whether
accounting was complete.

## Isolation

A run ID arrives in the path and is resolved against the state root, so it is
validated structurally and then checked for containment: the ID must match
`[A-Za-z0-9_-]{1,128}`, and the resolved directory's parent must be the resolved
state root. Symlinked run directories are refused. Nothing below a run directory
is served; artifacts remain outside this milestone.

The bind address is loopback unless the operator passes `--host`, and a
non-loopback host is refused outright: LAN exposure needs the session token
design in the delivery plan, and this API carries ticket text, repository paths,
review verdicts, and cost. Reaching a server from another device is an SSH tunnel
today. Requests whose `Host` header is not a loopback authority are refused, and
no CORS header is sent, so a page in a browser cannot read the API across
origins.

Reading a ledger recreates its shared-memory index, so the run directory must be
writable even though the server never writes the ledger. That condition is
reported per run as an unreadable ledger rather than a generic failure.

## Tests

`tests/test_serve.py`, using real temporary Git repositories and recorded
ledgers, binding port `0`, and never invoking a live model. It covers cursor
round-trips on every paginated route, `Last-Event-ID` resumption, stream
termination on a terminal run, traversal and symlink refusal, `Host` header
refusal, non-loopback bind refusal, an attempt whose `invocation.json` is missing
reporting unknown rather than zero, and an unreadable ledger that still leaves
its siblings listable.
