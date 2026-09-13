"""Validated, execution-independent ticket inputs."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_TASK_REQUIRED = {"id", "title", "objective", "depends_on", "acceptance_criteria"}
_TASK_OPTIONAL = {"skills", "worker", "resources", "exclusive"}


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

    def __post_init__(self) -> None:
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

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "title": self.title,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": list(self.acceptance_criteria),
            "skills": list(self.skills),
        }
        if self.worker is not None:
            result["worker"] = self.worker
        if self.resources:
            result["resources"] = list(self.resources)
        if self.exclusive:
            result["exclusive"] = True
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


def parse_tasks(document: Any) -> tuple[Task, ...]:
    """Validate document shape; graph validation is performed by TaskGraph."""
    _object_fields(document, {"version", "tasks"}, set(), "document")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ContractError("document.version must be the integer 1")
    if not isinstance(document["tasks"], list) or not document["tasks"]:
        raise ContractError("document.tasks must be a nonempty array")

    tasks = []
    for index, value in enumerate(document["tasks"]):
        label = f"tasks[{index}]"
        _object_fields(value, _TASK_REQUIRED, _TASK_OPTIONAL, label)
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
            )
        )
    return tuple(tasks)
