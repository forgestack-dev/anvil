# AI Hero integration

Anvil downloads skills from [mattpocock/skills](https://github.com/mattpocock/skills),
the source of the [AI Hero catalog](https://www.aihero.dev/skills). Installation
is an explicit operation after installing Anvil; Python package installation
does not fetch upstream content or register native skills.

## Install, inspect, and update

```sh
anvil skills install aihero --repo /path/to/project
anvil skills status aihero --repo /path/to/project --json
anvil skills update aihero --repo /path/to/project --dry-run
anvil skills update aihero --repo /path/to/project
```

The manager supports macOS/Linux. Repository scope uses Git to find the working
tree root, including from a subdirectory; it defaults to the current repository.
Use `--global` instead of `--repo` for user-wide installation. Both Codex and
Claude Code are selected by default. Installation can select one agent with
`--agent codex` or `--agent claude-code`.

| Scope | Codex skills | Claude Code skills | Manifest |
|---|---|---|---|
| Repository | `<repo>/.agents/skills` | `<repo>/.claude/skills` | `<repo>/.anvil/aihero.json` |
| Global | `~/.agents/skills` | `$CLAUDE_CONFIG_DIR/skills`, or `~/.claude/skills` when unset or empty | `~/.local/state/anvil/skills/aihero.json` |

The native discovery locations are documented by
[Codex](https://learn.chatgpt.com/docs/build-skills) and
[Claude Code](https://code.claude.com/docs/en/skills). The manager writes separate
copies of each complete skill directory into the selected agent roots.

Claude Code's [`CLAUDE_CONFIG_DIR` override](https://code.claude.com/docs/en/claude-directory)
applies only to global Claude installation. For example:

```sh
export CLAUDE_CONFIG_DIR=/path/to/claude-profile
anvil skills install aihero --global
anvil skills status aihero --global
anvil skills update aihero --global
```

The custom directory may be outside the home directory and is captured when
each command starts. Anvil records it in a version 2 manifest and requires later
commands to select the same directory. Changing or unsetting the variable yields
an error before downloading or replacing skills. Version 1 manifests retain
their original default destinations. There is one global manifest and lock
because the installation also shares Codex's destination; switching profiles
does not create a second owner or migrate files. Codex-only installations and
repository scope ignore this override. Symlinked installation directories,
overlapping agent destinations, and overlap with Anvil metadata are rejected.

Default selection includes `skills/engineering` and `skills/productivity`.
`--include-experimental` also includes `skills/in-progress`. Repeat `--skill NAME`
to choose a named subset, including skills from `skills/misc`; experimental
skills still require explicit opt-in even when selected by name. A subset
installs only those names. It does not automatically install other skills or
tools referenced by their instructions.

Install and update accept `--ref`, defaulting to `main`. A reference is resolved
once to an exact commit SHA, and the downloaded archive uses that SHA. The
manifest records the commit, original agent/skill selection, each file's content
hash, and its executable flag. `--dry-run` fetches and checks the selected source
and previews changes without replacing installed files or writing a manifest.
`status` reads local files and the saved manifest without network access; it does
not check whether upstream has a newer revision. All three commands accept
`--json`.

`update` applies the recorded agent and skill selection to the requested source
revision. It updates all recorded destinations together and does not change
the selection itself. Local modifications, missing managed files, or unmanaged
destination conflicts abort replacement for all agents. An unchanged source and
installation require no replacement. Normal application errors roll back the
operation. Replacement across multiple directories is not atomic against a hard
termination: inspect reported mismatches and preserve any retained staging
backups before deciding how to recover. There is no force, adoption, uninstall,
or in-place agent/selection-change command.

## Preserved content and compatibility

The installer preserves upstream instruction bytes, frontmatter, metadata, and
supporting files inside each selected skill directory. It makes no agent-specific
port or behavioral transformation. It also copies the upstream root license as
`LICENSE.aihero` into every installed skill directory, retaining its copyright
and permission notice. Anvil's own MIT license does not replace that notice.

At the design reference commit
`3cca18b368ae95cdbdebbff572ccafa662551015`, the source contains 25 stable skills,
8 experimental skills, and 4 miscellaneous skills. The stable default selection
covers the stable catalog's cross-skill references at that revision; subsets may
omit referenced skills. [upstream/aihero.lock.json](../upstream/aihero.lock.json)
is the design reference, while each installation's manifest is the authority for
its installed revision. Catalog counts and available names can change upstream.

These commands make skills available to normal Codex and Claude Code sessions
under their native invocation rules. Preserving upstream content does not prove
that every skill's expected tools, permissions, human decisions, or behavior
work in both agents. The installer does not execute upstream scripts or invoke
a model to certify compatibility.

## Remaining harness integration

Nonempty ticket `skills` requests remain rejected by the executor. Anvil's
Claude adapter still uses safe mode with native skill loading and the Skill tool
disabled. Native installation does not alter those worker permissions. Resolving
skills for bounded ticket attempts and recording their use remain planned.

Future harness adaptations must preserve user-invoked versus model-invoked
behavior, human decision requirements, and source snapshots. Keep adaptations
separate from upstream files. The experimental
`skills/in-progress/implement-spec/SKILL.md` remains a reference for graph
scheduling, isolated implementers, and integration.

Review must receive the full candidate, including work that began uncommitted,
and a pinned base revision. Review findings do not repair or validate a change
by themselves. The orchestrator retains its completion rules and scoped
permissions for tracker comments, ticket closure, commits, PRs, and setup. It
must distinguish work integrated locally, ready for review, merged, and
externally closed.
