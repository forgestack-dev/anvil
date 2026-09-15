# Automatic runtime skill selection

## Contract

Automatic selection is opt-in. Add this object to a run configuration:

```json
"skill_selection": {"mode": "rules", "max_skills": 2}
```

The rules apply only when a ticket's `skills` array is empty. Any explicit skill
list remains exact and is never augmented or replaced. Selection examines the
ticket title, objective, and acceptance criteria using fixed keyword rules. It
can choose `prototype`, `diagnosing-bugs`, `tdd`, `domain-modeling`,
`codebase-design`, or `writing-for-agents`, then uses `implement` as the general
engineering fallback. `max_skills` is an integer from 1 to 4 and defaults to 2.

Candidates must be present in the healthy repository-scoped AI Hero installation,
match the exact reviewed instruction hash, and be compatible with at least one
worker eligible for that ticket. A mixed pool can therefore select a shell skill
and route the ticket to Codex. A Claude Code or Muse-only ticket skips shell
skills and can select a compatible file-only workflow. If no reviewed, installed,
compatible skill can be selected, the run fails before baseline checks or model
work.

The supervisor freezes every ticket's selected names, selection origin, reasons,
source revision, files, and compatibility data in the run directory. Each attempt
records the decision with its delivered skill context. Resume validates and reuses
that snapshot; later installation updates or rule changes cannot alter the run.

The selector is deterministic and makes no model call. It does not infer new
capabilities, trust unreviewed upstream changes, execute skill scripts, or change
the independent review and verification gates.

## Acceptance tests

- Preserve exact explicit selections while selecting for empty skill arrays.
- Select test workflows for regression coverage and record human-readable reasons.
- Route a shell-dependent automatic selection to Codex in a mixed pool.
- Fall back to compatible file-only implementation guidance for Claude Code.
- Fail before any agent or check when the managed installation is unavailable.
- Reuse the frozen automatic decision through native resume.
- Preserve legacy skill-free behavior when the option is omitted.

