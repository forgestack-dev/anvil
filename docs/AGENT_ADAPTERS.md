# Agent adapters

Choose `agent: "codex"`, `agent: "claude-code"`, or `agent: "muse"` for serial implementation and review. A `workers` pool can combine agents; then the top-level `agent` selects the independent reviewer and each worker selects its own implementation agent. All agents use the same ticket contracts, managed Git worktrees, acceptance checks, and persisted state. Existing configurations default to serial Codex. Use `agent_binary` for a custom executable; it is unused for Muse. The legacy `codex_binary` setting applies only to the top-level Codex selection and cannot accompany `agent_binary`.

Pool runs locate every agent entrypoint before dispatch and share process capacity and cancellation across all managed commands. Each worker owns a separate worktree; one integration owner applies candidates to the current accepted branch and reviews/checks the resulting commit. See [worker coordination](PARALLEL_EXECUTION.md) for scheduling, shared resources, and dependency handoffs.

`anvil doctor --agent <agent>` probes the selected CLI's version and required flags without authenticating or requesting a model response. For a custom executable, add `--agent-binary /path/to/executable`. A successful probe establishes local CLI compatibility, not working account access. Anvil uses the CLI's configured account and model; it does not pass a model override unless an explicit adaptive profile is configured, and never passes a permission bypass. Subprocesses do not inherit desktop-only tools or connected apps. With `--config <run.json>`, doctor also runs each adaptive profile's preflight probe, and those probes withhold the run's configured `credential_exclusion` variables exactly as a live turn does; without `--config` there is no run configuration and so no exclusion set to apply.

## Codex

The Codex adapter uses noninteractive `codex exec`, sends the prompt through stdin, and records JSON events and schema-constrained final output. Implementation uses the CLI's `workspace-write` sandbox and independent review uses `read-only`. Existing Codex run behavior is unchanged by agent selection.

## Claude Code

Support targets Claude Code 2.1.260 or later with the flags checked by `doctor`. Each invocation uses print mode, stdin prompts, a JSON schema, and verbose JSON event streaming. A turn is limited to the configured `agent_turns` (32 by default) and the configured `agent_timeout`. Measurements of that ceiling against this repository's own ticket graph are recorded in [CLAUDE_TURN_BUDGET.md](CLAUDE_TURN_BUDGET.md). Claude documents these output and limit options in its [programmatic usage guide](https://code.claude.com/docs/en/headless) and [CLI reference](https://code.claude.com/docs/en/cli-reference).

Anvil locates Claude once before worktree execution. Relative `PATH` entries use the supervisor's starting directory, and all implementation and review turns use that selected absolute entrypoint. Probes and Claude run configurations preserve symlink names so wrappers that dispatch by their invoked name continue to work.

| Role | Available file tools | Responsibility |
|---|---|---|
| Implementation | `Read`, `Glob`, `Grep`, `Edit`, `Write` | Implement the ticket, add tests, and cite evidence from the files. |
| Independent review | `Read`, `Glob`, `Grep` | Review the supervisor-supplied diff and candidate files against every criterion. |

The adapter restricts built-in tools with `--tools`. Implementation uses `acceptEdits` to permit file edits; review uses `dontAsk` to deny operations requiring a prompt. Both disable permission prompts, and neither role has Bash, browser, MCP, or agent-spawning tools. The adapter does not pass `--allowedTools`. Claude distinguishes available tools from permission approvals in its [CLI reference](https://code.claude.com/docs/en/cli-reference) and explains permission modes in its [permission documentation](https://code.claude.com/docs/en/permissions).

The supervisor performs Git operations and executes the trusted verification commands. Claude can write and inspect tests but cannot execute them in this adapter; its prompts require honest evidence about that limit. A change needing a build or generation step during implementation may require a different workflow. The configured checks run after review and a failure stops the serial run; there is no automatic repair attempt.

Anvil uses `--safe-mode` to disable ordinary customization loading while retaining configured authentication and model selection. User/project hooks, plugins, native skills, MCP servers, and automatically loaded `CLAUDE.md` are disabled. The prompt explicitly directs Claude to read applicable repository `AGENTS.md` and `CLAUDE.md` files. Explicitly or automatically selected AI Hero text is pinned and supplied in the prompt by Anvil rather than loaded through Claude's Skill tool. The skill compatibility gate therefore treats Claude workers as file-only and rejects workflows known to require a shell or another unavailable capability. See the [safe-mode contract](https://code.claude.com/docs/en/cli-reference).

Safe mode still honors managed policy, including policy-configured hooks. Authentication helpers and other trusted administrative configuration can run code in the CLI process environment. Claude's file-tool permissions do not provide operating-system containment. These are local runs on trusted repositories and CLI installations; verification also executes directly on the host. See the [managed-hook documentation](https://code.claude.com/docs/en/hooks-guide).

Anvil preserves `schema.json`, `events.jsonl`, and `stderr.log`. It accepts structured output only from exactly one successful result with no permission denials, then writes the normalized `result.json` and applies the shared evidence validation. Claude Code 2.1.260 can emit informational `system/task_summary` events after the result; Anvil accepts these trailers and retains them in the raw event log. The adapter parses the entire stream and rejects other trailing events, duplicate results, and permission denials anywhere in the stream. Missing or malformed output, an error result, a nonzero exit, or a timeout fails the turn even if an earlier message claimed success. Session resumption is not used; saved Anvil state supports inspection only.

Explicit adaptive profiles pass provider-specific model and effort controls per invocation. See [routing](ADAPTIVE_ROUTING.md) for preflight, telemetry provenance, and supported limits.

## Muse

The Muse adapter has no CLI to launch: the implementer or reviewer for a Muse turn is the operator running Anvil (for example, a Muse agent session driving the harness). Each turn stages a handoff in the artifact directory — `prompt.txt`, `schema.json`, and a machine-readable `request.json` describing the worktree, role, and timeout — announces it on stderr, and waits for the operator to write `result.json`. The operator must write that file atomically (temporary file, then rename); the runner reads it once and applies the same strict result validation as the Codex adapter (regular file, 4 MiB limit, unique JSON keys, JSON object). A missing result at the turn timeout, or a malformed one, fails the turn.

`anvil doctor --agent muse` reports operator-handoff availability without probing any executable. The configured `agent_binary` is a label only and is never launched; adaptive execution profiles are unsupported for Muse because model and effort selection belong to the operator's own session. Muse can serve as the serial agent, the pool reviewer, or a worker-pool slot; a Muse slot blocks its pool thread until the operator fulfills the handoff. All acceptance gates — structured evidence, independent review, verification on the exact integration revision, and managed-branch advancement — apply unchanged.
