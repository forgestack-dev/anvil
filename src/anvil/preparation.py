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
from .adapters import create_runner
from .contracts import ContractError
from .planning import TaskGraph
from .skill_management import SkillError, SkillScope, managed_snapshot, status
from .ticket_status import atomic, encoded
from .workspaces import Repository, WorkspaceError


MAX_SPEC_BYTES = 2 * 1024 * 1024
_TASK_FIELDS = {"id", "title", "objective", "depends_on", "acceptance_criteria",
                "source_refs", "skills", "resources", "exclusive", "risk"}


def _read_spec(path: Path, repo: Path) -> bytes:
    path = Path(path).expanduser().absolute()
    resolved = path.resolve()
    if not path.is_relative_to(repo) or not resolved.is_relative_to(repo):
        raise ContractError("specification must be inside the target repository")
    try:
        relative = path.relative_to(repo)
        current = repo
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ContractError(f"specification path must not contain symlinks: {current}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ContractError("specification must be a regular file")
            data = stream.read(MAX_SPEC_BYTES + 1)
        if len(data) > MAX_SPEC_BYTES:
            raise ContractError("specification exceeds 2 MiB")
        data.decode("utf-8")
        return data
    except ContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read UTF-8 specification {path}: {exc}") from exc


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
            "required": ["version", "tasks"],
            "properties": {"version": {"type": "integer", "const": 1},
                           "tasks": {"type": "array", "minItems": 1,
                                     "maxItems": 100, "items": task}}}


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
        "Skills may be selected only from this installed and classified catalog. Use a skill only "
        "when its workflow materially helps implement that ticket; compatible_agents shows which "
        "Anvil workers can run it. Use an empty skills array when none applies.\n"
        f"{catalog}\n\nSource path: {source}\n"
        f"--- BEGIN UNTRUSTED SPECIFICATION ---\n{content}\n--- END UNTRUSTED SPECIFICATION ---"
    )


def prepare(source: Path, output: Path, *, repo: Path, agent: str = "codex",
            executable: str | None = None, timeout: float = 900,
            artifact_root: Path | None = None, runner=None) -> dict:
    """Generate, validate, and atomically write tickets; runner injection is test-only."""
    if agent not in ("codex", "claude-code", "muse"):
        raise ContractError("preparation agent must be codex, claude-code, or muse")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 3600):
        raise ContractError("preparation timeout must be between 0 and 3600 seconds")
    repository = Repository(repo)
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
    binary = executable or ("codex" if agent == "codex" else "claude" if agent == "claude-code" else "muse")
    selected = runner if runner is not None else create_runner(agent, binary)
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
    if (not isinstance(document, dict) or set(document) != {"version", "tasks"}
            or not isinstance(document.get("tasks"), list)):
        raise ContractError("prepared ticket result must contain only version and tasks")
    if not 1 <= len(document["tasks"]) <= 100:
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
    final = {"version": 1, "provenance": {
        "generator": "anvil", "generator_version": __version__,
        "source": relative_source,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "repo_head": repository.head(),
        "prepared_at": datetime.now(timezone.utc).isoformat(), "agent": agent,
    }, "tasks": []}
    for task in graph.tasks:
        value = task.to_dict()
        value["execution"] = {"status": "todo"}
        final["tasks"].append(value)
    TaskGraph.from_document(final)
    atomic(output, encoded(final))
    return {"status": "prepared", "output": str(output), "source": str(source),
            "artifact_dir": str(artifact_dir), "task_count": len(graph.tasks),
            "wave_count": len(graph.waves), "available_skills": sorted(allowed),
            "unclassified_skills": unclassified, "provenance": final["provenance"]}
