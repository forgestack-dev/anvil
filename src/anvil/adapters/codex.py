"""Prepare Codex commands and inspect local CLI capabilities without agent runs.

Flag contract: https://learn.chatgpt.com/docs/non-interactive-mode
The invocation builder never launches a process or creates output files. A future
executor must recheck paths, enforce time limits, capture events, and validate the
result independently before accepting a worker's claim of completion.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess


@dataclass(frozen=True)
class CodexInvocation:
    """Arguments for a future shell=False process, with the full prompt on stdin."""

    argv: tuple[str, ...]
    stdin: str


@dataclass(frozen=True)
class CodexDoctor:
    """Local CLI availability; compatible=None means the CLI was not probed.

    Compatibility checks only advertised flags, never credentials or model access.
    """

    executable: str | None
    version: str | None = None
    compatible: bool | None = None
    missing_flags: tuple[str, ...] = ()
    error: str | None = None


def _validate_binary(codex_binary: str) -> None:
    if not isinstance(codex_binary, str) or not codex_binary.strip() or "\0" in codex_binary:
        raise ValueError("codex_binary must be a nonempty executable name or path")


def build_invocation(
    repo: Path,
    prompt: str,
    result_schema: Path,
    result_file: Path,
    codex_binary: str = "codex",
) -> CodexInvocation:
    """Build a workspace-write command without running Codex.

    Relative paths use the caller's current directory. The repository must have a
    .git directory or worktree file, the schema must be a JSON object, and the
    result path must not exist (including a dangling symlink). Its parent must
    already exist. These checks do not prove Git validity, schema compatibility,
    runtime writability, or protect against paths changing before later execution.
    """
    _validate_binary(codex_binary)
    if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt:
        raise ValueError("prompt must be nonempty text without NUL characters")

    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"repository directory does not exist: {repo}")
    git_marker = repo / ".git"
    if not (git_marker.is_dir() or git_marker.is_file()):
        raise ValueError(f"repository must have a .git directory or worktree file: {repo}")

    result_schema = Path(result_schema).expanduser().resolve()
    if not result_schema.is_file():
        raise ValueError(f"result schema must be an existing file: {result_schema}")
    try:
        schema = json.loads(result_schema.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"result schema must contain readable JSON: {result_schema}") from exc
    if not isinstance(schema, dict):
        raise ValueError("result schema must contain a JSON object")

    requested_result = Path(result_file).expanduser()
    if requested_result.exists() or requested_result.is_symlink():
        raise ValueError(f"result path already exists: {requested_result}")
    result_file = requested_result.resolve()
    if not result_file.parent.is_dir():
        raise ValueError(f"result parent directory does not exist: {result_file.parent}")

    return CodexInvocation(
        argv=(
            codex_binary,
            "exec",
            "--sandbox",
            "workspace-write",
            "-C",
            str(repo),
            "--json",
            "--output-schema",
            str(result_schema),
            "-o",
            str(result_file),
            "-",
        ),
        stdin=prompt,
    )


def doctor(
    codex_binary: str = "codex", *, probe: bool = False, timeout: float = 5.0
) -> CodexDoctor:
    """Locate Codex, optionally checking --version and exec --help.

    Each probe has its own timeout, limited to 30 seconds. No login, agent turn,
    credential inspection, or installation is attempted. This executes the local
    binary only when probe=True; a binary path must come from trusted configuration.
    """
    _validate_binary(codex_binary)
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("timeout must be greater than zero and at most 30 seconds")
    executable = shutil.which(codex_binary)
    if executable is None:
        return CodexDoctor(executable=None, error="Codex executable was not found")
    if not probe:
        return CodexDoctor(executable=executable)

    version: str | None = None
    try:
        version_result = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        if version_result.returncode:
            return CodexDoctor(
                executable=executable,
                compatible=False,
                error=f"Codex version probe exited with code {version_result.returncode}",
            )
        version = version_result.stdout.strip()[:200] or None
        help_result = subprocess.run(
            [executable, "exec", "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
        if help_result.returncode:
            return CodexDoctor(
                executable=executable,
                version=version,
                compatible=False,
                error=f"Codex help probe exited with code {help_result.returncode}",
            )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        return CodexDoctor(
            executable=executable,
            version=version,
            compatible=False,
            error=f"Codex capability probe failed: {type(exc).__name__}",
        )

    required = (
        "--sandbox", "workspace-write", "--cd", "--json",
        "--output-schema", "--output-last-message",
    )
    missing = tuple(flag for flag in required if flag not in help_result.stdout)
    return CodexDoctor(
        executable=executable,
        version=version,
        compatible=not missing,
        missing_flags=missing,
        error="Codex exec does not advertise all required flags" if missing else None,
    )
