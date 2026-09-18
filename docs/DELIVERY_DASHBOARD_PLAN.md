# Repository delivery, issue synchronization, and run dashboard

Status: proposed specification; not implemented, and partly superseded.

`anvil serve` shipped after this document was written. It is a read-only local
HTTP server over the ledger with a packaged dashboard page, a versioned read
contract and a resumable event stream; see [the serve contract](SERVE.md).
Slice D5 below proposes building that server, and must not be built twice: its
three dashboard tickets are additive views on the serve contract, and the
`serve` version is the thing an added decomposition has to move. The delivery
and issue-synchronization slices are unaffected. This document describes a new
milestone after PR #12, based on `main` at `4f23a5d`. Commands, configuration
fields, and modules below are design targets. They are not available in the
current CLI. Provider documentation was checked on 2026-09-15.

## 1. Outcome and scope

Anvil should take an explicitly selected set of issues through implementation,
publish the accepted work as a pull request, update those issues as work starts
and becomes ready for review, and mark them complete after confirmed merge.
A local browser dashboard should show the evidence and any delivery problems
throughout that process.

Code hosting and issue tracking are independent choices:

| Code repository / pull requests | Jira Cloud issues | Linear issues | GitHub Issues |
| --- | --- | --- | --- |
| GitHub.com | Supported | Supported | Supported |
| Bitbucket Cloud | Supported | Supported | Supported |

GitHub Issues may live in a different repository from the code. Each target must
be explicitly configured and resolved to its provider identity. Jira and Linear
are issue sources and status destinations, not Git hosts. Existing local JSON
tickets and Markdown preparation remain usable without a tracker.

The complete milestone includes all six combinations. It works with serial or
parallel Codex, Claude Code, and Muse runs because delivery consumes supervisor
evidence rather than agent-specific messages. A run still operates on one local
Git repository, with one aggregate PR containing all of that run's accepted work.
Several tasks may implement one external issue; a task binds to at most one issue
in this release.

Default decisions:

- Support GitHub.com, Bitbucket Cloud, Jira Cloud, and Linear. GitHub Enterprise
  Server, Bitbucket Data Center, and Jira Data Center require later tested adapters.
- Publish only a fully successful run. Partial delivery, stacked PRs, cross-repo
  execution, and one PR per ticket are deferred.
- Create a draft PR first. Promote it only when the delivery checks below pass.
- Observe merges performed through the provider. Anvil does not auto-merge PRs.
- Close issues only on verified merge observation; local acceptance remains a
  separate result. Deployment and release completion are outside this milestone.
- Make remote writes opt-in. Trusted configuration may authorize automatic
  publication and issue updates without asking repeatedly during a run.
- Ship a read-only dashboard on loopback. Run control buttons, remote hosting,
  multi-user access, and a webhook gateway are separate follow-on features.
- Treat epics, projects, milestones, initiatives, sprints, and similar tracker
  constructs as a read-only lens for import and the dashboard. Anvil never
  transitions, closes, or edits a parent construct in this milestone.

Native pause/stop is not a prerequisite. The existing scaffold for that work is
separate from this specification.

## 2. Existing constraints and extension points

`store.py` persists immutable task inputs, attempts, and monotonically numbered
events in `state.sqlite`. `execution.py` and `parallel.py` own Git and acceptance.
A task becomes `done` only after criterion evidence, independent review, passing
checks on the exact integration revision, and managed-branch advancement.
Dependents unlock at that point. Run `success` means all tasks are locally done.
Neither meaning changes for delivery-enabled runs.

`ticket_status.Publisher` already updates local JSON execution metadata from
committed events. Its synchronous `after_commit` hook must not acquire remote
network dependencies. `recovery.py` reconciles the same run and accepted history.
Delivery synchronization must not reset attempts, invoke agents, or advance the
managed branch.

`RunStore.read()` offers a consistent read-only snapshot. The dashboard should
use a bounded query layer over the ledger, not repeatedly read the entire event
history or use the final-only `report.json` as its live data source. Existing
adaptive usage, model/effort decisions, and skill-context events provide most
dashboard evidence already.

## 3. User workflow and proposed commands

1. Configure repository and issue connections, credential references, and workflow
   mappings. Run a read-only connection doctor and review the proposed mutations.
2. Import selected issues into local, reviewable tickets, or attach explicit issue
   bindings to an existing ticket graph. Validate scope and dependencies.
3. Run Anvil normally. A bounded integration coordinator projects committed run
   events into status intents. Started issues update while implementation runs.
4. After run success, publish the accepted branch and draft PR manually or through
   an explicitly enabled `on_success` policy. Ready promotion and issue updates
   follow the configured remote-check policy.
5. Inspect the local dashboard. Reconcile provider state after human review and
   merge. A one-shot sync or explicitly started bounded watcher performs this;
   an idle dashboard is not an implied background automation.

