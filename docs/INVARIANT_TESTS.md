# Hardening AGENTS.md invariants into tests

Status: slices 1, 2, 3 and 5 implemented; slice 6 is half implemented and half
withdrawn; slices 4 and 7 are proposed and not implemented. The analysis below is based on `main` at `17ebf0e`, and its line
counts and violation list are that revision's. Module paths, test names, and
constants for the unimplemented slices remain design targets. The extractor in
section 4.1 was prototyped against `17ebf0e` and its output is reproduced here.
Nothing in sections 4.3 or 6 runs yet, and 5.4 is withdrawn.

## 1. Outcome and scope

`AGENTS.md` states the invariants this repository must preserve. Most of them
are enforced by the test suite. Some are enforced only by a reader's attention,
and those are the ones that fail silently: the code grows a new case, the prose
still reads correctly, and no test turns red. Run `51fec4cd` is the recorded
cost of exactly that shape.

This milestone converts the enforceable subset of `AGENTS.md` into failing
tests, and marks the remainder as unenforced so a reader can tell the two apart.
It adds no runtime dependency, no new command, and no change to execution
behavior, with one deliberate exception: section 4.3 adds a thread-ownership
assertion to the ledger and the repository, because an invariant about who may
write is worth more as a runtime guard than as a test.

Out of scope: retrofitting tests for claims already covered, reorganizing the
existing suite, and any attempt to check design intent that has no predicate.

## 2. Classification of the claims

Every normative sentence in `AGENTS.md` falls into one of three classes. The
class determines whether a test is possible and what kind.

**Class A, behavioral.** A statement about what a run does. Checks run before
the review; a rejection names a criterion and a location that resolves; recovery
retains the ledger, branch, and completed evidence; skill installation copies
the root license. These are covered. `tests/test_execution.py`,
`tests/test_adaptive.py`, `tests/test_recovery.py`, and
`tests/test_skill_management.py` carry them, and no work is proposed here.

**Class B, structural closure.** A statement of the form *every X does Y*, where
the set of X grows as the code grows. A behavioral test covers the members that
existed when it was written. Nothing fails when a new member appears. The
credential exclusion invariant is the canonical instance, and `AGENTS.md` names
the maintenance burden directly: "Enumerate the launch sites when adding one."
That sentence is an instruction to a human, not a failure condition. Section 4
converts the three Class B claims into failure conditions.

**Class C, cross-artifact consistency.** A statement that two artifacts must
agree: schema with runtime validation, README and entry skill with the CLI. No
test reads both artifacts today, and the CLI claim is already violated. Section
5 covers these.

A fourth group is not a class at all. Sentences such as "dependency handoffs are
saved evidence, not instructions or live model-to-model chat" and "cost
estimates are not invoices or hard budget caps" express design intent with no
predicate to evaluate. Section 10 records why they stay prose.

## 3. Current coverage and the gap

The suite is larger than the source it covers: 9,668 lines of tests against
7,656 lines in `src/anvil/`. The gap is not volume, it is shape.

`tests/test_environment.py` defines `CredentialExclusionReachesEverySubprocess`
with one test per known launch site: runner factories, verification commands,
preflight probes, Git descendants, recorded artifacts, and the frozen
configuration. Those tests are correct and should remain. They are also a
hand-maintained enumeration of a set that the code can extend without them.
Adding an eleventh launch site that inherits the host environment breaks the
invariant and passes the suite.

Three further claims have no enforcement of any kind. `src/anvil/` is
stdlib-only today, verified by AST walk over every module, but the first
third-party import would be caught only in review. `store.py` and
`workspaces.py` contain no thread guard, so "only the coordinator thread writes
Git or SQLite" rests entirely on discipline. And no test reads
`schemas/run.schema.json`; the runtime never loads it, so the schema and
`config.py` can disagree indefinitely.

## 4. Structural closure

### 4.1 Launch-site registry — implemented

A static check over `src/anvil/` finds every call to `run_process` and to
`subprocess.Popen`, `subprocess.run`, `subprocess.call`,
`subprocess.check_call`, and `subprocess.check_output`. For each it records the
module, the enclosing qualified name, the callee, and the unparsed expression
passed as `env`. The test asserts two things: no site omits `env`, and the
discovered set equals a registry declared in the test.

Keying on the enclosing function rather than the line number keeps ordinary
edits from churning the registry. Two sites may share a key, so the registry
maps each key to a count and an exclusion source rather than to a source alone.

Implemented as `tests/test_environment.py::LaunchSiteRegistry`, the module
section 7 designates, beside the behavioral tests for the same invariant and
beside its complement in 4.2. The extractor run
against `17ebf0e` found eleven sites, and the same eleven were present when the
test landed, so the table below is still the registry's content:

