# Ticket skill execution: first bounded milestone

## Contract

A nonempty ticket `skills` array explicitly asks Anvil to supply those AI Hero
skills to the implementation turn. The target repository must have a healthy
repository-scoped installation created by `anvil skills install aihero`. Every
requested name must exist in that installation. Anvil validates all managed
copies before starting any agent, then pins the requested files into the run
directory. Updating or removing the repository installation cannot change an
active or resumed run.

Anvil supplies the exact pinned `SKILL.md` and UTF-8 supporting files inside the
worker prompt. This common compatibility path works for Codex, Claude Code, Muse,
and mixed pools without relying on agent-specific slash commands or implicit skill
discovery. Project instructions, the ticket, and existing authorization continue
to take precedence. Skill text may guide execution but cannot broaden scope, grant
permissions, waive verification, commit, publish, or answer a human decision.

The supervisor records the source, revision, selected names, file hashes, and the
attempt receiving each skill context. A recovered run validates and reuses its
pinned snapshot. Fresh attempts receive the same snapshot; candidate reuse still
requires independent review and verification.

This milestone accepts text resources up to a bounded aggregate prompt size.
Binary resources, invalid UTF-8, oversized bundles, missing files, symlinks, local
changes, and hash or executable-mode mismatches fail before model work starts.
Text scripts are supplied as reference material but are never executed by Anvil.
If their required runtime or another tool is unavailable, the worker must return a
blocked result. Reviewers evaluate the ticket and resulting change; they do not
repeat the implementation skill workflow.

## Acceptance tests

- Run requested installed skills through serial Codex, Claude, Muse, and a mixed
  pool using deterministic runners; inspect exact prompt bytes and saved evidence.
- Reject missing installations, unknown skills, modified managed copies, symlinks,
  binary/oversized resources, and agent results that report a missing decision.
- Prove one pinned revision remains stable if the installation changes after run
  creation and through native resume.
- Keep skill-free tickets byte-for-byte compatible with existing prompts and avoid
  network access or skill installation during execution.

## Deferred

Automatic skill selection, model-assisted compatibility classification, binary
asset transport, executable helper invocation, interactive human-decision resume,
Markdown/spec intake, and catalog-wide live evaluation remain later parts of the
full-skill milestone.
