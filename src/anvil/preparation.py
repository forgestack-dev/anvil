"""Create a reviewable ticket graph from a committed Markdown specification."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

from . import __version__
from .adapters import EXECUTION_AGENTS, create_runner, probe_agent
from .contracts import ContractError, _object_fields, _text
from .routing import preflight, validate_selection
from .planning import TaskGraph, _reject_constant, _unique_object
from .processes import ProcessError
from .skill_management import SkillError, SkillScope, managed_snapshot, status
from .ticket_status import atomic, encoded
from .workspaces import Repository, WorkspaceError


MAX_SPEC_BYTES = 2 * 1024 * 1024
MAX_QUESTIONS = 50
_TASK_FIELDS = {"id", "title", "objective", "depends_on", "acceptance_criteria",
                "source_refs", "skills", "resources", "exclusive", "risk"}
_QUESTION_REQUIRED = {"id", "rule", "kind", "question", "source_refs"}
_QUESTION_KINDS = ("missing", "ambiguous", "undecidable", "unbounded")
# The gate runs on the execution adapter the preparation turn did not select.
# A gate on the authoring adapter grades its own output, so `prepare` refuses
# the match; "none" disables the gate and is recorded in provenance.
_GATE_DEFAULT = {"codex": "claude-code", "claude-code": "codex", "muse": "codex"}


def _read_spec(path: Path, repo: Path, *, label: str = "specification") -> bytes:
    path = Path(path).expanduser().absolute()
    resolved = path.resolve()
    if not path.is_relative_to(repo) or not resolved.is_relative_to(repo):
        raise ContractError(f"{label} must be inside the target repository")
    try:
        relative = path.relative_to(repo)
        current = repo
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ContractError(f"{label} path must not contain symlinks: {current}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ContractError(f"{label} must be a regular file")
            data = stream.read(MAX_SPEC_BYTES + 1)
        if len(data) > MAX_SPEC_BYTES:
            raise ContractError(f"{label} exceeds 2 MiB")
        data.decode("utf-8")
        return data
    except ContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read UTF-8 {label} {path}: {exc}") from exc


def _output_path(path: Path, repo: Path) -> Path:
    requested = Path(path).expanduser().absolute()
    resolved = requested.resolve()
    if not requested.is_relative_to(repo) or not resolved.is_relative_to(repo):
        raise ContractError("ticket output must be inside the target repository")
    current = repo
    for part in requested.relative_to(repo).parts:
        current /= part
        if current.is_symlink():
            raise ContractError(f"ticket output path must not contain symlinks: {current}")
    return resolved


def _skills(repo: Path) -> tuple[list[dict], list[str]]:
    scope = SkillScope(repo)
    try:
        report = status(scope)
        if report["status"] == "not-installed":
            return [], []
        if report["status"] != "installed":
            raise ContractError("AI Hero installation has local changes; run anvil skills status aihero")
        with managed_snapshot(scope) as manifest:
            from .skill_runtime import _capabilities, _registry
            registry = _registry()["entries"]
            available, unclassified = [], []
            for name, installed in sorted(manifest["skills"].items()):
                entry = registry.get(name)
                digest = installed["files"]["SKILL.md"]["sha256"]
                if entry is None or entry["sha256"] != digest:
                    unclassified.append(name)
                    continue
                compatible = [agent for agent in ("codex", "claude-code", "muse")
                              if set(entry["requires"]) <= _capabilities(agent)]
                if compatible:
                    available.append({"name": name, "requires": entry["requires"],
                                      "compatible_agents": compatible})
            return available, unclassified
    except SkillError as exc:
        raise ContractError(str(exc)) from exc


def _question_schema() -> dict:
    """One bound and located question; the gate may return nothing else."""
    text = {"type": "string", "minLength": 1, "pattern": r"\S"}
    return {
        "type": "object", "additionalProperties": False,
        "required": sorted(_QUESTION_REQUIRED),
        "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 80,
                   "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
            "rule": {"type": "integer", "minimum": 1, "maximum": 6},
            "kind": {"type": "string", "enum": list(_QUESTION_KINDS)},
            "question": text,
            "source_refs": {"type": "array", "minItems": 1, "uniqueItems": True,
                            "items": text},
            "options": {"type": "array", "uniqueItems": True, "items": text},
        },
    }


def gate_schema() -> dict:
    """The gate has no tasks branch and no verdict: it may only ask."""
    return {"type": "object", "additionalProperties": False,
            "required": ["version", "questions"],
            "properties": {"version": {"type": "integer", "const": 1},
                           "questions": {"type": "array", "maxItems": MAX_QUESTIONS,
                                         "items": _question_schema()}}}


def result_schema(skill_names: list[str]) -> dict:
    text = {"type": "string", "minLength": 1, "pattern": r"\S"}
    identifier = {"type": "string", "minLength": 1, "maxLength": 80,
                  "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"}
    skill_item = ({"type": "string", "enum": skill_names}
                  if skill_names else {"type": "string"})
    skills = {"type": "array", "uniqueItems": True, "items": skill_item}
    if not skill_names:
        skills["maxItems"] = 0
    task = {
        "type": "object", "additionalProperties": False,
        "required": sorted(_TASK_FIELDS),
        "properties": {
            "id": identifier, "title": text, "objective": text,
            "depends_on": {"type": "array", "uniqueItems": True, "items": identifier},
            "acceptance_criteria": {"type": "array", "minItems": 1, "items": text},
            "source_refs": {"type": "array", "minItems": 1, "uniqueItems": True,
                            "items": text},
            "skills": skills,
            "resources": {"type": "array", "uniqueItems": True, "items": identifier},
            "exclusive": {"type": "boolean"},
            "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }
    return {"type": "object", "additionalProperties": False,
            "required": ["version"],
            "oneOf": [{"required": ["tasks"]}, {"required": ["questions"]}],
            "properties": {"version": {"type": "integer", "const": 1},
                           "tasks": {"type": "array", "minItems": 1,
                                     "maxItems": 100, "items": task},
                           "questions": {"type": "array", "minItems": 1,
                                         "maxItems": MAX_QUESTIONS,
                                         "items": _question_schema()}}}


def _prompt(source: str, content: str, skills: list[dict]) -> str:
    catalog = json.dumps(skills, indent=2)
    return (
        "Convert the committed Markdown specification below into a minimal, executable Anvil "
        "ticket graph. This is a planning turn: do not edit files, run commands, commit, publish, "
        "or implement the work. Preserve settled scope and expose ambiguity as an acceptance "
        "criterion or a narrowly scoped ticket; do not invent product features. Each ticket must "
        "be independently reviewable, use stable IDs, name only real prerequisite IDs in "
        "depends_on, declare shared resources that should not be changed concurrently, and use "
        "exclusive only when overlap with every other active task is unsafe. Acceptance criteria "
        "must describe observable outcomes. Every source_refs item must be an exact stripped "
        "Markdown heading or requirement-label line from the specification, including any heading "
        "marker, so Anvil can verify traceability. Set risk to low, medium, or "
        "high. Return only the schema-shaped object.\n\n"
        "If the specification does not determine a ticket graph, return questions instead of "
        "tasks: do not invent the missing decision. Each question names the criterion contract "
        "rule it invokes (1 every criterion names its decision procedure, 2 one criterion one "
        "role, 3 an absence names the set it holds over, 4 one criterion one claim, 5 criteria "
        "are frozen for the run, 6 prefer a criterion the worker cannot grade itself), a kind of "
        "missing, ambiguous, undecidable, or unbounded, and source_refs that are exact stripped "
        "lines of the specification.\n\n"
        "Skills may be selected only from this installed and classified catalog. Use a skill only "
        "when its workflow materially helps implement that ticket; compatible_agents shows which "
        "Anvil workers can run it. Use an empty skills array when none applies.\n"
        f"{catalog}\n\nSource path: {source}\n"
        f"--- BEGIN UNTRUSTED SPECIFICATION ---\n{content}\n--- END UNTRUSTED SPECIFICATION ---"
    )


_CRITERION_RULES = (
    "1. Every criterion names its decision procedure: the run's configured verification "
    "commands, a named region of the candidate diff, or a named path at the integration "
    "revision.\n"
    "2. One criterion, one role. Independent review can read but cannot execute, so a "
    "criterion decided by running something must not be left to it.\n"
    "3. An absence names the complete set it holds over. 'No X anywhere' is not decidable.\n"
    "4. One criterion, one claim. The acceptance map is per-criterion and boolean.\n"
    "5. Criteria are frozen for the run; a requirement discovered later is a new ticket.\n"
    "6. Prefer a criterion the implementing turn cannot grade with its own new tests.\n"
)


def _gate_prompt(source: str, content: str, tasks: list[dict]) -> str:
    """Judge a graph against the criterion contract without seeing its author."""
    graph = json.dumps({"tasks": tasks}, indent=2)
    return (
        "Adversarially review the proposed Anvil ticket graph below against the specification it "
        "claims to implement. This is a read-only judging turn: do not edit files, run commands, "
        "commit, or implement anything, and do not propose a replacement graph.\n\n"
        "You cannot approve. Return only questions the specification must answer before this graph "
        "is executable. Return an empty questions array when you have none; that records the "
        "absence of an objection and is not an endorsement.\n\n"
        "Every question must name the criterion contract rule it invokes and cite source_refs that "
        "are exact stripped lines of the specification, so an unfounded question is visible as "
        "one. The rules:\n"
        f"{_CRITERION_RULES}\n"
        "Use kind undecidable for rule 1 or 2, unbounded for rule 3, and missing or ambiguous when "
        "the specification simply does not settle something. Ask only what the specification can "
        "answer; a question about the repository's code is not one.\n\n"
        f"Source path: {source}\n"
        f"--- BEGIN PROPOSED TICKET GRAPH ---\n{graph}\n--- END PROPOSED TICKET GRAPH ---\n"
        f"--- BEGIN UNTRUSTED SPECIFICATION ---\n{content}\n--- END UNTRUSTED SPECIFICATION ---"
    )


def _document_gate_prompt(source: str, document_text: str, tasks: list[dict]) -> str:
    """Judge a committed ticket document `prepare` did not write.

    There is no specification to compare the graph to, so a question's
    citation binds to the ticket document's own text instead: the same
    bound-and-located contract `_gate_prompt` applies to a specification.
    """
    graph = json.dumps({"tasks": tasks}, indent=2)
    return (
        "Adversarially review the committed Anvil ticket graph below against the criterion "
        "contract. This is a read-only judging turn: do not edit files, run commands, commit, "
        "or implement anything, and do not propose a replacement graph. No preparation turn "
        "produced this graph, so there is no author's account to weigh; judge it exactly as "
        "written.\n\n"
        "You cannot approve. Return only questions the ticket document must answer before this "
        "graph is executable. Return an empty questions array when you have none; that records "
        "the absence of an objection and is not an endorsement.\n\n"
        "Every question must name the criterion contract rule it invokes and cite source_refs "
        "that are exact stripped lines of the ticket document below, so an unfounded question is "
        "visible as one. The rules:\n"
        f"{_CRITERION_RULES}\n"
        "Use kind undecidable for rule 1 or 2, unbounded for rule 3, and missing or ambiguous "
        "when the ticket document simply does not settle something.\n\n"
        f"Source path: {source}\n"
        f"--- BEGIN TICKET GRAPH ---\n{graph}\n--- END TICKET GRAPH ---\n"
        f"--- BEGIN COMMITTED TICKET DOCUMENT ---\n{document_text}\n--- END COMMITTED TICKET DOCUMENT ---"
    )


def _questions(value, source_lines: set[str], label: str) -> list[dict]:
    """Validate questions from either turn; the caller never writes tickets with any."""
    if not isinstance(value, list):
        raise ContractError(f"{label} must be an array")
    if len(value) > MAX_QUESTIONS:
        raise ContractError(f"{label} must contain at most {MAX_QUESTIONS} questions")
    seen: set[str] = set()
    result = []
    for index, item in enumerate(value):
        name = f"{label}[{index}]"
        _object_fields(item, set(_QUESTION_REQUIRED), {"options"}, name)
        identity = _text(item["id"], f"{name}.id")
        if identity in seen:
            raise ContractError(f"{label} contains duplicate question ID: {identity}")
        seen.add(identity)
        if type(item["rule"]) is not int or not 1 <= item["rule"] <= 6:
            raise ContractError(f"{name}.rule must be a criterion contract rule from 1 to 6")
        if item["kind"] not in _QUESTION_KINDS:
            raise ContractError(f"{name}.kind must be one of {', '.join(_QUESTION_KINDS)}")
        _text(item["question"], f"{name}.question")
        references = item["source_refs"]
        if not isinstance(references, list) or not references:
            raise ContractError(f"{name}.source_refs must be a nonempty array")
        for reference in references:
            _text(reference, f"{name}.source_refs item")
            if reference not in source_lines:
                raise ContractError(
                    f"{name}.source_refs cites a line absent from the specification: {reference}")
        if len(set(references)) != len(references):
            raise ContractError(f"{name}.source_refs contains duplicate references")
        for option in item.get("options", []):
            _text(option, f"{name}.options item")
        result.append(dict(item))
    return result


def _profile(agent: str, profile, label: str):
    """Validate an explicit model and effort selection for one preparation turn."""
    if profile is None:
        return None
    if agent == "muse":
        raise ContractError(
            f"{label} profile is unsupported: Muse turns are an operator handoff with no "
            "model or effort selection")
    if not isinstance(profile, dict):
        raise ContractError(f"{label} profile must be an object with model and effort")
    unknown = profile.keys() - {"model", "effort", "max_budget_usd"}
    if unknown:
        raise ContractError(f"{label} profile has unknown fields: {', '.join(sorted(unknown))}")
    try:
        validate_selection(agent, profile)
    except ContractError as exc:
        raise ContractError(f"{label} profile: {exc}") from exc
    return dict(profile)


def _binary(agent: str, executable: str | None) -> str:
    if executable:
        return executable
    return "codex" if agent == "codex" else "claude" if agent == "claude-code" else "muse"


def _gate_selection(agent: str, gate_agent: str | None) -> str | None:
    """Resolve the gate adapter; None disables it and is recorded in provenance."""
    selected = _GATE_DEFAULT[agent] if gate_agent is None else gate_agent
    if selected == "none":
        return None
    if selected not in EXECUTION_AGENTS:
        raise ContractError("gate agent must be codex, claude-code, muse, or none")
    if selected == agent:
        raise ContractError(
            "gate agent must differ from the preparation agent; pass none to disable the gate")
    return selected


# A hand-written ticket document has no authoring adapter for `_GATE_DEFAULT` to key
# on, so `gate_document` falls back to this fixed, recorded choice instead of a lookup.
_DOCUMENT_GATE_DEFAULT = "codex"


def _document_gate_agent(document: dict, gate_agent: str | None) -> str:
    """Resolve the gate adapter for a ticket document `prepare` may not have written.

    `prepare`'s rule -- refuse a gate on the adapter that authored the graph -- still
    applies when the document carries one in `provenance.agent`, which happens when an
    already-prepared document is gated again. A hand-written graph carries none, so
    there is nothing to derive a default from; the fallback is `_DOCUMENT_GATE_DEFAULT`
    rather than a `_GATE_DEFAULT` lookup keyed on an authoring adapter that does not
    exist, and the selection -- explicit or defaulted -- is always recorded.
    """
    authoring = None
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        authoring = provenance.get("agent")
    selected = gate_agent if gate_agent is not None else _DOCUMENT_GATE_DEFAULT
    if selected not in EXECUTION_AGENTS:
        raise ContractError("gate agent must be codex, claude-code, or muse")
    if authoring is not None and selected == authoring:
        raise ContractError(
            "gate agent must differ from the adapter that authored this ticket document "
            f"({authoring}); choose another with --gate-agent")
    return selected


def _exclusion(value) -> tuple[str, ...]:
    """The same variable-name rule RunConfig.credential_exclusion enforces."""
    names = tuple(value)
    if (any(not isinstance(name, str) or not name.strip() or "\0" in name or "=" in name
            for name in names) or len(set(names)) != len(names)):
        raise ContractError(
            "credential exclusion must be unique nonempty variable names without NUL or '='")
    return names


def _probe(agent: str, binary: str, profile, exclude: tuple[str, ...], label: str) -> None:
    """Confirm the agent is installed, and that it advertises the profile's controls.

    Both probes withhold the exclusion set: `probe_agent` passes it to the
    adapter's doctor, and `preflight` additionally confirms the CLI advertises the
    controls a profile depends on. Muse launches no subprocess either way.
    """
    try:
        if profile is None or agent == "muse":
            probe_agent(agent, binary, exclude=exclude)
        else:
            preflight(agent, binary, profile, exclude=exclude)
    except (ContractError, ProcessError, OSError) as exc:
        remedy = "; pass none to disable the gate" if label == "gate agent" else ""
        raise ContractError(f"{label} {agent} is not available: {exc}{remedy}") from exc


def _distinct(profile, gate_profile) -> None:
    """A different adapter running the same model is not a second opinion."""
    if profile and gate_profile and profile["model"] == gate_profile["model"]:
        raise ContractError(
            f"gate model must differ from the preparation model: {profile['model']}")


def prepare(source: Path, output: Path, *, repo: Path, agent: str = "codex",
            executable: str | None = None, timeout: float = 900,
            artifact_root: Path | None = None, runner=None, profile=None,
            exclude: tuple[str, ...] = (), gate_agent: str | None = None,
            gate_executable: str | None = None, gate_profile=None, gate_runner=None) -> dict:
    """Generate, gate, validate, and atomically write tickets; runner injection is test-only."""
    if agent not in ("codex", "claude-code", "muse"):
        raise ContractError("preparation agent must be codex, claude-code, or muse")
    gate = _gate_selection(agent, gate_agent)
    profile = _profile(agent, profile, "preparation")
    if gate is None and gate_profile is not None:
        raise ContractError("a gate profile requires a gate agent")
    gate_profile = _profile(gate, gate_profile, "gate") if gate is not None else None
    _distinct(profile, gate_profile)
    exclude = _exclusion(exclude)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 3600):
        raise ContractError("preparation timeout must be between 0 and 3600 seconds")
    repository = Repository(repo, exclude=exclude)
    repository.assert_clean()
    source = Path(source).expanduser().absolute()
    data = _read_spec(source, repository.path)
    source = source.resolve()
    relative_source = str(source.relative_to(repository.path))
    try:
        repository.git("ls-files", "--error-unmatch", "--", relative_source)
    except WorkspaceError as exc:
        raise ContractError("specification must be committed in the target repository") from exc
    output = _output_path(output, repository.path)
    if output.exists() or output.is_symlink():
        raise ContractError(f"ticket output already exists: {output}")
    if not output.parent.is_dir():
        raise ContractError(f"ticket output parent does not exist: {output.parent}")
    skills, unclassified = _skills(repository.path)
    artifact_root = Path(artifact_root or Path.home() / ".local/state/anvil/preparations").expanduser()
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_dir = artifact_root.resolve() / uuid.uuid4().hex
    binary = _binary(agent, executable)
    gate_binary = _binary(gate, gate_executable) if gate is not None else None
    if runner is None and (profile is not None or exclude):
        _probe(agent, binary, profile, exclude, "preparation agent")
    if gate is not None and gate_runner is None:
        _probe(gate, gate_binary, gate_profile, exclude, "gate agent")
    selected = (runner if runner is not None
                else create_runner(agent, binary, profile=profile, exclude=exclude))
    invocation = dict(
        repo=repository.path,
        prompt=_prompt(relative_source, data.decode("utf-8"), skills),
        schema=result_schema([item["name"] for item in skills]),
        artifact_dir=artifact_dir,
        timeout=timeout,
        read_only=True,
    )
    if agent == "muse" and runner is None:
        invocation["purpose"] = "plan"
    document = selected.run(**invocation)
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ContractError("prepared result must be an object with version 1")
    if set(document) not in ({"version", "tasks"}, {"version", "questions"}):
        raise ContractError("prepared result must contain version and exactly one of "
                            "tasks or questions")
    source_lines = {line.strip() for line in data.decode("utf-8").splitlines() if line.strip()}
    if "questions" in document:
        questions = _questions(document["questions"], source_lines, "prepared questions")
        if not questions:
            raise ContractError("a questions result must contain at least one question")
        return {"status": "needs_clarification", "raised_by": "preparation",
                "source": str(source), "source_sha256": hashlib.sha256(data).hexdigest(),
                "artifact_dir": str(artifact_dir), "questions": questions}
    if not isinstance(document["tasks"], list) or not 1 <= len(document["tasks"]) <= 100:
        raise ContractError("prepared ticket result must contain between 1 and 100 tasks")
    for index, task in enumerate(document["tasks"]):
        if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
            raise ContractError(f"prepared tasks[{index}] does not match the required ticket fields")
    graph = TaskGraph.from_document(document)
    allowed = {item["name"] for item in skills}
    if any(not task.source_refs for task in graph.tasks):
        raise ContractError("every prepared ticket must contain source_refs")
    source_lines = {line.strip() for line in data.decode("utf-8").splitlines() if line.strip()}
    invalid_refs = sorted({reference for task in graph.tasks for reference in task.source_refs
                           if reference not in source_lines})
    if invalid_refs:
        raise ContractError("prepared tickets contain source_refs absent from the specification: "
                            + ", ".join(invalid_refs))
    unknown = sorted({name for task in graph.tasks for name in task.skills if name not in allowed})
    if unknown:
        raise ContractError("prepared tickets selected unavailable skills: " + ", ".join(unknown))
    tasks = [task.to_dict() for task in graph.tasks]
    gate_artifact_dir = None
    gate_record = None
    if gate is not None:
        gate_artifact_dir = artifact_root.resolve() / uuid.uuid4().hex
        judge = (gate_runner if gate_runner is not None
                 else create_runner(gate, gate_binary, profile=gate_profile, exclude=exclude))
        request = dict(
            repo=repository.path,
            prompt=_gate_prompt(relative_source, data.decode("utf-8"), tasks),
            schema=gate_schema(),
            artifact_dir=gate_artifact_dir,
            timeout=timeout,
            read_only=True,
        )
        if gate == "muse" and gate_runner is None:
            request["purpose"] = "plan"
        verdict = judge.run(**request)
        if (not isinstance(verdict, dict) or set(verdict) != {"version", "questions"}
                or verdict["version"] != 1):
            raise ContractError("gate result must contain only version and questions")
        raised = _questions(verdict["questions"], source_lines, "gate questions")
        if raised:
            return {"status": "needs_clarification", "raised_by": "gate",
                    "gate_agent": gate, "source": str(source),
                    "source_sha256": hashlib.sha256(data).hexdigest(),
                    "artifact_dir": str(artifact_dir),
                    "gate_artifact_dir": str(gate_artifact_dir), "questions": raised}
        gate_record = {"agent": gate, "distinct_adapter": gate != agent, "questions": 0}
        if gate_profile is not None:
            gate_record["model"] = gate_profile["model"]
    final = {"version": 1, "provenance": {
        "generator": "anvil", "generator_version": __version__,
        "source": relative_source,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "repo_head": repository.head(),
        "prepared_at": datetime.now(timezone.utc).isoformat(), "agent": agent,
        "gate": gate_record,
    }, "tasks": []}
    if profile is not None:
        final["provenance"]["model"] = profile["model"]
    for value in tasks:
        value["execution"] = {"status": "todo"}
        final["tasks"].append(value)
    TaskGraph.from_document(final)
    atomic(output, encoded(final))
    return {"status": "prepared", "output": str(output), "source": str(source),
            "artifact_dir": str(artifact_dir),
            "gate_artifact_dir": str(gate_artifact_dir) if gate_artifact_dir else None,
            "task_count": len(graph.tasks),
            "wave_count": len(graph.waves), "available_skills": sorted(allowed),
            "unclassified_skills": unclassified, "provenance": final["provenance"]}


def gate_document(path: Path, *, repo: Path, gate_agent: str | None = None,
                   gate_executable: str | None = None, timeout: float = 900,
                   artifact_root: Path | None = None, gate_profile=None,
                   gate_runner=None, exclude: tuple[str, ...] = ()) -> dict:
    """Run the readiness gate over a committed ticket document `prepare` did not write.

    Reads the document and the target repository only, so a hand-written graph gets
    the same intake scrutiny as a generated one: no planning turn runs first, nothing
    is written back, no run starts, and this never touches Git or the ledger -- a
    graph cannot be silently rewritten by being inspected. `prepare`'s own behavior,
    including its `_GATE_DEFAULT` lookup and write path, is untouched by this function.
    """
    exclude = _exclusion(exclude)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 3600):
        raise ContractError("gate timeout must be between 0 and 3600 seconds")
    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise ContractError(f"repository does not exist: {repo}")
    path = Path(path).expanduser().absolute()
    data = _read_spec(path, repo, label="ticket document")
    path = path.resolve()
    relative_document = str(path.relative_to(repo))
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except ContractError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except (ValueError, RecursionError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc
    graph = TaskGraph.from_document(document)
    selected = _document_gate_agent(document, gate_agent)
    gate_profile = _profile(selected, gate_profile, "gate")
    binary = _binary(selected, gate_executable)
    artifact_root = Path(artifact_root or Path.home() / ".local/state/anvil/preparations").expanduser()
    artifact_root.mkdir(parents=True, exist_ok=True)
    gate_artifact_dir = artifact_root.resolve() / uuid.uuid4().hex
    if gate_runner is None:
        _probe(selected, binary, gate_profile, exclude, "readiness gate agent")
    judge = (gate_runner if gate_runner is not None
             else create_runner(selected, binary, profile=gate_profile, exclude=exclude))
    tasks = [task.to_dict() for task in graph.tasks]
    source_lines = {line.strip() for line in data.decode("utf-8").splitlines() if line.strip()}
    request = dict(
        repo=repo,
        prompt=_document_gate_prompt(relative_document, data.decode("utf-8"), tasks),
        schema=gate_schema(),
        artifact_dir=gate_artifact_dir,
        timeout=timeout,
        read_only=True,
    )
    if selected == "muse" and gate_runner is None:
        request["purpose"] = "plan"
    verdict = judge.run(**request)
    if (not isinstance(verdict, dict) or set(verdict) != {"version", "questions"}
            or verdict["version"] != 1):
        raise ContractError("gate result must contain only version and questions")
    raised = _questions(verdict["questions"], source_lines, "gate questions")
    provenance = {"generator": "anvil", "generator_version": __version__,
                  "document": relative_document,
                  "document_sha256": hashlib.sha256(data).hexdigest(),
                  "gated_at": datetime.now(timezone.utc).isoformat(),
                  "gate": {"agent": selected, "questions": len(raised)}}
    if gate_profile is not None:
        provenance["gate"]["model"] = gate_profile["model"]
    result = {"status": "needs_clarification" if raised else "clean",
              "document": str(path), "gate_agent": selected,
              "artifact_dir": str(gate_artifact_dir), "provenance": provenance}
    if raised:
        result["raised_by"] = "gate"
        result["questions"] = raised
    else:
        result["task_count"] = len(graph.tasks)
    return result
