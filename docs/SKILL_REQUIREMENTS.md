# Skill requirements preflight: bounded milestone

## Contract

Before launching any implementation turn for a ticket-selected skill, Anvil
matches that skill's exact pinned `SKILL.md` hash against a reviewed compatibility
registry. A name match alone is insufficient: changed or newly added instructions
are unclassified and fail closed until their requirements are reviewed.

Each registry entry declares capabilities the upstream workflow requires and any
Anvil-owned adaptations. The first capability vocabulary is `shell`, `network`,
`subagents`, `human_dialogue`, `issue_tracker`, `conversation_history`,
`git_control`, and `review_role`. Adapter guarantees are deliberately narrow:
Codex workers add `shell`; Claude Code and Muse workers guarantee only the common
file context. No adapter guarantees network, nested agents, live human dialogue,
tracker access, conversation history, control of Git, or the independent-review
role. Anvil verification does not make a shell available during a worker turn.

For serial execution, all selected skills must be compatible with the configured
agent. For a pool, an unpinned ticket may dispatch only to compatible workers; a
worker-pinned ticket must be compatible with that worker. A task with no compatible
worker fails preflight before baseline checks or model calls. Independent tickets
are not launched around a known-incompatible task because the run cannot complete.

The pinned run snapshot records the compatibility-registry version. Each delivered
skill-context event records the agent, requirements, satisfied capabilities, and
adaptations. Resume uses and validates the same registry version and skill hashes.
Updates to the installed catalog cannot change an active run.

An explicit ticket selection satisfies an upstream `disable-model-invocation`
policy; it does not satisfy a need for later human answers. Compatibility means the
adapter can attempt the documented workflow, not that the workflow will succeed.
Runtime discoveries can still produce a normal blocked result.

## Acceptance tests

- Reject an unknown instruction hash before any agent/check process starts.
- Route an unpinned `tdd` ticket to Codex when a Claude slot is also available;
  reject it when only Claude or Muse workers exist.
- Allow file-only skills across Codex, Claude, and Muse and record exact preflight
  evidence; preserve implementation-specific adaptations.
- Reject interactive, network, subagent, tracker, conversation, Git-control, and
  review-role workflows with actionable missing-capability details.
- Preserve the compatibility registry and evidence through native resume.
- Keep tickets without selected skills and their scheduling behavior unchanged.

## Deferred

User-configured capability attestations, interactive decision continuation,
tracker and network adapters, nested-agent delegation, and model-assisted
selection/classification remain separate milestones. Deterministic automatic
selection is described in [AUTOMATIC_SKILL_SELECTION.md](AUTOMATIC_SKILL_SELECTION.md). A registry update
requires review and deterministic tests; it never silently trusts new upstream
instructions.
