"""Environment isolation for commands that operate on managed worktrees."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable


MAX_EXCLUSIONS = 64
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def exclusion_set(names: object) -> tuple[str, ...]:
    """Validate the variable names a caller withholds from child processes.

    Accepts a list or tuple of portable environment variable names, rejects
    duplicates, and returns them in the configured order. These are names a
    user configures, never the secret values, so a run may record them.
    """
    if isinstance(names, (str, bytes)) or not isinstance(names, (list, tuple)):
        raise ValueError("credential_exclusion must be an array of environment variable names")
    result: list[str] = []
    for name in names:
        if not isinstance(name, str) or _VARIABLE_NAME.fullmatch(name) is None:
            raise ValueError(
                "each credential_exclusion entry must be an environment variable name "
                "of letters, digits, and underscores, not starting with a digit")
        if name in result:
            raise ValueError(f"credential_exclusion contains a duplicate name: {name}")
        result.append(name)
    if len(result) > MAX_EXCLUSIONS:
        raise ValueError(f"credential_exclusion must name at most {MAX_EXCLUSIONS} variables")
    return tuple(result)


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
