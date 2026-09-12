"""Dry-run dependency planning; this module never starts workers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from os import PathLike
from pathlib import Path
from typing import Any

from .contracts import ContractError, Task, parse_tasks


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError(f"invalid JSON constant: {value}")


@dataclass(frozen=True, slots=True)
class TaskGraph:
    """An acyclic task graph with stable waves of potential concurrency."""

    tasks: tuple[Task, ...]
    waves: tuple[tuple[Task, ...], ...] = field(init=False)

    def __post_init__(self) -> None:
        if not self.tasks:
            raise ContractError("task graph must contain at least one task")
        by_id: dict[str, Task] = {}
        for task in self.tasks:
            if task.id in by_id:
                raise ContractError(f"duplicate task ID: {task.id}")
            by_id[task.id] = task
        for task in self.tasks:
            for dependency in task.depends_on:
                if dependency == task.id:
                    raise ContractError(f"task {task.id} depends on itself")
                if dependency not in by_id:
                    raise ContractError(f"task {task.id} has missing dependency: {dependency}")

        completed: set[str] = set()
        remaining = list(self.tasks)
        waves: list[tuple[Task, ...]] = []
        while remaining:
            ready = tuple(task for task in remaining if set(task.depends_on) <= completed)
            if not ready:
                blocked = ", ".join(task.id for task in remaining)
                raise ContractError(f"dependency cycle prevents scheduling: {blocked}")
            waves.append(ready)
            completed.update(task.id for task in ready)
            remaining = [task for task in remaining if task.id not in completed]
        object.__setattr__(self, "waves", tuple(waves))

    @classmethod
    def from_document(cls, document: Any) -> TaskGraph:
        return cls(parse_tasks(document))

    @classmethod
    def load(cls, path: str | PathLike[str]) -> TaskGraph:
        """Read strict JSON, rejecting ambiguous duplicate fields."""
        input_path = Path(path)
        try:
            raw = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ContractError(f"cannot read ticket document {input_path}: {exc}") from exc
        try:
            document = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
        except ContractError:
            raise
        except json.JSONDecodeError as exc:
            raise ContractError(
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        except (ValueError, RecursionError) as exc:
            raise ContractError(f"invalid JSON: {exc}") from exc
        return cls.from_document(document)

    def to_dict(self) -> dict[str, Any]:
        """Return a preview only; tasks are never marked started or completed."""
        return {
            "version": 1,
            "mode": "dry-run",
            "task_count": len(self.tasks),
            "wave_count": len(self.waves),
            "max_wave_size": max(len(wave) for wave in self.waves),
            "waves": [
                {"number": number, "tasks": [task.to_dict() for task in wave]}
                for number, wave in enumerate(self.waves, start=1)
            ],
        }