```sh
# All commands in this document are proposed.
anvil integrations doctor --config .anvil/integrations.json --json
anvil issues import --config .anvil/integrations.json --source jira-work \
  --issue ENG-123 --issue ENG-124 --output .anvil/tickets.json
anvil issues import --config .anvil/integrations.json --source linear-work \
  --group PROJECT_ID --output .anvil/tickets.json
anvil delivery plan RUN_DIR --json
anvil delivery publish RUN_DIR
anvil delivery ready RUN_DIR
anvil delivery sync RUN_DIR --json
anvil delivery sync RUN_DIR --watch --interval 60 --timeout 1800
anvil dashboard --state-dir ~/.local/state/anvil --port 0
```

`issues import` supports each tracker, repeated explicit issue identifiers, one
bounded provider query, or one `--group` that expands an epic, project, milestone,
or similar construct to its current members (section 5). `--max-issues` defaults
to 100 and must be explicit to increase it, with a hard bound of 1,000. Overflow fails before output; it must
not silently omit selected issues. Import and doctor perform no remote writes
or model calls. `delivery plan` refreshes read-only observations and lists exact
repositories, refs, SHAs, PR actions, issue targets, and externally visible text.
Plans contain their evidence digest and expiry, never credentials. Publication
revalidates preconditions, even when applying a recent plan.

`delivery ready` explicitly requests promotion for a manual ready policy; it
rechecks the same head, evidence, and required-check gates as automatic promotion.
It cannot bypass them. `publish` records publication authorization durably before
its first remote write so later sync can reconcile an interrupted request.

`delivery sync` retries or reconciles previously authorized operations and reads
PR/issue state; it cannot turn a manual publication policy into automatic
publication. It does not resume execution. Watch mode runs in the foreground,
has a finite timeout, and leaves pending work durable on exit. Suggested defaults
are 60 seconds between batched observations, a 15-second HTTP timeout, and at
most five transient retries per operation per invocation, respecting longer
provider retry hints without exceeding the invocation deadline.

Execution CLI exit codes stay compatible. Proposed delivery exit codes: `0` for
the requested operation being confirmed or nothing due; `1` for pending remote
work at the deadline; `2` for invalid configuration, authorization, or input;
`3` for a conflict requiring a decision. Waiting for human merge is not a failed
sync. A successful local run can still have delivery pending, clearly shown in
its final report and the dashboard.

## 4. Configuration and frozen inputs

Use a separate versioned integration configuration, referenced by an optional
run-level `integrations` object. No fields become mandatory for existing runs.
Add matching runtime validation and JSON Schemas when implementing. Example
connection names below are aliases, not inferred provider accounts.

```json
{
  "version": 1,
  "connections": {
    "code": {
      "provider": "bitbucket-cloud",
      "workspace": "example-team",
      "repository": "product",
      "credential": "env:ANVIL_BITBUCKET_TOKEN"
    },
    "jira-work": {
      "provider": "jira-cloud",
      "site": "https://example.atlassian.net",
      "credential": "helper:jira-oauth"
    },
    "linear-work": {
      "provider": "linear",
      "credential": "env:ANVIL_LINEAR_ACCESS_TOKEN"
    },
    "github-work": {
      "provider": "github-issues",
      "repository": "example-org/product-issues",
      "credential": "env:ANVIL_GITHUB_ISSUES_TOKEN"
    }
  },
  "credential_helpers": {
    "jira-oauth": {
      "argv": ["company-credentials", "access-token", "jira"]
    }
  }
}
```

Each credential reference must resolve a typed record with its authentication
scheme and required identity metadata. A Bitbucket API token also needs its
configured Atlassian email; a bearer access token does not use that scheme.
The example shows connection structure, not a complete authenticated setup.
Doctor must reject missing metadata without attempting a different scheme.

Illustrative run addition, using Jira as its sole writable tracker:

```json
{
  "integrations": {
    "config": "integrations.json",
    "delivery": {
      "connection": "code",
      "remote": "origin",
      "base_branch": "main",
      "publish": "manual",
      "review_ready": "after_checks",
      "required_checks": [{"name": "build", "producer_id": "configured-ci"}],
      "disclosure": "summary_only"
    },
    "issues": {
      "write_connections": ["jira-work"],
      "sync": "during_run",
      "completion": "merged",
      "links": "on_delivery",
      "comments": "off",
      "workflow_mappings": "issue-workflows.json"
    }
  }
}
```

Alternative `publish` value: `on_success`. `review_ready` may be `manual`; either
policy requires an explicit check list. An empty list is allowed only when the user
explicitly configures `required_checks: []`, meaning local gates plus provider
PR availability are sufficient for ready promotion. Anvil's own evidence status
cannot satisfy an external CI requirement. A checker is identified by producer
and context/key where the provider exposes them, not an ambiguous display name.
Issue `links` defaults to `off`; `on_delivery` authorizes an ordinary PR link or
one managed link comment where required. `comments` controls optional progress
summaries separately and defaults to `off`.

Workflow mappings bind Jira transitions, Linear states, or GitHub lifecycle
labels to explicit permitted source states. Require `started`, `ready_for_review`,
and `done`; allow optional blocked/failed/interrupted mappings. Doctor resolves
IDs and reports unavailable transition fields, missing labels, and permissions.
It never creates workflow states or labels implicitly.