| Module | Enclosing | Callee | `env` expression |
| --- | --- | --- | --- |
| `adapters/claude.py` | `ClaudeRunner.run` | `run_process` | `environment` |
| `adapters/claude.py` | `doctor.run_probe` | `run_process` | `managed_environment(exclude)` |
| `adapters/codex.py` | `CodexRunner.run` | `run_process` | `managed_environment(self.exclude)` |
| `adapters/codex.py` | `doctor` | `subprocess.run` | `environment` (two sites) |
| `execution.py` | `verify` | `run_process` | `managed_environment(config.credential_exclusion)` |
| `processes.py` | `run_process` | `subprocess.Popen` | `env` |
| `routing.py` | `preflight` | `run_process` | `managed_environment(exclude)` (two sites) |
| `skill_management.py` | `scope_for` | `run_process` | `managed_environment()` |
| `workspaces.py` | `Repository.git` | `run_process` | `environment` |

Each registry entry names one of three legal exclusion sources: the run
configuration's `credential_exclusion`, an adapter's `exclude` attribute, or the
documented absence of a run configuration. Only `skill_management.scope_for`
takes the third, and it already says so at the site. The `processes.run_process`
entry is the sink rather than a launch site; the registry marks it as such so
its bare `env` forwarding is not mistaken for an unexamined default.

The failure message must print the new site, the three legal sources, and the
`AGENTS.md` sentence it violates. A structural test whose failure does not say
what to do gets deleted within a month. It does all three, verified by adding an
undeclared `subprocess.run` and reading what a developer would see.

Three tests carry the section. One asserts no site leaves its environment to
inheritance, one asserts the discovered set equals the registry, and one asserts
every registered entry names a legal source, with `processes.run_process` the
only entry allowed to name the sink. Mutation-tested three ways: an undeclared
site that inherits the environment fails the first two, and a second launch
inside an already-registered function fails on the count rather than passing
because its key was already present.

Note that the two `subprocess.run` probes in `adapters/codex.py` never pass
through `run_process`. A check written only against `run_process` would miss
them, which is why the extractor covers both paths.

### 4.2 Canary interception — implemented

The registry proves that every written site was reviewed. It does not prove that
what runs is clean, because the expression passed as `env` can be reviewed and
still be wrong. A second test closes that half.

Set a canary variable in `os.environ`, name it in `credential_exclusion`, and
drive one full fake-agent run with `subprocess.Popen` wrapped for the duration.
Assert the canary appears in no intercepted environment mapping. This covers
whatever the run actually launches, however each site is spelled, and it
naturally extends to the Git commands the supervisor issues as part of the same
run.

Neither test subsumes the other. The registry catches an unexercised site that
no test drives; the canary catches an exercised site whose expression is
reviewed and wrong. Both are required.

Implemented as `tests/test_environment.py::CanaryReachesNoLaunchedProcess`, and
the claim above is now demonstrated rather than argued: replacing
`Repository.git`'s environment with `dict(os.environ)` leaves the registry green
and fails the canary. One run intercepts about 150 launches.

Two things the implementation had to get right. A launch is attributed to its
*nearest* caller, skipping stdlib subprocess frames, because Anvil drives the
fake agent and so appears somewhere on every stack; without that, the fake's own
Git calls counted as the run's and the test failed on the harness. And the
failure reports counts only. The first version printed the offending
environments, which put every value on the host into the failure log -- the
thing the invariant exists to prevent.

### 4.3 Thread ownership

"Only the coordinator thread writes Git or SQLite" is the one Class B claim
worth enforcing in the code rather than in a test. `RunStore` captures
`threading.get_ident()` when the run is initialized and asserts it inside
`_transaction(write=True)`. `Repository` does the same on its write path.
Violations then fail during a real run, not only under CI.

Two paths legitimately cross threads and must be designed for rather than
excepted. `store.after_commit` is assigned the publisher's flush in
`parallel.py` and runs inside the coordinator's commit; recovery re-enters an
existing ledger from a different thread than the one that created it. Give the
guard a narrow, explicit `adopt()` that transfers ownership and records the
transfer, rather than a flag that disables the check. An escape hatch will be
used the first time the guard is inconvenient, and the invariant returns to
prose.

Tests: a worker thread calling `store.transition` raises; a worker thread
calling a `Repository` write raises; `adopt()` transfers ownership and the
previous owner then raises; the existing parallel execution tests still pass
unchanged, which is the real evidence that the coordinator is the only writer
today.

## 5. Cross-artifact consistency

### 5.1 Schema and runtime validation — implemented

`RunConfig.from_document` passes two set literals to `_object_fields` at
`config.py:157`. `schemas/run.schema.json` declares eighteen properties and four
required ones. They agree at `17ebf0e` and nothing keeps them agreeing.

