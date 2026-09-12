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
    agent: str = "codex"
    agent_binary: str | None = None

    def __post_init__(self) -> None:
        if self.agent not in ("codex", "claude-code"):
            raise ContractError("run configuration.agent must be codex or claude-code")
        if self.agent == "claude-code" and self.codex_binary != "codex":
            raise ContractError("codex_binary is only supported for the codex agent")
        if (self.agent_binary is not None and self.codex_binary != "codex"
                and self.agent_binary != self.codex_binary):
            raise ContractError("agent_binary conflicts with the legacy codex_binary")
        binary = self.agent_binary
        if binary is None:
            binary = self.codex_binary if self.agent == "codex" else "claude"
        if not isinstance(binary, str) or not binary.strip() or "\0" in binary:
            raise ContractError("run configuration.agent_binary must be nonempty text without NUL")
        object.__setattr__(self, "agent_binary", binary)
        if self.agent == "codex":
            object.__setattr__(self, "codex_binary", binary)

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
                        "agent_timeout", "check_timeout"},
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
        agent = value.get("agent", "codex")
        if agent not in ("codex", "claude-code"):
            raise ContractError("run configuration.agent must be codex or claude-code")
        if "codex_binary" in value and "agent_binary" in value:
            raise ContractError("use agent_binary or legacy codex_binary, not both")
        if "codex_binary" in value and agent != "codex":
            raise ContractError("codex_binary is only supported for the codex agent")
        binary_field = "codex_binary" if "codex_binary" in value else "agent_binary"
        binary = text(binary_field, "codex" if agent == "codex" else "claude")
        if "/" in binary or "\\" in binary:
            binary = str((base / Path(binary).expanduser()).resolve())
        return cls(
            repo=resolve("repo"), tickets=resolve("tickets"),
            verification=tuple(tuple(command) for command in commands),
            state_dir=resolve("state_dir", str(Path.home() / ".local/state/anvil")),
            agent=agent, agent_binary=binary, agent_timeout=seconds("agent_timeout", 900),
            check_timeout=seconds("check_timeout", 300),
        )

    def to_dict(self) -> dict:
        return {"version": 1, "repo": str(self.repo), "tickets": str(self.tickets),
                "state_dir": str(self.state_dir), "agent": self.agent,
                "agent_binary": self.executable,
                "verification": [list(command) for command in self.verification],
                "agent_timeout": self.agent_timeout, "check_timeout": self.check_timeout}