Resolve connection identities and freeze effective non-secret configuration,
workflow IDs, issue bindings, credential-reference names, and publication policy
at run creation. Copy the frozen document into run artifacts and bind its digest
to a committed ledger event. Native resume uses this snapshot, not a modified
repository config. Credentials may rotate behind the same reference. Changing
target, workflow policy, or authorization requires an explicit audited rebind,
not editing an old snapshot. First release can refuse rebind and require a new
run instead; making another delivery plan cannot change frozen authorization.
Historical runs without an integration snapshot remain readable in the dashboard
but are not eligible for publication or tracker writes in this milestone.
For delivery-enabled runs, preflight before model work must fetch the configured
target and require the local starting revision to equal that target revision.
This prevents publishing pre-existing local work outside the run's acceptance
scope. Record both identities and revisions; do not change the user's checkout
to satisfy this precondition.

## 5. Issue intake and identity

Introduce an optional immutable ticket `external_issue` object containing a
connection alias, canonical tenant/workspace and issue ID, display identifier,
source URL, import timestamp, source update revision, and content digest. Freeze
the imported title, description, acceptance criteria, and selected relationship
edges separately from mutable execution metadata. Resolve IDs through the
configured connection; never infer identity from a title, branch, or free text.

Initially import one issue as one ticket. Use an explicitly configured acceptance
field or a documented acceptance-criteria section. Missing criteria produce a
review-needed import report and prevent emitting an executable ticket file.
Do not invent criteria or silently send issues through a paid planning turn.
Users may deliberately split an issue into several local tickets afterwards.

Convert Jira ADF and tracker Markdown to plain Markdown while preserving lists,
code, links, and criteria. Record unsupported-node warnings and retain the bounded
source representation locally. No automatic attachment downloads. Provider text
is task data; it cannot grant write permissions or change model/budget settings.

Import only selected dependency edges whose meaning has an explicit mapping.
Do not equate parent/child relations with blocking dependencies. Report dependencies
outside the imported set for resolution; never mark them satisfied just because
their external status says Done. Refuse cycles, duplicate canonical IDs, missing
criteria, invalid references, or output overwrite before atomically writing the
new ticket graph. Manual JSON tickets may use a read-only source binding without
enabling any remote mutation.

For many tasks bound to one issue, freeze the full task membership at run start.
Anvil may close the issue only when the binding declares that this set covers the
whole issue and all members qualify. A partial-issue binding publishes progress
and links only; it cannot change the whole issue's workflow state or phase labels.
A second run cannot automatically take ownership of that issue.

In addition to the original source-content digest, record a normalized scope
digest over imported title, objective, acceptance criteria, and selected dependency
fields. Exclude workflow state, comments, links, and update timestamps. Compare
scope at run preflight, before publication, and before review-ready/completion writes. Human scope
changes produce `source_changed` and suspend those writes; they cannot expand an
in-flight task or cause acceptance against old criteria to close a revised issue.
Keep local acceptance evidence intact and require explicit resolution or a new run.

### Grouping constructs

Trackers organize issues into larger constructs: Jira epics, initiatives, and
sub-task parents through the unified `parent` field, plus sprints, fix versions,
and components; Linear projects, initiatives, cycles, project milestones, and
parent issues; GitHub milestones, sub-issue parents, and issue types. This
milestone supports these constructs as a read-only lens over work. Anvil never
creates, edits, transitions, or closes a grouping construct, and membership
never affects execution. GitHub Projects v2 fields remain deferred.

Record each observed group as an immutable identity plus a mutable observation:
connection alias, canonical provider group ID, provider-native `kind` (never a
synthesized universal taxonomy), display name, source URL, optional parent group
identity, and observation timestamp. An issue holds a set of group memberships,
not a single parent pointer: one Jira story may belong to an epic, a sprint, and
a fix version at once. Groups may nest (Linear initiative to project; Jira
initiative to epic). Record only edges the provider returned; never infer them.
Unknown or unmapped kinds are recorded under their provider name, not dropped.

Membership is captured at import for imported issues and refreshed by ordinary
sync reads. Because Anvil does not own membership, a moved or regrouped issue is
an observation, not a conflict; it does not suspend writes. Scope digests
continue to exclude grouping fields.

Grouping is distinct from dependencies. The prohibition above stands: epic,
project, milestone, and parent membership never produces a blocking edge and
never changes run scope, task ordering, PR boundaries, or issue-binding
membership. It never authorizes transitioning or closing any issue. Reject any
later change that derives execution semantics from group membership.

`issues import --group` expands one group to its current members as a bounded
provider query. The `--max-issues` rule applies unchanged, so an oversized group
fails before output instead of producing a silently partial graph. Nested groups
are not expanded transitively in this release; import a child group explicitly.
Members the query returns but cannot resolve are reported, not skipped.

Rollup is computed locally for display: how many members are imported, accepted,
delivered, merged, pending, or outside this state root. A member never imported
into any run under this state root is unknown, never complete because its
external status says Done. A group index in the state-root integration registry
(section 10) supports this view across runs, since one epic is commonly
delivered over several runs. Parent-construct writes are later work (section 14).

## 6. Three distinct lifecycles

Keep execution, delivery, and tracker synchronization as separately queryable
records. Do not collapse them into a single `done` flag.

