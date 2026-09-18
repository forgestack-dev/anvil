# Reaching a run from another device

Status: proposed specification; not implemented. Based on `main` at `eb63795`.
It answers the one question `docs/SERVE.md` defers in a sentence — what it would
take to bind the read-only server beyond loopback — and the mobile clients that
question was originally asked about. Nothing here is implemented, and the server
refuses a non-loopback bind today.

## 1. What is already settled, and by whom

`anvil serve` binds `127.0.0.1`, refuses a non-loopback `--host` outright, and
refuses a request whose `Host` header is not a loopback authority. Section 11 of
[DELIVERY_DASHBOARD_PLAN.md](DELIVERY_DASHBOARD_PLAN.md) states the requirement
that gates any change: *a per-session local token exchanged for a scoped
HTTP-only session; validate Host/Origin and serve no permissive CORS; token
bootstrapping must avoid leaking through referrers or request logs.* It also
puts LAN and public binding outside that release.

So the deferral is deliberate and this document does not reopen it. It specifies
what the eventual change has to satisfy, so the decision can be made once rather
than argued each time someone wants the dashboard on a phone.

## 2. What the port carries

A ledger is not neutral telemetry. The read API serves ticket text, acceptance
criteria, repository paths, branch and revision names, review findings,
verification command output, and cost. `docs/CLOUD_SYNC.md` section 5 already
says as much about a service consuming it; the same is true of a browser.

That is why loopback is the default rather than a placeholder. The question is
not "how do we bind `0.0.0.0`" but "what authenticates a reader, and what does a
stolen URL get them".

## 3. Today's answer, which is not a workaround

An SSH tunnel:

```sh
ssh -N -L 8787:127.0.0.1:8787 your-dev-box
```

The server stays loopback-bound, the operating system's own authentication
carries the session, and nothing about Anvil changes. For one engineer watching
their own runs from a second device this is the whole requirement, and it should
stay documented as the supported path even after section 4 ships.

## 4. What binding beyond loopback would have to add

Each item is a requirement, not a design:

- **A token the operator can revoke, minted per serve invocation.** Printed once
  on the terminal that started the server, never written to a file the run tree
  keeps, and exchanged for a scoped session cookie rather than sent on every
  request. A long-lived bearer token in a URL is the failure the dashboard plan
  names: it leaks through referrers, shell history, and request logs.
- **`Host` and `Origin` validation against the bound authority**, replacing the
  loopback-only check the handler has now, and still no CORS header.
- **A bind address the operator states explicitly**, with the token required
  before the socket opens rather than checked per request, so a server cannot be
  started reachable and unauthenticated even briefly.
- **Transport the operator chose.** Plain HTTP over a LAN exposes everything in
  section 2 to the network. Either the server terminates TLS with a certificate
  the operator supplies, or the documentation says plainly that this is for a
  trusted network and an SSH tunnel is the alternative. Generating a
  self-signed certificate and training operators to click through a warning is
  worse than either.
- **Read-only is not the safety argument.** Every route being a `GET` limits
  what an attacker can change, not what they can read, and reading is the whole
  exposure here.

## 5. Mobile clients

The original question that produced this work was whether the macOS, iOS, and
Android clients of another project could be reused. The finding stands and is
worth keeping: that project has one web UI and three thin shells — an Electron
wrapper and WebView hosts in Swift and Kotlin — that load a server URL. The
shells are generic; the UI is bound to that project's API.

So a mobile client is not a UI problem. It is section 4 plus a packaging
exercise, and it is pointless before section 4: a WebView pointed at a
loopback-only server on another machine has nothing to show. If section 4 ever
ships, the cheapest path is a shell that takes a server URL and a token, and the
existing page in `src/anvil/assets/` renders unchanged, because it is plain
HTML, CSS, and JavaScript with no build step.

Reusing another project's shells means copying Apache-2.0 source into an MIT
repository, which `docs/LICENSING.md` decision 3 governs. That decision comes
first.

## 6. What this does not establish

Nobody has asked for LAN access in anger. The one request behind this document
was satisfied by an SSH tunnel, and building section 4 on one hypothetical is
how a read-only local tool grows an authentication surface it did not need.

The trigger worth waiting for is a second person needing to watch a run they did
not start — a team, not a second device. That is also the point at which
`docs/PRODUCT_BOUNDARY.md` says the work stops being local: anything requiring
other people is the paid tier, and a hosted service authenticates differently
than a token printed on a terminal. Section 4 may therefore never be built in
this form, and should not be started until it is clear which side of that
boundary the requirement falls on.
