"""Select a local coding agent without changing the supervisor's acceptance gates."""

from anvil.contracts import ContractError


AGENT_NAMES = ("codex", "claude-code")


def create_runner(agent: str, executable: str, *, profile=None):
    if agent == "codex":
        from .codex import CodexRunner
        return CodexRunner(executable, profile=profile)
    if agent == "claude-code":
        from .claude import ClaudeRunner
        return ClaudeRunner(executable, profile=profile)
    raise ContractError(f"unsupported agent: {agent}")


def probe_agent(agent: str, executable: str | None = None):
    if agent == "codex":
        from .codex import doctor
        return doctor(executable if executable is not None else "codex", probe=True)
    if agent == "claude-code":
        from .claude import doctor
        return doctor(executable if executable is not None else "claude", probe=True)
    raise ContractError(f"unsupported agent: {agent}")
