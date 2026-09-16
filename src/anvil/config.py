"""Explicit configuration for a trusted local agent run."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from .contracts import ContractError, _identifier, _object_fields
from .planning import _reject_constant, _unique_object


def _executable_path(binary: str, agent: str, base: Path) -> str:
    if "/" in binary or "\\" in binary:
        binary_path = base / Path(binary).expanduser()
        # Claude wrappers can dispatch on their invoked symlink name.
        return str(binary_path.absolute() if agent == "claude-code" else binary_path.resolve())
    return binary


@dataclass(frozen=True)
class WorkerConfig:
    """One named worker slot; the run's selected agent remains its reviewer."""

    id: str
    agent: str = "codex"
    agent_binary: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "worker.id")
        if self.agent not in ("codex", "claude-code", "muse"):
            raise ContractError("worker.agent must be codex, claude-code, or muse")
        binary = self.agent_binary
        if binary is None:
            # The Muse adapter never launches an executable; "muse" is only a label.
            binary = ("codex" if self.agent == "codex"
                      else "claude" if self.agent == "claude-code" else "muse")
        if not isinstance(binary, str) or not binary.strip() or "\0" in binary:
            raise ContractError("worker.agent_binary must be nonempty text without NUL")
        object.__setattr__(self, "agent_binary", binary)

    @property
    def executable(self) -> str:
        return self.agent_binary

    def to_dict(self) -> dict:
        return {"id": self.id, "agent": self.agent, "agent_binary": self.executable}

    @classmethod
    def from_document(cls, value: object, *, base: Path) -> WorkerConfig:
        _object_fields(value, {"id", "agent"}, {"agent_binary"}, "worker")
        # An explicit JSON null is not the same as omitting the executable.
        if "agent_binary" in value and value["agent_binary"] is None:
            raise ContractError("worker.agent_binary must be nonempty text without NUL")
        worker = cls(value["id"], value["agent"], value.get("agent_binary"))
        return cls(worker.id, worker.agent, _executable_path(worker.executable, worker.agent, base))


