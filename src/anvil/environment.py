"""Environment isolation for commands that operate on managed worktrees."""

from __future__ import annotations

import os
from collections.abc import Iterable


def managed_environment(exclude: Iterable[str] | None = None) -> dict[str, str]:
    """Copy the host environment without inherited Git context or overrides.

    Git must discover the repository from the managed working directory. Keep
    unrelated settings unchanged, including the agent's configuration and PATH.
    Callers can add explicit Git settings after discarding inherited GIT_* keys.

    `exclude` names additional variables to omit, such as credentials a caller
    must withhold from a child process. Matching is case-sensitive and an
    absent or empty set changes nothing about the result.
    """
    excluded = set(exclude) if exclude else set()
    return {
        key: value for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in excluded
    }
