# Agent adapters

Choose `agent: "codex"` or `agent: "claude-code"` for serial implementation and review. A `workers` pool can combine both agents; then the top-level `agent` selects the independent reviewer and each worker selects its own implementation agent. Both use the same ticket contracts, managed Git worktrees, acceptance checks, and persisted state. Existing configurations default to serial Codex. Use `agent_binary` for a custom executable; the legacy `codex_binary` setting applies only to the top-level Codex selection and cannot accompany `agent_binary`.

Pool runs locate every agent entrypoint before dispatch and share process capacity and cancellation across all managed commands. Each worker owns a separate worktree; one integration owner applies candidates to the current accepted branch and reviews/checks the resulting commit. See [worker coordination](PARALLEL_EXECUTION.md) for scheduling, shared resources, and dependency handoffs.

`anvil doctor --agent <agent>` probes the selected CLI's version and required flags without authenticating or requesting a model response. For a custom executable, add `--agent-binary /path/to/executable`. A successful probe establishes local CLI compatibility, not working account access. Anvil uses the CLI's configured account and model; it does not pass a model override or permission bypass. Subprocesses do not inherit desktop-only tools or connected apps.

## Codex

The Codex adapter uses noninteractive `codex exec`, sends the prompt through stdin, and records JSON events and schema-constrained final output. Implementation uses the CLI's `workspace-write` sandbox and independent review uses `read-only`. Existing Codex run behavior is unchanged by agent selection.

## Claude Code

Support targets Claude Code 2.1.260 or later with the flags checked by `doctor`. Each invocation uses print mode, stdin prompts, a JSON schema, and verbose JSON event streaming. A turn is limited to 32 agentic turns and the configured `agent_timeout`. Claude documents these output and limit options in its [programmatic usage guide](https://code.claude.com/docs/en/headless) and [CLI reference](https://code.claude.com/docs/en/cli-reference).

Anvil locates Claude once before worktree execution. Relative `PATH` entries use the supervisor's starting directory, and all implementation and review turns use that selected absolute entrypoint. Probes and Claude run configurations preserve symlink names so wrappers that dispatch by their invoked name continue to work.

| Role | Available file tools | Responsibility |
|---|---|---|
| Implementation | `Read`, `Glob`, `Grep`, `Edit`, `Write` | Implement the ticket, add tests, and cite evidence from the files. |
| Independent review | `Read`, `Glob`, `Grep` | Review the supervisor-supplied diff and candidate files against every criterion. |

The adapter restricts built-in tools with `--tools`. Implementation uses `acceptEdits` to permit file edits; review uses `dontAsk` to deny operations requiring a prompt. Both disable permission prompts, and neither role has Bash, browser, MCP, or agent-spawning tools. The adapter does not pass `--allowedTools`. Claude distinguishes available tools from permission approvals in its [CLI reference](https://code.claude.com/docs/en/cli-reference) and explains permission modes in its [permission documentation](https://code.claude.com/docs/en/permissions).

The supervisor performs Git operations and executes the trusted verification commands. Claude can write and inspect tests but cannot execute them in this adapter; its prompts require honest evidence about that limit. A change needing a build or generation step during implementation may require a different workflow. The configured checks run after review and a failure stops the serial run; there is no automatic repair attempt.

Anvil uses `--safe-mode` to disable ordinary customization loading while retaining configured authentication and model selection. User/project hooks, plugins, skills, MCP servers, and automatically loaded `CLAUDE.md` are disabled. The prompt explicitly directs Claude to read applicable repository `AGENTS.md` and `CLAUDE.md` files. This does not enable AI Hero skill loading. See the [safe-mode contract](https://code.claude.com/docs/en/cli-reference).

Safe mode still honors managed policy, including policy-configured hooks. Authentication helpers and other trusted administrative configuration can run code in the CLI process environment. Claude's file-tool permissions do not provide operating-system containment. These are local runs on trusted repositories and CLI installations; verification also executes directly on the host. See the [managed-hook documentation](https://code.claude.com/docs/en/hooks-guide).

Anvil preserves `schema.json`, `events.jsonl`, and `stderr.log`. It accepts structured output only from a successful final result with no permission denials, then writes the normalized `result.json` and applies the shared evidence validation. Missing or malformed output, an error result, a nonzero exit, or a timeout fails the turn even if an earlier message claimed success. Session resumption is not used; saved Anvil state supports inspection only.