| Committed evidence | Execution meaning | Remote delivery / issue effect |
| --- | --- | --- |
| Attempt claimed before launch | Running; worker launch may still be pending | Desired issue `started` |
| Candidate enters Anvil review | Internal review queued/active | Dashboard shows internal review; no external review-ready transition |
| Exact integration revision accepted | Local task `done` | Implemented locally; issue stays started unless an explicit intermediate mapping exists |
| Every task locally accepted | Run `success` | Eligible for aggregate delivery |
| Remote branch confirmed at manifest SHA and draft PR exists | Execution unchanged | Delivery `draft`; publish authorized issue links |
| Expected PR head, required checks passed, provider confirms non-draft | Execution unchanged | Delivery `ready_for_review`; desired issue review state |
| Human/provider reviews PR | Execution unchanged | Display approval/change-request state; no automatic repair or merge |
| Provider confirms tracked PR merged with expected source evidence | Execution unchanged | Delivery `merged`; whole-issue bindings may transition to Done |
| PR closed/declined without merge | Execution unchanged | Delivery `closed_unmerged`; never close issues as completed |
| Remote head changes or identity/ownership conflicts | Execution unchanged | Delivery needs attention; suspend automatic completion |

Base delivery phase: `not_requested`, `pending`, `branch_published`, `draft`,
`ready_for_review`, `merged`, or `closed_unmerged`. Track synchronization health
separately as `current`, `pending`, `reconciling`, `conflict`, or `auth_required`.
Provider review outcome, check outcome, and unknown/stale observations are also
separate fields. Missing data is not success.

Each issue records desired, last observed, and last confirmed state; operation
sequence; owning run/generation; and last observation time. Aggregate membership
means first claim can mark started, but all members must be delivered for review
and merged for completion. Local JSON execution `done` retains its current
meaning even while the external issue is In Review.

Before mutation, re-read the current external state. Already at the intended
target is a converged observation, not proof Anvil caused the change. A human
cancellation, reopen, or disallowed move produces a conflict. Do not auto-reopen
issues, reverse human decisions, or replay historical started events after a run
has already completed. Read-before-write is not atomic on every provider; perform
readback and document the remaining race rather than claiming universal locking.

## 7. Delivery manifest and publication

For a fully successful run, produce an immutable delivery manifest containing:
run/repository identities, expected remote identity, source branch, intended base
branch, original base SHA, final accepted tip, per-task accepted commits and
evidence references, complete issue binding groups, disclosure policy, and digest.
Verify acceptance history against Git using the existing recovery invariants.
Worker claims, dashboard actions, and issue status can never authorize publication.

Acquire the repository lock before publication, confirm no active supervisor,
and verify the managed branch still matches the manifest. Resolve the configured
Git remote and provider repository to the same identity. Fetch the target base.
Normal target advancement from the recorded starting base is permitted. Compare
each observed target with the last confirmed target and retain that observation
chain; this detects observed rewrites without promising detection of every
unobserved intermediate change. Reject rewritten/unrelated target history or a
changed target identity with `base_changed`;
this release requires a fresh delivery-enabled run to resolve that condition.
Do not silently rebase, run models, or transplant unreviewed changes in delivery.

Push only `refs/heads/anvil/<run-id>` to that exact remote ref, using an atomic
create-if-absent precondition (for example, an empty expected-ref Git lease used
solely for creation). A normal push after a separate absence check is insufficient:
it could fast-forward a concurrently created branch. An absent destination can be
created; an existing
destination at the expected tip is an idempotent success. Any unexpected tip is
a conflict, including an ancestor that could be fast-forwarded. No updates or
rewrites of existing remote refs, deletion, broad push, target-branch write, or
automatic merge. Read
back the remote tip after an uncertain push before issuing another write.

Create one draft PR using a persisted delivery ID and exact source/base identity.
Use a neutral title and Anvil-owned body section with concise acceptance/check
evidence. Default disclosure excludes private issue descriptions, raw logs,
prompts, local filesystem paths, provider credentials, and full configuration.
Issue titles and private tracker links require explicit disclosure configuration,
especially when the destination repository is public.

On create timeout, first search for the unique delivery marker across relevant
draft/open/closed/merged PR states. Never adopt an arbitrary PR solely because
its branch name matches. Zero matches permit delayed bounded reconciliation;
multiple matches require attention. Own only the generated body section and
compare its last observed digest before editing. Preserve human-authored content.

Optional Anvil commit-status publication reports its own local verification on
the published SHA under a stable context/key. Display external check runs/build
statuses and human reviews separately. Required check results must belong to the
expected head and configured producer. Pending/unknown results and ambiguous
producer identity cannot pass ready promotion. Stale provider policy data is
shown as unknown, not reconstructed from a simplistic approval count.

Record base observations before publication and before ready promotion. If the
base advances, refresh mergeability and invalidate checks scoped to the old merge
candidate. Source-head checks remain valid only for the same source head. Ready
promotion requires current provider mergeability with no reported conflicts;
unknown mergeability leaves it pending. Local evidence continues to describe the
locally tested tree; do not claim it verifies a new remote merge tree. Default
required checks are scoped to the source head. A stronger merge-candidate policy
must bind evidence to both source and destination revisions and must fail doctor
when a provider/check setup cannot establish that association.

