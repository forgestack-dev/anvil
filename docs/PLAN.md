# Anvil — build plan

Status: initial scaffold. JSON validation, dependency planning, and Codex command preparation are implemented. The execution runtime and upstream installation remain planned. See ROADMAP.md for milestones.

## Recommendation

Build `anvil`: one discoverable entry skill backed by a small local executable runtime and a versioned library of AI Hero skills. The skill provides the user interface and engineering guidance. The runtime enforces scheduling, exclusive task ownership, persistence, process limits, and integration.

Start with a single host and one Git repository per run. Use Python with SQLite for a compact runtime, and Codex CLI as the first agent adapter. Keep the adapter boundary explicit so Claude Code or another agent can be added without rewriting scheduling.

One worker and several parallel workers use the same execution model. Dependencies create successive waves of work. Multiple workstreams in the same repository share a coordinator and integration queue, including cross-workstream dependencies.

## What the upstream project already provides

The [AI Hero catalog](https://www.aihero.dev/skills) currently advertises 25 skills spanning setup, planning, implementation, review, research, maintenance, and human-facing work. Its [to-tickets guide](https://www.aihero.dev/skills-to-tickets) explicitly assigns execution of the ticket graph to an outside runner.

There is also an experimental [implement-spec skill](https://github.com/mattpocock/skills/blob/main/skills/in-progress/implement-spec/SKILL.md). It describes a dependency-aware frontier, isolated implementer worktrees, a merger agent, and final review. Use this as an acknowledged design reference. Its location under `in-progress` means it should not become an unversioned runtime dependency.

The [upstream repository](https://github.com/mattpocock/skills) distinguishes user-invoked workflows from model-invoked skills. Preserve that distinction in integration. The harness needs an explicit invocation/compatibility layer; simply telling a supervisor to call every slash command would be insufficient.

The [implement source](https://github.com/mattpocock/skills/blob/main/skills/engineering/implement/SKILL.md) runs review before committing, while [code-review](https://github.com/mattpocock/skills/blob/main/skills/engineering/code-review/SKILL.md) compares committed revisions. The harness must supply a complete, immutable candidate diff so work awaiting a commit cannot disappear from review. Any adapted ordering or invocation behavior must be visible in harness-owned compatibility instructions.

## Deliverables

| Component | Responsibility |
|---|---|
| `skills/anvil/SKILL.md` | Recognize spec/ticket burndown requests; prepare, run, inspect, pause, resume, and stop the harness. |
| Skill references | Detailed worker, review, integration, and human-decision protocols, loaded only when needed. |
| `src/anvil/` | Executable scheduler, persistent state, agent adapter, workspace management, and integration. |
| Skill registry and lockfile | Source revision, content hashes, skill names, supporting resources, invocation restrictions, compatibility notes, and required capabilities. |
| Run configuration | Repository, input source, completion target, verification procedures, worker count, runtime settings, and limits. |
| Per-run state and artifacts | SQLite state, task evidence, messages, agent logs, immutable candidate references, and a burndown report. |
| Behavioral fixtures and tests | Small repositories and fake workers for deterministic failure testing, followed by a live agent exercise. |

This table describes the full target; some components remain planned. A plugin package can be added after the workflow works; it is a distribution option, not a prerequisite for the runtime.

## Using any AI Hero skill

Discover the entire installed catalog rather than hardcoding a short selection. Read descriptions first, then load the selected skill and its required references or assets. Preserve upstream attribution and license notices, pin the source snapshot for each run, and make updates explicit between runs.

Selection uses the ticket's objective, stage, available tools, and explicit user preferences. Record selected skills and resulting artifacts per attempt. A user can explicitly select a skill or let the worker choose relevant ones. New catalog entries become discoverable after an update; their unattended compatibility still needs classification.

Typical routes include:

| Need | Relevant upstream skills |
|---|---|
| Establish project context | `setup-matt-pocock-skills`, `domain-modeling` |
| Clarify scope and decisions | `grill-with-docs`, `wayfinder`, `grilling` |
| Produce executable work | `to-spec`, `to-tickets`; `triage` for incoming issues that need refinement |
| Investigate uncertainty | `research`, `prototype`, `diagnosing-bugs` |
| Implement and improve design | `implement`, `tdd`, `codebase-design`, `improve-codebase-architecture` |
| Review and integrate | `code-review`, `resolving-merge-conflicts` |
| Transfer context or request input | `handoff`, `to-questionnaire`, `wizard` |

This table is illustrative, not an allowlist. Other catalog skills remain available when relevant.

Each registry entry identifies whether it can run automatically, requires an explicit workflow invocation, needs human interaction, or lacks necessary runtime capabilities. Upstream instructions remain intact; harness adaptations live separately and explain their changes. Starting a run can carry previously granted authorization and settled decisions. Workers must not invent human answers or treat a timeout as approval. A new material decision blocks the affected work while independent tickets continue.

## Intake and planning

Accept a specification or a set of ticket files. The first implementation supports Markdown and JSON, including AI Hero's local ticket format. Define a tracker adapter boundary, then add one live tracker based on actual usage rather than implementing every provider initially.

For an agreed spec, use the relevant planning skills to produce independently verifiable slices with dependencies. For ready tickets, preserve their scope and identifiers. Avoid re-triaging tickets already made agent-ready by `to-tickets`.

Normalize each task into an ID, objective, source reference, dependencies, acceptance criteria, verification procedures, and status. Keep a mapping from spec requirements to tickets and acceptance evidence so an omitted requirement cannot be hidden by an empty queue.

Before execution, validate missing dependencies and cycles, inspect project instructions, identify build/test commands and agreed public test seams, record the baseline revision and existing test failures, and resolve decisions necessary to make the selected work executable. Provide a dry-run view of the task graph, available parallelism, selected skills, and run limits.

## The coordinated loops

### 1. Scheduling loop

Find tasks whose prerequisites have successfully integrated, claim them transactionally, and dispatch up to the configured worker limit. Maintain one authoritative supervisor per managed repository/run, including protection against overlapping supervisors.

Every claim has a unique attempt ID, heartbeat expiry, and increasing ownership token. Only the current owner can submit an accepted result. Expiry alone does not authorize reusing a live worker's workspace: replacement attempts get separate workspaces and late submissions are rejected.

### 2. Worker loop

Each attempt runs in its own branch and worktree with the ticket, relevant spec sections, project instructions, prior decisions, dependency artifacts, and selected skills. Use bounded implement/test/review/fix cycles and save results outside conversational memory.

Workers submit evidence and proposed changes; the supervisor owns authoritative task transitions. Restart or refresh context from saved artifacts when needed rather than relying on an indefinitely growing conversation. All worker and reviewer processes count against the global concurrency limit; unrestricted nested spawning is not part of the first version.

### 3. Review and integration loop

Review a known candidate revision against both the ticket/spec and repository standards. Associate review and verification evidence with the exact candidate. Fixes invalidate affected evidence.

A single integration lane combines accepted work with the latest managed branch, resolves or returns conflicts, and runs required checks on the resulting combined candidate. Only then does the coordinator mark the task locally complete and release dependents.

Represent the upstream wide-refactor exception explicitly as an integration group. Intermediate changes may accumulate on the group's branch and enable internal steps, but remain staged rather than done. A final integrate-and-verify task must pass before the group enters the managed branch, its members become done, or external dependents unlock. Detect this need during planning; do not silently waive verification for ordinary tasks.

Journal the intended integration with expected old and new revisions. On restart, reconcile Git and SQLite if the process died between moving a branch and recording completion. Preserve recoverable work until it is successfully integrated.

### Coordination between workers

Provide a durable mailbox for blocker reports, clarification requests, proposed interface changes, artifact availability, and review feedback. Deliver messages at worker step boundaries. Persist important agreements as shared task facts, not only chat messages.

The supervisor validates dependency changes and serializes conflicting shared work. Worktree isolation prevents accidental file overwrites but does not prove semantic compatibility; combined verification remains necessary. Shared fixtures, databases, and service ports need per-attempt isolation or explicit serialization.

For example, a common prerequisite completes first; two independent feature slices then run in parallel; a final acceptance task starts only after both have integrated. Multiple workstreams use this same dependency model.

## State, limits, and completion

Track pending, running, awaiting review, awaiting integration, done, blocked, and failed states, with readiness derived from dependency state. Distinguish a recoverable failed attempt from a failed task or run.

Persist maximum concurrency, attempt timeout, attempts per ticket, run duration, and repeated-failure limits. Track token usage where reported by the adapter; present costs as estimates unless reliable billing data exists. Budget exhaustion pauses or stops with resumable state and never counts as success.

Stop successfully only when every required task has evidence of acceptance and integration, and final checks cover the selected spec. If no work is ready, distinguish workers still running from unresolved dependencies, human decisions, and failures. Do not spin an idle loop or report blocked work as complete. Discoveries outside the agreed scope become proposals rather than silently enlarging the backlog.

The first completion target is a verified local integration branch with a report. With a tracker/PR adapter, explicitly distinguish local completion, ready for review, merged, and ticket closure; synchronize external status only at the configured completion point and within the user's authorized scope.

The runtime is a process, so continued unattended execution requires its host to remain running. Persistence supports recovery after interruption; it does not itself make a process survive a machine shutdown.

## Agent adapter

The first adapter launches the locally installed Codex CLI, captures structured events and result schemas, and records session IDs when useful. The installed CLI exposes noninteractive execution, JSON event output, structured final output, and resume. These are also documented in the [official noninteractive guide](https://learn.chatgpt.com/docs/non-interactive-mode).

The adapter contract covers capability checks, launch, events, structured result, cancellation, and optional resume. Keep sandbox and approval behavior consistent with the selected runtime and existing authorization. The CLI adapter supplies fresh execution contexts; desktop-only tools and connected apps must not be assumed to exist inside subprocesses.

## Build order and acceptance checkpoints

1. **Contracts and skill compatibility.** Finalize the task/result/message schemas, completion semantics, configuration, and upstream lockfile. Inventory the catalog and classify interaction/tool requirements. Deliver a dry-run plan and validated entry skill.
2. **One complete serial slice.** Build local intake, SQLite persistence, the Codex adapter, one worker, candidate review, integration, and reporting. Demonstrate a small spec completing with acceptance evidence before adding concurrency.
3. **Parallel execution and coordination.** Add atomic claims, per-attempt worktrees, dependency scheduling, durable messages, global process limits, and serialized integration. Demonstrate independent slices running concurrently and dependent work waiting correctly.
4. **Interruption and recovery.** Add heartbeat expiry, stale-result rejection, pause/resume/stop, timeouts, retries, rate-limit backoff, and Git/state reconciliation. Verify recovery at each externally visible transition.
5. **Full catalog validation and packaging.** Exercise skill discovery, dependency loading, explicit invocation, human-decision paths, and missing capabilities. Run an independent skill evaluation and a real end-to-end burndown. Package the skill and runtime with concise setup and usage instructions.
6. **Extensions after the core works.** Add the preferred live tracker and another agent adapter. Consider plugin distribution, remote execution, or a visual dashboard when there is a demonstrated need.

## Required behavioral tests

- Serial execution completes a small specification with all acceptance criteria mapped to evidence.
- Parallel workers cannot claim the same task or write the same attempt workspace.
- Dependencies do not unlock before the prerequisite is integrated and verified.
- A wide-refactor group can stage intermediate changes while preventing external dependents from starting until final group verification succeeds.
- Worker success reports cannot override failing verification or missing acceptance evidence.
- Overlapping changes are integrated one at a time, and combined regressions are detected.
- Review includes changes that originally arrived uncommitted; evidence cannot apply to a later, changed candidate.
- A killed worker can be replaced, and its late result cannot be accepted.
- A supervisor crash during execution or integration causes no duplicate completion or lost work on resume.
- Two overlapping supervisors cannot independently control the same managed repository.
- Missing tools or human answers block only affected tasks while independent work continues.
- Dependency cycles, unresolved prerequisites, and exhausted limits produce explicit non-success outcomes.
- A pinned skill snapshot remains unchanged throughout a run even if upstream updates.

Use deterministic fake-agent tests for scheduling and crash handling, real Git fixture repositories for integration, and a bounded live-agent exercise for skill behavior. Validate the skill's structure as well as its observed decisions.

## Decisions left for implementation

The first agent is Codex, with adapters for other agents later. A target project and actual spec/ticket set are needed for the first live trial. A live issue tracker, publishing behavior, and remote execution are optional extensions and do not block building the local core.
