"""Explicit configuration for a trusted local serial run."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from .contracts import ContractError, _object_fields
from .planning import _reject_constant, _unique_object


@dataclass(frozen=True)
class RunConfig:
    repo: Path
    tickets: Path
    verification: tuple[tuple[str, ...], ...]
    state_dir: Path
    codex_binary: str = "codex"
    agent_timeout: float = 900
    check_timeout: float = 300

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
                       {"state_dir", "codex_binary", "agent_timeout", "check_timeout"},
                       "run configuration")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ContractError("run configuration.version must be the integer 1")

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
        binary = text("codex_binary", "codex")
        if "/" in binary or "\\" in binary:
            binary = str((base / Path(binary).expanduser()).resolve())
        return cls(
            repo=resolve("repo"), tickets=resolve("tickets"),
            verification=tuple(tuple(command) for command in commands),
            state_dir=resolve("state_dir", str(Path.home() / ".local/state/anvil")),
            codex_binary=binary, agent_timeout=seconds("agent_timeout", 900),
            check_timeout=seconds("check_timeout", 300),
        )

    def to_dict(self) -> dict:
        return {"version": 1, "repo": str(self.repo), "tickets": str(self.tickets),
                "state_dir": str(self.state_dir), "codex_binary": self.codex_binary,
                "verification": [list(command) for command in self.verification],
                "agent_timeout": self.agent_timeout, "check_timeout": self.check_timeout}
