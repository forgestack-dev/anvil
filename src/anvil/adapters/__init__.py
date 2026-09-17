"""Select a local coding agent without changing the supervisor's acceptance gates."""

from anvil.contracts import ContractError


AGENT_NAMES = ("codex", "claude-code")
# Agents usable as harness workers or reviewers. Muse turns are fulfilled by
# the operator through a staged handoff instead of a local CLI, so Muse has
# no native skill directory and stays out of AGENT_NAMES (used for skill
# installation targets).
EXECUTION_AGENTS = ("codex", "claude-code", "muse")


def create_runner(agent: str, executable: str, *, profile=None, turns=None, exclude=()):
    if agent == "codex":
        from .codex import CodexRunner
        return CodexRunner(executable, profile=profile, exclude=exclude)
    if agent == "claude-code":
        from .claude import ClaudeRunner
        return ClaudeRunner(executable, profile=profile, turns=turns, exclude=exclude)
    if agent == "muse":
        from .muse import MuseRunner
        return MuseRunner(executable, profile=profile)
    raise ContractError(f"unsupported agent: {agent}")


def probe_agent(agent: str, executable: str | None = None, *, exclude=()):
    """Probe one agent CLI, withholding the excluded variables from the probe."""
    if agent == "codex":
        from .codex import doctor
        return doctor(executable if executable is not None else "codex", probe=True,
                      exclude=exclude)
    if agent == "claude-code":
        from .claude import doctor
        return doctor(executable if executable is not None else "claude", probe=True,
                      exclude=exclude)
    if agent == "muse":
        from .muse import doctor
        return doctor(executable if executable is not None else "muse", probe=True,
                      exclude=exclude)
    raise ContractError(f"unsupported agent: {agent}")
