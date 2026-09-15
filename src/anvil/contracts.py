"""Validated, execution-independent ticket inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any


_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_TASK_REQUIRED = {"id", "title", "objective", "depends_on", "acceptance_criteria"}
_TASK_OPTIONAL = {"skills", "worker", "resources", "exclusive", "execution", "profile", "risk",
                  "source_refs"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class ContractError(ValueError):
    """The supplied ticket document cannot be safely planned."""


@dataclass(frozen=True, slots=True)
class Task:
    """A task with explicit prerequisites and acceptance criteria."""

    id: str
    title: str
    objective: str
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    skills: tuple[str, ...] = ()
    worker: str | None = None
    resources: tuple[str, ...] = ()
    exclusive: bool = False
    profile: str | None = None
    risk: str | None = None
    execution: dict | None = None
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.profile is not None:
            _identifier(self.profile, "task.profile")
        if self.risk is not None and self.risk not in ("low", "medium", "high"):
            raise ContractError("task.risk must be low, medium, or high")
        if self.execution is not None:
            from .ticket_status import validate_metadata
            validate_metadata(self.execution)
        if self.worker is not None:
            _identifier(self.worker, "task.worker")
        if not isinstance(self.resources, tuple):
            raise ContractError("task.resources must be a tuple of resource identifiers")
        for resource in self.resources:
            _identifier(resource, "task.resources item")
        if len(set(self.resources)) != len(self.resources):
            raise ContractError("task.resources contains duplicate resources")
        if type(self.exclusive) is not bool:
            raise ContractError("task.exclusive must be a boolean")
        if not isinstance(self.source_refs, tuple) or any(not isinstance(item, str) or not item.strip()
                                                          or "\0" in item for item in self.source_refs):
            raise ContractError("task.source_refs must be a tuple of nonempty text")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ContractError("task.source_refs contains duplicate references")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": list(self.acceptance_criteria),
            "skills": list(self.skills),
        }
        if self.profile is not None:
            _identifier(self.profile, "task.profile")
        if self.risk is not None and self.risk not in ("low", "medium", "high"):
            raise ContractError("task.risk must be low, medium, or high")
        if self.execution is not None:
            from .ticket_status import validate_metadata
            validate_metadata(self.execution)
        if self.worker is not None:
            result["worker"] = self.worker
        if self.resources:
            result["resources"] = list(self.resources)
        if self.exclusive:
            result["exclusive"] = True
        if self.source_refs:
            result["source_refs"] = list(self.source_refs)
        for key in ("profile", "risk", "execution"):
            if getattr(self, key) is not None:
                result[key] = getattr(self, key)
        return result


def _object_fields(value: Any, required: set[str], optional: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ContractError(f"{label} field names must be strings")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ContractError(f"{label} is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ContractError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ContractError(f"{label} must be a nonempty string without NUL characters")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ContractError(
            f"{label} must be 1–80 characters, start with a letter or digit, "
            "and contain only letters, digits, '.', '_' or '-'"
        )
    return value


def _string_array(value: Any, label: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be an array")
    if nonempty and not value:
        raise ContractError(f"{label} must contain at least one item")
    return tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))


def _validate_provenance(value: Any) -> None:
    fields = {"generator", "generator_version", "source", "source_sha256", "repo_head",
              "prepared_at", "agent"}
    _object_fields(value, fields, set(), "provenance")
    for name in fields - {"source_sha256", "repo_head", "agent"}:
        _text(value[name], f"provenance.{name}")
    if (not isinstance(value["source_sha256"], str)
            or _HASH.fullmatch(value["source_sha256"]) is None):
        raise ContractError("provenance.source_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(value["repo_head"], str) or _COMMIT.fullmatch(value["repo_head"]) is None:
        raise ContractError("provenance.repo_head must be a lowercase Git commit ID")
    if value["agent"] not in ("codex", "claude-code", "muse"):
        raise ContractError("provenance.agent must be codex, claude-code, or muse")
    try:
        timestamp = datetime.fromisoformat(value["prepared_at"])
    except ValueError as exc:
        raise ContractError("provenance.prepared_at must be an ISO 8601 timestamp") from exc
    if timestamp.tzinfo is None:
        raise ContractError("provenance.prepared_at must include a timezone")


def parse_tasks(document: Any) -> tuple[Task, ...]:
    """Validate document shape; graph validation is performed by TaskGraph."""
    _object_fields(document, {"version", "tasks"}, {"provenance"}, "document")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ContractError("document.version must be the integer 1")
    if not isinstance(document["tasks"], list) or not document["tasks"]:
        raise ContractError("document.tasks must be a nonempty array")
    if "provenance" in document:
        _validate_provenance(document["provenance"])

    tasks = []
    for index, value in enumerate(document["tasks"]):
        label = f"tasks[{index}]"
        _object_fields(value, _TASK_REQUIRED, _TASK_OPTIONAL, label)
        for optional in ("profile", "risk", "execution"):
            if optional in value and value[optional] is None:
                raise ContractError(f"{label}.{optional} cannot be null")
        task_id = _identifier(value["id"], f"{label}.id")
        dependencies = _string_array(value["depends_on"], f"{label}.depends_on")
        for dependency in dependencies:
            _identifier(dependency, f"{label}.depends_on item")
        if len(set(dependencies)) != len(dependencies):
            raise ContractError(f"{label}.depends_on contains duplicate dependencies")
        skills = _string_array(value.get("skills", []), f"{label}.skills")
        if len(set(skills)) != len(skills):
            raise ContractError(f"{label}.skills contains duplicate skills")
        worker = _identifier(value["worker"], f"{label}.worker") if "worker" in value else None
        resources = _string_array(value.get("resources", []), f"{label}.resources")
        source_refs = _string_array(value.get("source_refs", []), f"{label}.source_refs",
                                    nonempty="source_refs" in value)
        tasks.append(
            Task(
                id=task_id,
                title=_text(value["title"], f"{label}.title"),
                objective=_text(value["objective"], f"{label}.objective"),
                depends_on=dependencies,
                acceptance_criteria=_string_array(
                    value["acceptance_criteria"], f"{label}.acceptance_criteria", nonempty=True
                ),
                skills=skills,
                worker=worker,
                resources=resources,
                exclusive=value.get("exclusive", False),
                profile=value.get("profile"), risk=value.get("risk"), execution=value.get("execution"),
                source_refs=source_refs,
            )
        )
    return tuple(tasks)