Before closing issues, independently verify canonical repository and PR identity,
configured target branch, provider merged state, expected published source head,
and resulting merge evidence. A merge commit field on an open PR is not proof
of merge. Squash/rebase may remove original commit ancestry: rely on provider
evidence tying the merged PR to the recorded source head and resulting target
commit. If that association cannot be proved, record `merged_unverified` as an
observation, preserve local acceptance, and require reconciliation before closure.
Human pushes to the PR source invalidate the bound delivery evidence. Branch
deletion after a valid merge does not invalidate persisted merge evidence.

## 8. Provider-specific contracts

### Code-host adapters

`CodeHost` exposes repository resolution, capability/permission discovery, PR
lookup/create/update, ready promotion, exact-head observations, check/review
observations, commit-status upsert, and merge reconciliation. Git transport is a
separate boundary. Unsupported capabilities fail doctor explicitly.

- **GitHub:** REST supports draft creation and PR/status reads; use the documented
  GraphQL ready mutation for promotion. Read external check runs and commit
  statuses; initially publish Anvil evidence through commit statuses. Store
  canonical repository and PR IDs as well as display names/numbers. See
  [pull requests](https://docs.github.com/en/rest/pulls/pulls),
  [GraphQL pull requests](https://docs.github.com/en/graphql/reference/pulls), and
  [commit statuses](https://docs.github.com/en/rest/commits/statuses).
- **Bitbucket Cloud:** model native draft and ready flags, PR states, source and
  destination commit hashes, participants/change requests, and commit build
  statuses. Use a stable build-status key and source refname. Contract-test draft
  lookup and update, including recovery when a PR has already closed. See
  [pull requests](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/)
  and [commit statuses](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commit-statuses/).

Do not expose one provider's workflow as a fake universal approval model. Do not
treat an accepted asynchronous provider operation as a confirmed completed effect.

### Issue adapters

`IssueTracker` exposes resolve/search, snapshot read, workflow discovery,
transition planning/application, delivery-link upsert, optional managed summary,
and operation reconciliation. Preserve provider-specific state and transition IDs.

- **Jira Cloud:** use enhanced JQL search with `nextPageToken`, stable site/cloud
  and issue IDs, and ADF conversion. Fetch currently available transitions per
  issue; status IDs are not transition IDs. Required transition fields must be
  supplied by trusted configuration or reported as missing. Upsert PR links with
  a deterministic `globalId`. See [search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/),
  [transitions](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/),
  [ADF](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro), and
  [remote links](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-remote-links/).
- **Linear:** use workspace/issue UUIDs and team-specific workflow state IDs.
  Do not infer In Review from a category shared by several states. Update `stateId`,
  upsert a PR attachment, and inspect GraphQL errors and mutation success even
  with HTTP 200. Follow cursors and use bounded batched queries. See
  [GraphQL](https://linear.app/developers/graphql),
  [workflow states](https://linear.app/docs/configuring-workflows),
  [attachments](https://linear.app/developers/attachments), and
  [pagination](https://linear.app/developers/pagination).
- **GitHub Issues:** use stable repository/issue IDs and opaque global node IDs,
  plus current repository and issue number for addressing. Exclude PR objects
  returned by issue endpoints. Resolve transfers again; writes must stop if the
  new repository falls outside approved scope, even when an old URL redirects.
  Represent started/review-ready with configured, pre-existing Anvil-owned
  labels; native open/closed state remains separate. Close completed issues with
  the appropriate completed reason only after qualified delivery. Preserve other
  labels and all human text; add/remove only owned phase labels instead of
  replacing the complete label set. GitHub Projects fields are deferred. See
  [issues](https://docs.github.com/en/rest/issues/issues) and
  [labels](https://docs.github.com/en/rest/issues/labels), plus
  [global IDs](https://docs.github.com/en/graphql/guides/using-global-node-ids) and
  [issue transfers](https://docs.github.com/en/issues/tracking-your-work-with-issues/administering-issues/transferring-an-issue-to-another-repository).

GitHub Issues may link to a Bitbucket PR using the same ordinary URL mechanism.
Where a provider lacks a dedicated external-link upsert, explicitly enabling
delivery links authorizes one managed link comment; progress commentary remains
off by default. Reconcile comment markers and IDs before any retry.

Avoid generated closing keywords in PR bodies, commit messages, and managed issue
comments. Inspect the delivery's existing commit messages and report detected
automatic-close directives before publication without rewriting accepted commits.
Detected competing automation blocks publication until explicitly acknowledged
and recorded. Anvil's closeout guarantees apply to its own mutations: users and
other integrations can still close issues independently. An external closure
without qualified delivery evidence is `externally_closed_unverified`, not proof
of completed delivery; preserve it without reopening the issue. Setup must
disclose this boundary and any detected competing automation.
See [GitHub linking behavior](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue).

## 9. Authentication and credential isolation

Connections authorize only selected repositories, projects/teams, and operations.
Separate read-only import/inspection access from issue writes, PR writes, and Git
push. Doctor is read-only and reports missing grants; it cannot guarantee future
access. Resolve provider hosts from validated configuration and known cloud API
roots, not issue content. Validate redirects/pagination targets before forwarding
authorization headers. Pin a supported API version where available.

Supported credential boundaries are an explicit environment reference, secure
OS credential storage, or a trusted helper invoked with an argument array and
bounded timeout. Tokens never enter run config, SQLite payloads, Git remote URLs,
command arguments, reports, dashboard data, or agent prompts. Strip configured
integration credential variables from agent and verification subprocess
environments. Current `managed_environment()` preserves non-Git environment
variables, so this requires an explicit implementation change and leakage tests.
Git push receives only its selected transport credentials. A helper's output is
parsed privately and never copied to command artifacts.

GitHub can use repository-scoped personal tokens for a local operator or supplied
GitHub App tokens for organizational automation. Bitbucket uses scoped API tokens
or supported access/OAuth tokens; do not build new onboarding around retired app
passwords. See [GitHub authentication](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api)
and [Bitbucket authentication](https://developer.atlassian.com/cloud/bitbucket/rest/intro/#authentication).

For distributable Jira onboarding, use an approved OAuth integration. Atlassian's
guidance discourages collecting customer API tokens or requiring every customer
to register a separate application. Its documented confidential OAuth refresh
flow needs a client secret: never embed that in the Anvil CLI. The initial adapter
may consume an organization-managed OAuth credential helper; a public turnkey
login requires a separately operated authorization broker and app registration.
Execution and run evidence remain local. Track this as a release prerequisite,
not an assumed built-in service. See [Atlassian guidance](https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/)
and [refresh flow](https://developer.atlassian.com/cloud/oauth/getting-started/refresh-tokens/).

Linear supports PKCE OAuth for native clients; a user-managed key can also be an
explicit local credential source. Serialize refresh by credential identity and
redact both access and refresh material. See [Linear OAuth](https://linear.app/developers/oauth-2-0-authentication).
No model calls are needed to authenticate, import, deliver, synchronize, or render
the dashboard.

## 10. Persistence, ownership, and recovery

Add a versioned `integrations.sqlite` journal per run. The execution ledger remains
owned by the existing execution coordinator. A separate integration coordinator
owns journal writes under an OS advisory lock and never writes execution state.
The dashboard opens both read-only. Active runs may launch one supervised bounded
integration loop; one-shot sync uses the same lock. Shutdown joins that loop, and
remaining intents survive process exit. No detached daemon is implicit.

During execution this loop performs tracker synchronization only; publication is
deferred while the execution coordinator holds the repository lock. It must never
wait on that lock while shutdown waits for the loop. After execution cleanup and
lock release, `anvil run` performs one bounded foreground publication/sync pass
when `on_success` authorizes it. A crash in that gap leaves publication pending
for a later sync. Manual sync during an active run may update authorized tracker
state but reports publication deferred; it cannot compete for Git ownership.

Core events are the durable source. In one journal transaction, project committed
events into external intents and advance the projection cursor. A crash before
projection simply replays events; a crash after projection cannot lose the outbox
intent. Do not implement an unreliable dual write between execution and delivery
databases. Optional failure to synchronize cannot change accepted code into a
failed implementation or contaminate adaptive model-quality learning.

Minimum records:

| Record | Required fields |
| --- | --- |
| Frozen configuration | Version, effective connection identities/policy, reference names, digest bound to execution ledger |
| Projection cursor | Run ID, source ledger identity, last committed event ID |
| Delivery | Manifest digest, repository/base/source identity, accepted/published heads, PR ID/URL, phase, check/review/merge observations |
| Issue binding | Canonical tenant/issue identity, task membership, whole/partial scope, owner run/generation, workflow mapping |
| Outbox operation | Operation ID, semantic dedupe key, event ID, target, ownership generation, expected state/head, payload digest, status, retry deadline |
| Receipt | Provider ID/request correlation, observed effect, timestamp, reconciliation evidence, bounded redacted error |
| Group | Connection alias, canonical group ID, provider-native kind, display name, URL, parent group identity, last observation |
| Group membership | Canonical issue identity, canonical group identity, first and last observation, observing run or sync |

Use `pending`, `in_flight`, `confirmed`, `reconciling`, `conflict`, and
`auth_required` operation states. A crash with an in-flight write enters
reconciliation. Persist intent before network writes; acknowledge only after
readback confirms the effect. Do not promise exactly-once remote APIs. If a create
cannot be uniquely reconciled, stop retrying it automatically. Coalesce superseded
unstarted lifecycle intents while retaining their audit history.

A state-root integration registry binds each canonical external issue to one
active local run generation, preventing another run or repository under the same
Anvil state root from stealing its status sink. Use an issue-operation lock across
ownership verification, mutation, and receipt persistence. Explicit ownership
handoff must wait for in-flight writes and retire old generations. Old pending
events cannot update a new owner's issues. Concurrent ownership from other hosts
or independently configured state roots is not solved by local locks; declare a
single authoritative controller for each issue scope or defer writes on conflict.

Network I/O has deadlines and per-credential throttling. Honor provider retry
hints, use exponential backoff with jitter, and distinguish authentication,
permission, workflow conflict, missing objects, rate limits, and transient errors.
Linear rate limits may be GraphQL `RATELIMITED` errors with HTTP 400; handling only
429 is insufficient. See [Linear limits](https://linear.app/developers/rate-limiting),
[Jira limits](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/),
[GitHub integration practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api),
and [Bitbucket limits](https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/).

Resume preserves delivery IDs, membership, receipts, and ownership generations.
Reconstruct desired state from current accepted history rather than blindly
replaying old started events. Once a complete run is merged, ordinary sync remains
read-only unless a new authorized tracker operation is pending. A human-reopened
issue must not be automatically closed again by an old run.

## 11. Dashboard specification

Ship a small Python HTTP server and packaged HTML/CSS/JavaScript assets with no
cloud service requirement. Bind to `127.0.0.1`, use an available port when `0` is
requested, and print the session URL. Do not depend on a Node runtime for users.
Opening a browser is optional. The server is foreground and exits on Ctrl-C;
stopping it cannot stop workers or delete evidence.

Views:

| View | Content |
| --- | --- |
| Run list | Repository, agent mix, start time, accepted/total tasks, execution state, delivery state, stale observations, sync attention |
| Run overview | Dependency waves/graph, accepted progress, active worker slots, queued work, current review/check stage, elapsed time |
| Ticket detail | Issue link when permitted, acceptance criteria/evidence, attempt history, model/effort selection and reasons, selected skills, local/remote states |
| Delivery | Host, source/base refs and SHAs, PR URL/draft/review/check/merge state, issue synchronization backlog and conflicts |
| Evidence | Independent review findings, verification command outcomes, event timeline, bounded local artifacts, usage/cost provenance |
| Group | Epic/project/milestone/initiative identity and kind, member issues with local, delivery, and tracker states, computed rollup counts, runs that touched members, members outside this state root |

The default overview answers: What is active? What is accepted? What is blocked?
Where is the PR? Why is an issue still open? The group view answers: How far
along is this epic, project, or milestone, and which parts has Anvil never seen?
Keep execution progress and externally delivered progress separate. Mark missing token/cost data as unknown; identify
estimates and incomplete accounting. Never interpret an old heartbeat alone as
proof a worker is alive or dead. Show last evidence time and reconciliation state.

Proposed read API: paginated run summaries, per-run task summaries, ticket attempts,
delivery/issue projections, events after a cursor, and allowlisted artifacts by
opaque ID. Default to 100 rows, maximum 500 per page; bound artifact excerpts to
256 KiB. Use incremental polling about every two seconds for the visible active
run and slower polling for the list. Dashboard polling never triggers provider
requests; it reads observations created by sync. The group view reads the
state-root group index across runs. Label the two database snapshot
cursors/timestamps so temporary projection lag is visible rather than presented
as contradictory authoritative state.

Security and usability requirements:

- Use a per-session local token exchanged for a scoped HTTP-only session; validate
  Host/Origin and serve no permissive CORS. Token bootstrapping must avoid leaking
  through referrers or request logs. LAN/public binding is outside this release.
- GET routes are read-only. No endpoints for run, resume, shell commands, sync,
  merge, or issue mutation. Display useful CLI commands as selectable text only.
- Render untrusted issue text, comments, and logs as escaped text or sanitized
  Markdown with raw HTML disabled. Serve packaged assets only, with a restrictive
  content policy; never execute project JavaScript.
- Resolve artifact IDs through an allowlist; reject traversal, symlinks, unrelated
  files, and oversized reads. Stream sanitized excerpts without loading whole logs.
  Restrict run discovery to the configured local state root and supported layouts.
- No credentials, authorization headers, full environment, or hidden external
  content in responses. Public PR evidence and local operator evidence have
  distinct disclosure rules. Do not upload a local dashboard URL to providers.
- Keyboard navigation, visible focus, accessible state labels, contrast, and a
  table alternative to the dependency graph are acceptance requirements. Support
  offline historical inspection and surface unreadable legacy records per run.

## 12. Delivery slices and implementation order

Each slice should be a separately reviewed PR, with its supported scope stated
in docs. Do not call the whole milestone complete after only the first provider.

| Slice | Deliverable | Exit criterion |
| --- | --- | --- |
| D1: contracts and journal | Versioned config/bindings/manifest, group and membership records, cross-run group index, read models, sidecar projection/outbox, ownership and credential boundaries | Deterministic crash/ownership tests; old runs readable; fake providers only |
| D2: read-only issue intake | GitHub Issues, Jira Cloud, Linear resolve/import, group membership capture and import-by-group, rich text and explicit criteria/dependency mapping, read-only doctor | All three produce valid frozen graphs or actionable no-output failures |
| D3: code-host publication | GitHub and Bitbucket Cloud branch/PR delivery, draft/ready, evidence statuses, observed checks and merges | Both pass uncertain-write/reconciliation tests and publish only manifest-bound work |
| D4: issue lifecycle sync | Started/review-ready/completion mappings for all three trackers, issue groups, links, manual-conflict handling | All six host/tracker combinations pass the same end-to-end contract |
| D5: local dashboard | Delivery, sync and group views added to the shipped `anvil serve` contract, not a second server | Browser verification, bounded queries, isolation and accessibility checks |
| D6: operational acceptance | Read-only plan, bounded watching, packaging, setup docs/entry skill, authorized sandbox-provider exercises | Full regression suite/CI plus recorded external acceptance for each adapter |

Likely modules: `integrations/contracts.py`, `config.py`, `journal.py`,
`coordinator.py`, `credentials.py`, separate `hosts/` and `issues/` adapters,
`delivery.py`, and `dashboard/` read models/server/assets. Keep protocol handling,
publication decisions, and presentation separate. Use Python 3.11+ and standard
library where suitable; any HTTP/OAuth dependency needs an explicit benefit.
Add packaged integration schemas and clearly labeled examples. Extend the Anvil
entry skill once capabilities actually ship, not as part of this spec-only change.

D2 and dashboard read-model work can proceed after D1 independently of code-host
publication. D4 depends on D3's immutable delivery evidence. Auth app registrations,
credential-helper setup, and sandbox provider accounts should be prepared early.
Public Jira OAuth broker ownership is a separate deployment decision; no hosted
infrastructure is implicitly authorized by this plan.

## 13. Acceptance and failure matrix

Ordinary tests use fake HTTP providers and real temporary Git repositories. Use
contract fixtures with documented API versions/capabilities, never live personal
issues. Required behaviors:

1. Every host/tracker pairing imports, marks started, publishes one PR, marks
   review-ready, observes merge, and closes only whole-issue bindings.
2. Agent success alone, missing review, failed checks, branch divergence, an
   interrupted run, and partial group completion never publish or close issues.
3. A candidate queued for Anvil review does not prematurely mark external review
   ready. Delayed or failed CI keeps the PR draft; head changes invalidate evidence.
4. Crash at projection, intent commit, push success, PR create, link/comment create,
   workflow transition, and receipt commit converges or exposes uncertainty without
   silently duplicating PRs/comments or losing accepted work.
5. Two sync processes, an active run plus manual sync, native resume, and cross-run
   issue ownership preserve single-writer rules and reject stale operations.
6. Human PR body edits, external commits, issue moves, cancellation, reopen, changed
   issue scope/workflow, required Jira fields, missing GitHub labels, and wrong
   Linear team state IDs produce precise conflicts; no destructive correction
   occurs. Scope digests ignore Anvil's own status/link/comment updates.
7. Squash/rebase/merge commits, deletion of a merged branch, closed-unmerged PRs,
   unknown mergeability, and partial provider data distinguish confirmed merge
   from insufficient evidence. A synthetic merge SHA cannot pass by itself.
8. GitHub issue-list PR objects are excluded. Imported query overflow, pagination,
   duplicate/moved identities, unsupported rich text, and outside dependencies
   cannot produce a silently incomplete executable graph.
9. Lost responses, 401/403/404/409/429/5xx, Linear GraphQL errors/rate limits,
   revoked credentials, and eventual consistency retain actionable pending state.
   Provider downtime never reruns models or changes local acceptance/learning.
10. Credential references survive rotation while values are absent from agents,
    verification environments, subprocess argv, artifacts, database receipts,
    dashboard JSON, and public PR output. Cross-host redirects cannot receive tokens.
11. Dashboard reads during execution stay consistent and bounded, handle absent
    integrations on legacy runs, expose stale observations honestly, resist stored
    markup injection and arbitrary file access, and remain useful offline.
12. Packaging installs the schemas and UI assets; all existing serial/parallel,
    recovery, routing, status, and skill tests remain green on supported Python
    versions and CI platforms.
13. Import by group respects the issue bound, reports unresolved members, and
    does not expand nested groups implicitly. Membership changes between sync
    reads produce observations only; they never create dependencies, alter run
    scope, or suspend or authorize any write.
14. Group rollup counts derive only from local evidence and confirmed delivery
    observations. A member outside this state root, or one whose external status
    is Done without Anvil delivery evidence, is unknown in the rollup; no group
    is ever shown or recorded as complete on that basis.

Before claiming live acceptance, use explicitly authorized sandbox repositories
and issues to exercise both hosts and all three tracker adapters. The six-way
pairing matrix can run deterministically; record which combinations also ran
live. Seeded accepted evidence can test remote delivery without paid model turns.
Deleting provider fixtures and authorizing trial merges must be explicit test
scope. Provider fixtures validate transport and workflow behavior, not live model
implementation quality. No live provider writes or model calls are authorized
by writing this specification alone.

## 14. Completion definition and later work

This milestone is complete when all adapters, durable synchronization, issue
aggregation, immutable delivery evidence, and the local dashboard are packaged,
documented, and pass the acceptance matrix. A user must be able to identify a
run's accepted local work, its external PR, its issue states, and any reason those
states differ, without manually reading SQLite or guessing from worker messages.

Later work includes enterprise/server variants, GitHub Projects, parent-construct
writes (transitioning epics, closing milestones, updating project status, which
need their own policy because a parent whose members were delivered by mixed
actors is a partial binding under section 5), bulk creation of remote issues
from specs, partial or per-ticket delivery, automatic remediation
from human PR feedback, auto-merge, deployment status, webhooks, multi-host
coordination, and dashboard control actions backed by native lifecycle commands.
