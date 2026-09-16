"""Prepare and execute Codex commands without granting extra permissions.

Flag contract: https://learn.chatgpt.com/docs/non-interactive-mode
The invocation builder remains side-effect free. CodexRunner bounds execution
and records output, but its JSON result still requires independent verification
before accepting a worker's claim of completion.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import subprocess

from anvil.environment import managed_environment
from anvil.processes import ProcessError, run_process


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


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
    sandbox: str = "workspace-write",
) -> CodexInvocation:
    """Build a workspace-write or read-only command without running Codex.

    Relative paths use the caller's current directory. The repository must have a
    .git directory or worktree file, the schema must be a JSON object, and the
    result path must not exist (including a dangling symlink). Its parent must
    already exist. These checks do not prove Git validity, schema compatibility,
    runtime writability, or protect against paths changing before later execution.
    """
    _validate_binary(codex_binary)
    if sandbox not in ("workspace-write", "read-only"):
        raise ValueError("sandbox must be workspace-write or read-only")
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
            sandbox,
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


class CodexRunner:
    """Launch Codex with fresh artifacts; return an unverified JSON object.

    The caller owns result-schema validation and acceptance decisions. The
    adapter applies only explicitly configured profiles, never enables network access, or bypasses sandbox
    or approval policy. Its executable must come from trusted configuration.
    """

    def __init__(self, codex_binary: str = "codex", *, profile=None, exclude=()) -> None:
        self.exclude = tuple(exclude)
        if profile is not None:
            from ..routing import validate_selection
            validate_selection("codex", profile)
        self.profile = dict(profile) if profile is not None else None
        _validate_binary(codex_binary)
        self.codex_binary = codex_binary

    def run(
        self,
        *,
        repo: Path,
        prompt: str,
        schema: dict,
        artifact_dir: Path,
        timeout: float,
        read_only: bool = False,
    ) -> dict:
        if not isinstance(schema, dict):
            raise ProcessError("Codex result schema must be a JSON object")
        if not isinstance(read_only, bool):
            raise ProcessError("read_only must be a boolean")
        artifact_dir = Path(artifact_dir).expanduser()
        try:
            schema_text = json.dumps(schema, indent=2, allow_nan=False) + "\n"
            artifact_dir.mkdir(parents=True, exist_ok=False)
            artifact_dir = artifact_dir.resolve()
            schema_path = artifact_dir / "schema.json"
            with schema_path.open("x", encoding="utf-8") as stream:
                stream.write(schema_text)
            result_path = artifact_dir / "result.json"
            invocation = build_invocation(
                repo=repo,
                prompt=prompt,
                result_schema=schema_path,
                result_file=result_path,
                codex_binary=self.codex_binary,
                sandbox="read-only" if read_only else "workspace-write",
            )
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            raise ProcessError(f"could not prepare Codex execution: {exc}") from exc
        if self.profile is not None:
            from dataclasses import replace
            invocation = replace(invocation, argv=(*invocation.argv[:-1], "--model", self.profile["model"],
                "--config", 'model_reasoning_effort="' + self.profile["effort"] + '"', "-"))
        outcome = run_process(
            invocation.argv,
            cwd=repo,
            stdin=invocation.stdin,
            stdout_path=artifact_dir / "events.jsonl",
            stderr_path=artifact_dir / "stderr.log",
            timeout=timeout,
            env=managed_environment(self.exclude),
        )
        if outcome.timed_out:
            raise ProcessError(f"Codex execution timed out after {timeout} seconds; artifacts: {artifact_dir}")
        if outcome.returncode:
            raise ProcessError(f"Codex execution exited with code {outcome.returncode}; artifacts: {artifact_dir}")
        try:
            if result_path.is_symlink() or not result_path.is_file():
                raise ValueError("result.json must be an existing regular file, not a symlink")
            with result_path.open("rb") as stream:
                data = stream.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("result.json exceeds the 4 MiB limit")
            result = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if not isinstance(result, dict):
                raise ValueError("result.json must contain a JSON object")
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise ProcessError(f"Codex returned an invalid result: {exc}; artifacts: {artifact_dir}") from exc
        return result


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