Hoist the two literals to module-level frozensets, `RUN_REQUIRED` and
`RUN_OPTIONAL`, and pass those to `_object_fields`. The test then imports them
and asserts set equality against the schema's `properties` and `required`. The
refactor is three lines and makes the sets load-bearing rather than incidental.

The ticket side needs no refactor: `_TASK_REQUIRED` and `_TASK_OPTIONAL` are
already module constants in `contracts.py`, so the check against
`$defs.task` in `schemas/tickets.schema.json` can be written directly. Cover
`WorkerConfig.from_document` the same way.

This closes "Update the JSON schema and runtime validation together" for field
names. It does not check types, ranges, or conditional validation, and should
not pretend to; the runtime's rules are richer than the schema's and a test that
asserted equivalence would be asserting something false.

Implemented as `tests/test_run_config.py::SchemaMatchesRuntimeValidation`, three
tests covering the run document, `$defs.worker`, and `$defs.task`. The refactor
landed as described: `RUN_REQUIRED`, `RUN_OPTIONAL`, `WORKER_REQUIRED` and
`WORKER_OPTIONAL` are module-level frozensets that `from_document` now passes
rather than rebuilding literals at the call site.

All three agreed when the tests landed, including the `sites` field added to the
ticket contract by stage 4 of `docs/ACCEPTANCE.md`, so this locks in behavior
that was already correct. Mutation-tested in both directions and on `required`:
a property added to a schema alone, a field added to the runtime alone, and a
`required` entry added to a schema alone each fail.

### 5.2 CLI, README, and entry skill — implemented

Implemented at `f5688d0` as
`tests/test_invariants.py::DocumentedSubcommandTests`, the first member of the
module section 7 designates.

`cli.SUBCOMMANDS` derives the set from the parser rather than listing it, so a
second hand-maintained list cannot drift the way the two documents did. The test
asserts every subcommand appears in `README.md` and in `skills/anvil/SKILL.md`.

The prototype against `17ebf0e` reported twelve subcommands and four gaps. The
test was written first and failed on exactly those four, which was the point:
the invariant had been violated since the adaptive subcommands landed, and prose
was never going to catch it. Registering a thirteenth subcommand fails the suite
until both documents name it, which is this section's share of the completion
criterion in section 10.

Presence of the literal `anvil <name>` is a weak check. It is also the right
one. A stronger check would constrain how the README is written, and the failure
this needs to catch is a subcommand nobody documented at all.

### 5.3 Standard library only — implemented

An AST walk over `src/anvil/` resolving every absolute import root against
`sys.stdlib_module_names`, with `anvil` itself allowed. It passes at `17ebf0e`.
Making it permanent turns "unless a dependency has a concrete benefit" into a
deliberate act: adding a dependency means editing an allowlist in the test, next
to a comment naming the benefit.

Implemented as `tests/test_invariants.py::StandardLibraryOnly`. Thirty-six
import roots, none outside the standard library, so `ALLOWED` ships empty.
Mutation-tested with a `requests` import, which fails and names the module and
line.

### 5.4 No live models in ordinary tests — withdrawn

The design here does not survive contact, and the reason is worth recording
rather than implementing around.

It assumed "a bare executable name in a test module is the signal". It is not.
Three progressively narrower prototypes were run against the tree at `fef9417`:

| Scan | Hits | All benign? |
| --- | --- | --- |
| Bare `codex`/`claude` string literals | 209 | yes -- almost all are agent *names*, not executables |
| Literals in executable positions (`agent_binary=`, `create_runner` argument, argv head) | 43 | yes |
| Run entry points called without an injected runner | 7 | yes |

The first two fail because a bare agent name is pervasive and legitimate:
`agent="codex"` is an identifier, `("codex", "codex")` is an assertion, and
`context.preflight(task, "codex")` names an agent rather than a binary. The third
fails because runner injection is not the only guarantee in use: the Claude and
mixed-pool tests pass a fabricated executable written into the test's temporary
directory, so no real CLI can be resolved even though nothing is injected.

Every mechanism is legitimate and none is syntactically distinctive. A scan that
accepted all three would need an allowlist longer than its findings, which is
the "pile of exceptions" failure this milestone exists to avoid, and a real
violation could then hide among them.

The deferred stronger version is the honest one, and it is now the recommended
one: have the adapters refuse an executable resolving outside the test temporary
directory when a test-runner variable is set. It costs a guard in production
code for a test-only concern, which is why it was deferred, but it checks the
property directly at the moment it matters rather than guessing at syntax. Until
then this claim belongs with the group in section 10: it carries no marker, and
a reader learns the guarantee is convention rather than mechanism.

