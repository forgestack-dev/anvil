"""Environment isolation for commands that operate on managed worktrees."""

from __future__ import annotations

import os


def managed_environment() -> dict[str, str]:
    """Copy the host environment without inherited Git context or overrides.

    Git must discover the repository from the managed working directory. Keep
    unrelated settings unchanged, including the agent's configuration and PATH.
    Callers can add explicit Git settings after discarding inherited GIT_* keys.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