@dataclass(frozen=True)
class RunConfig:
    repo: Path
    tickets: Path
    verification: tuple[tuple[str, ...], ...]
    state_dir: Path
    codex_binary: str = "codex"
    agent_timeout: float = 900
    check_timeout: float = 300
    agent: str = "codex"
    agent_binary: str | None = None
    workers: tuple[WorkerConfig, ...] = ()
    max_processes: int | None = None
    ticket_status: bool = False
    adaptive: dict | None = None
    skill_selection: dict | None = None
    agent_turns: int | None = None
    orientation: Path | None = None
    credential_exclusion: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.ticket_status) is not bool:
            raise ContractError("ticket_status must be boolean")
        if (not isinstance(self.credential_exclusion, tuple)
                or any(not isinstance(name, str) or not name or "\0" in name
                       for name in self.credential_exclusion)
                or len(set(self.credential_exclusion)) != len(self.credential_exclusion)):
            raise ContractError(
                "run configuration.credential_exclusion must be a tuple of unique nonempty names without NUL")
        if self.agent_turns is not None and (type(self.agent_turns) is not int
                                             or not 1 <= self.agent_turns <= 1000):
            raise ContractError("agent_turns must be an integer between 1 and 1000")
        if self.adaptive is not None:
            from .routing import validate_config
            validate_config(self.adaptive)
        if self.skill_selection is not None:
            value = self.skill_selection
            if not isinstance(value, dict) or set(value) - {"mode", "max_skills"}:
                raise ContractError("skill_selection must be an object with mode and optional max_skills")
            if value.get("mode") != "rules":
                raise ContractError("skill_selection.mode must be rules")
            maximum = value.get("max_skills", 2)
            if type(maximum) is not int or not 1 <= maximum <= 4:
                raise ContractError("skill_selection.max_skills must be an integer between 1 and 4")
            object.__setattr__(self, "skill_selection", {"mode": "rules", "max_skills": maximum})
        if self.agent not in ("codex", "claude-code", "muse"):
            raise ContractError("run configuration.agent must be codex, claude-code, or muse")
        if self.agent != "codex" and self.codex_binary != "codex":
            raise ContractError("codex_binary is only supported for the codex agent")
        if (self.agent_binary is not None and self.codex_binary != "codex"
                and self.agent_binary != self.codex_binary):
            raise ContractError("agent_binary conflicts with the legacy codex_binary")
        binary = self.agent_binary
        if binary is None:
            if self.agent == "codex":
                binary = self.codex_binary
            elif self.agent == "claude-code":
                binary = "claude"
            else:
                # The Muse adapter never launches an executable; "muse" is only a label.
                binary = "muse"
        if not isinstance(binary, str) or not binary.strip() or "\0" in binary:
            raise ContractError("run configuration.agent_binary must be nonempty text without NUL")
        object.__setattr__(self, "agent_binary", binary)
        if self.agent == "codex":
            object.__setattr__(self, "codex_binary", binary)
        if (not isinstance(self.workers, tuple) or len(self.workers) > 8
                or any(not isinstance(worker, WorkerConfig) for worker in self.workers)):
            raise ContractError("run configuration.workers must be a tuple of at most 8 WorkerConfig entries")
        if len({worker.id for worker in self.workers}) != len(self.workers):
            raise ContractError("run configuration.workers contains duplicate worker IDs")
        if self.workers:
            limit = len(self.workers) if self.max_processes is None else self.max_processes
            if type(limit) is not int or not 1 <= limit <= 8:
                raise ContractError("run configuration.max_processes must be an integer between 1 and 8")
            object.__setattr__(self, "max_processes", limit)
        elif self.max_processes is not None:
            raise ContractError("run configuration.max_processes requires workers")

    @property
    def executable(self) -> str:
        """Executable for the selected agent, including legacy Codex overrides."""
        return self.agent_binary

    @classmethod
    def load(cls, path: Path) -> RunConfig:
        path = Path(path).resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"),
                                  object_pairs_hook=_unique_object,
                                  parse_constant=_reject_constant)
        except (OSError, ValueError, RecursionError) as exc:
            raise ContractError(f"cannot read run configuration: {exc}") from exc
        return cls.from_document(document, base=path.parent)

    @classmethod
    def from_document(cls, value: object, *, base: Path) -> RunConfig:
        _object_fields(value, {"version", "repo", "tickets", "verification"},
                       {"state_dir", "codex_binary", "agent", "agent_binary",
                        "agent_timeout", "check_timeout", "workers", "max_processes",
                        "ticket_status", "adaptive", "skill_selection",
                        "agent_turns", "orientation", "credential_exclusion"},
                       "run configuration")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ContractError("run configuration.version must be the integer 1")

        if "adaptive" in value and value["adaptive"] is None:
            raise ContractError("adaptive must be an object, not null")
        if "skill_selection" in value and value["skill_selection"] is None:
            raise ContractError("skill_selection must be an object, not null")
        for name in ("agent_turns", "orientation"):
            if name in value and value[name] is None:
                raise ContractError(f"{name} must not be null; omit it to use the default")

        def text(name: str, default: str | None = None) -> str:
            result = value.get(name, default)
            if not isinstance(result, str) or not result.strip() or "\0" in result:
                raise ContractError(f"run configuration.{name} must be nonempty text without NUL")
            return result

        def resolve(name: str, default: str | None = None) -> Path:
            result = Path(text(name, default)).expanduser()
            return (base / result).resolve() if not result.is_absolute() else result.resolve()

        def seconds(name: str, default: int) -> float:
            result = value.get(name, default)
            if (type(result) not in (int, float) or not 0 < result <= 3600
                    or not math.isfinite(result)):
                raise ContractError(f"run configuration.{name} must be between 0 and 3600 seconds")
            return float(result)

        commands = value["verification"]
        if not isinstance(commands, list) or not commands:
            raise ContractError("verification must contain at least one command argument array")
        for command in commands:
            if (not isinstance(command, list) or not command
                    or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)):
                raise ContractError("each verification command must be a nonempty array of arguments")
        agent = value.get("agent", "codex")
        if agent not in ("codex", "claude-code", "muse"):
            raise ContractError("run configuration.agent must be codex, claude-code, or muse")
        if "codex_binary" in value and "agent_binary" in value:
            raise ContractError("use agent_binary or legacy codex_binary, not both")
        if "codex_binary" in value and agent != "codex":
            raise ContractError("codex_binary is only supported for the codex agent")
        binary_field = "codex_binary" if "codex_binary" in value else "agent_binary"
        binary = text(binary_field, "codex" if agent == "codex"
                      else "claude" if agent == "claude-code" else "muse")
        binary = _executable_path(binary, agent, base)
        workers = ()
        if "workers" in value:
            if not isinstance(value["workers"], list) or not 1 <= len(value["workers"]) <= 8:
                raise ContractError("run configuration.workers must be an array containing 1 to 8 workers")
            workers = tuple(WorkerConfig.from_document(worker, base=base) for worker in value["workers"])
        if "max_processes" in value:
            if not workers:
                raise ContractError("run configuration.max_processes requires workers")
            if type(value["max_processes"]) is not int or not 1 <= value["max_processes"] <= 8:
                raise ContractError("run configuration.max_processes must be an integer between 1 and 8")
        credential_exclusion = value.get("credential_exclusion", [])
        if (not isinstance(credential_exclusion, list)
                or any(not isinstance(name, str) or not name.strip() or "\0" in name
                       for name in credential_exclusion)):
            raise ContractError(
                "run configuration.credential_exclusion must be an array of nonempty variable names")
        return cls(
            repo=resolve("repo"), tickets=(base / Path(text("tickets")).expanduser()).parent.resolve() / Path(text("tickets")).name,
            verification=tuple(tuple(command) for command in commands),
            state_dir=resolve("state_dir", str(Path.home() / ".local/state/anvil")),
            agent=agent, agent_binary=binary, agent_timeout=seconds("agent_timeout", 900),
            check_timeout=seconds("check_timeout", 300),
            workers=workers, max_processes=value.get("max_processes"),
            ticket_status=value.get("ticket_status", False), adaptive=value.get("adaptive"),
            skill_selection=value.get("skill_selection"),
            agent_turns=value.get("agent_turns"),
            orientation=(resolve("orientation") if "orientation" in value else None),
            credential_exclusion=tuple(credential_exclusion),
        )

    def to_dict(self) -> dict:
        result = {"version": 1, "repo": str(self.repo), "tickets": str(self.tickets),
                "state_dir": str(self.state_dir), "agent": self.agent,
                "agent_binary": self.executable,
                "verification": [list(command) for command in self.verification],
                "agent_timeout": self.agent_timeout, "check_timeout": self.check_timeout}
        if self.ticket_status:
            result["ticket_status"] = True
        if self.adaptive is not None:
            result["adaptive"] = self.adaptive
        if self.skill_selection is not None:
            result["skill_selection"] = dict(self.skill_selection)
        if self.agent_turns is not None:
            result["agent_turns"] = self.agent_turns
        if self.orientation is not None:
            result["orientation"] = str(self.orientation)
        if self.credential_exclusion:
            result["credential_exclusion"] = list(self.credential_exclusion)
        if self.workers:
            result.update(workers=[worker.to_dict() for worker in self.workers],
                          max_processes=self.max_processes)
        return result