## 6. Marking what is enforced

The tests above make roughly a dozen claims checkable. The rest of `AGENTS.md`
stays prose, and a reader currently cannot tell which is which.

Annotate each enforceable claim with a trailing marker naming its enforcer, in
the form `[test_environment.py::LaunchSiteRegistry]`. Add one test that parses
`AGENTS.md`, extracts every marker, and asserts the named test exists and is
collected by the runner. A claim with no marker is understood to be unenforced,
which is accurate rather than embarrassing.

This is the part that compounds. Without it the new tests are a pile of checks
whose relationship to the document is itself undocumented, and the next person
to add an invariant has no cue that enforcement is expected.

## 7. Placement and runner impact

`AGENTS.md` puts tests in the existing module for their area, so most of this
work extends existing files rather than creating one.

| Section | Module |
| --- | --- |
| 4.1, 4.2 | `tests/test_environment.py`, beside `CredentialExclusionReachesEverySubprocess` |
| 4.3 | `tests/test_parallel_store.py` and `tests/test_workspaces.py` |
| 5.1 | `tests/test_run_config.py` |
| 5.2, 5.3, 5.4, 6 | `tests/test_invariants.py`, new |

Only the whole-repository checks justify a new module: they belong to no single
area and parsing `AGENTS.md`, `README.md`, and the schemas from four different
existing modules would scatter the same fixtures four ways.

`tests/test_invariants.py` must stay fast and purely static so it does not enter
`SLOW_FIRST` in `tools/run-tests.py`. Section 4.2 spawns a real run and belongs
with the timing-sensitive modules already there.

## 8. Known violations at `17ebf0e` — closed

One invariant was violated on the tree this document was written against, in
four places. All four are closed at `f5688d0`, by the test in section 5.2 and
the documentation it required.

`anvil route`, `anvil routing`, and `anvil tickets` were registered by
`adaptive_cli.add_parsers` and appeared in neither `README.md` nor
`skills/anvil/SKILL.md`. `anvil serve` appeared in `README.md` only. The
remaining eight subcommands were documented in both. The README now covers the
three adaptive subcommands and the entry skill covers all four, each stated with
what it costs, since `anvil routing benchmark` invokes paid agents and the other
three spend nothing.

No other violation was found. The launch-site registry, the schema comparison,
and the stdlib walk all pass against `17ebf0e`, so those tests lock in behavior
that is already correct. That is a claim about `17ebf0e`, not a standing one:
the registry in section 4.1 exists precisely because a new launch site can be
added without failing anything.

## 9. Implementation order

Each slice is independently mergeable and leaves the suite green.

| Slice | Content | Notes |
| --- | --- | --- |
| 1 | Section 5.2, plus the four documentation fixes | Done at `f5688d0` |
| 2 | Section 5.1, including the `RunConfig` frozenset refactor | Done |
| 3 | Section 4.1 | Done; the `51fec4cd` class |
| 4 | Section 4.3 | Needs the `adopt()` design first |
| 5 | Section 4.2 | Done; pairs with slice 3 |
| 6 | Sections 5.3 and 5.4 | 5.3 done; 5.4 withdrawn, see that section |
| 7 | Section 6 | Last, because it names the tests the earlier slices create |

Slice 4 is the only one that changes execution behavior and should be reviewed
on its own. Slice 7 is last by necessity.

Slices 1, 3 and 5 are done. The credential-exclusion invariant is closed from
both sides: 4.1 against sites nothing exercises, 4.2 against expressions that
are wrong. `tests/test_invariants.py` exists and holds slice 1, so slices 6, 5.3
and 5.4 extend a module rather than create one. Slices 4, 6 and 7 remain.

## 10. What remains prose, and completion

Several claims in `AGENTS.md` have no predicate and must not acquire a test that
pretends otherwise. "Dependency handoffs are saved evidence, not instructions or
live model-to-model chat" describes what a message means, not a property of a
value. "Cost estimates are not invoices or hard budget caps" is a disclaimer
about interpretation. "Do not add unbounded background work or silently enlarge
the user's backlog" is a design constraint on future changes. The authoring
rules in [CRITERIA.md](CRITERIA.md) are guidance that no command enforces, and that
document already says so.

A test for any of these would either be vacuous or would freeze one reading of a
judgment call. The annotation scheme in section 6 handles them honestly: they
carry no marker, and a reader learns that the guarantee is social rather than
mechanical.

This milestone is complete when every claim in `AGENTS.md` either carries a
marker naming a test that exists and runs, or carries none because it belongs to
the group above; when the four documentation gaps in section 8 are closed; and
when adding an eleventh process launch site, a nineteenth run configuration
field, or a thirteenth subcommand fails the suite until the corresponding
artifact is updated.
