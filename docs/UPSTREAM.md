# AI Hero integration

Upstream repository: https://github.com/mattpocock/skills

Catalog: https://www.aihero.dev/skills

The reference in `upstream/aihero.lock.json` identifies a verified source commit. It is a reference-only record: this scaffold does not download, install, load, or execute upstream skills, and does not claim compatibility testing against the catalog.

## Integration requirements

Discover all skill directories and include their referenced resources. Record per-file content hashes when imports are implemented. Do not automatically update an active run's source snapshot.

Preserve user-invoked versus model-invoked behavior and human decision requirements. A user can provide settled decisions and authorization up front; missing answers must not be fabricated. Keep harness-owned workflow adaptations separate from upstream files.

The experimental `skills/in-progress/implement-spec/SKILL.md` provides the reference pattern for graph scheduling, isolated implementers, and integration. Experimental content can change or disappear, so pin its revision.

The stable implementation and review skills need a compatibility adjustment: review must receive the full candidate, including work that began uncommitted, and a pinned base revision. Review findings do not themselves repair or validate a change.

Honor scoped run permissions around tracker comments, ticket closure, commits, PRs, and human-facing setup workflows. The orchestrator owns completion semantics and must distinguish code integrated locally, ready for review, merged, and externally closed.

The upstream project is MIT licensed. Preserve its copyright and permission notice alongside any imported files. Anvil's license does not replace the upstream license.
