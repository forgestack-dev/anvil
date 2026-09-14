"""Muse operator-handoff adapter.

There is no Muse CLI to launch: the implementer or reviewer for a Muse turn
is the operator running Anvil (for example, a Muse agent session driving the
harness). Each turn stages a handoff directory holding the prompt, the result
schema, and a machine-readable request, announces it on stderr, and waits for
the operator to write `result.json`. The operator must write that file
atomically (temporary file, then rename); the runner reads it once, exactly
like the Codex adapter reads the CLI-written result.

A Muse turn's JSON result still requires the supervisor's independent
validation before any worker claim can advance a ticket. Timeouts, Ctrl-C,
and malformed results fail the turn without touching existing Codex or
Claude Code behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
import time

from anvil.processes import ProcessError


MAX_RESULT_BYTES = 4 * 1024 * 1024
_POLL_INTERVAL = 0.5


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
class MuseHandoff:
    """A staged operator request in a freshly created artifact directory."""

    prompt_path: Path
    schema_path: Path
    request_path: Path
    result_path: Path
    repo: Path
    read_only: bool
    timeout: float


@dataclass(frozen=True)
class MuseDoctor:
    """No CLI to probe; compatibility means the operator fulfills Muse turns.

    `executable` is always None: nothing is located, versioned, or launched.
    """

    executable: str | None
    version: str | None = None
    compatible: bool | None = None
    missing_flags: tuple[str, ...] = ()
    error: str | None = None


def _validate_timeout(timeout: object) -> float:
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a finite number of seconds greater than zero")
    return float(timeout)


def build_handoff(
    repo: Path,
    prompt: str,
    schema: dict,
    artifact_dir: Path,
    *,
    read_only: bool = False,
    timeout: float = 900,
) -> MuseHandoff:
    """Stage prompt, schema, and request files without waiting for the operator.

    The repository must have a .git directory or worktree file, the schema
    must be a JSON object, and the artifact directory must not exist yet.
    Nothing is launched and no model is contacted; the caller waits for
    `result.json` separately so tests can fulfill the handoff directly.
    """
    if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt:
        raise ValueError("prompt must be nonempty text without NUL characters")
    if not isinstance(schema, dict):
        raise ValueError("Muse result schema must be a JSON object")
    if not isinstance(read_only, bool):
        raise ValueError("read_only must be a boolean")
    timeout = _validate_timeout(timeout)

    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"repository directory does not exist: {repo}")
    git_marker = repo / ".git"
    if not (git_marker.is_dir() or git_marker.is_file()):
        raise ValueError(f"repository must have a .git directory or worktree file: {repo}")

    artifact_dir = Path(artifact_dir).expanduser()
    artifact_dir.mkdir(parents=True, exist_ok=False)
    artifact_dir = artifact_dir.resolve()

    prompt_path = artifact_dir / "prompt.txt"
    schema_path = artifact_dir / "schema.json"
    request_path = artifact_dir / "request.json"
    result_path = artifact_dir / "result.json"

    role = "review" if read_only else "implement"
    request = {
        "agent": "muse",
        "role": role,
        "repo": str(repo),
        "read_only": read_only,
        "prompt_file": prompt_path.name,
        "schema_file": schema_path.name,
        "result_file": result_path.name,
        "timeout_seconds": timeout,
        "protocol": (
            "The operator fulfills this turn: read prompt.txt, work in the repo "
            "worktree above, then write result.json as a JSON object matching "
            "schema.json. Write a temporary file and rename it over result.json "
            "so the runner never reads a partial write."
        ),
    }
    try:
        with prompt_path.open("x", encoding="utf-8") as stream:
            stream.write(prompt if prompt.endswith("\n") else prompt + "\n")
        with schema_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(schema, indent=2, allow_nan=False) + "\n")
        with request_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(request, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise ValueError(f"could not stage Muse handoff: {exc}") from exc

    return MuseHandoff(
        prompt_path=prompt_path,
        schema_path=schema_path,
        request_path=request_path,
        result_path=result_path,
        repo=repo,
        read_only=read_only,
        timeout=timeout,
    )


def _announce(handoff: MuseHandoff) -> None:
    role = "review the candidate in" if handoff.read_only else "implement the ticket in"
    print(
        f"Anvil Muse handoff: {role} {handoff.repo}\n"
        f"  prompt: {handoff.prompt_path}\n"
        f"  write the schema-shaped result object to: {handoff.result_path}\n"
        "  (write a temporary file, then rename it over result.json)\n"
        f"  waiting up to {handoff.timeout:g} seconds",
        file=sys.stderr,
        flush=True,
    )


def _await_result(handoff: MuseHandoff) -> None:
    deadline = time.monotonic() + handoff.timeout
    while True:
        if not handoff.result_path.is_symlink() and handoff.result_path.is_file():
            return
        if time.monotonic() >= deadline:
            raise ProcessError(
                f"Muse turn timed out after {handoff.timeout:g} seconds; "
                f"artifacts: {handoff.result_path.parent}; "
                f"the operator never wrote {handoff.result_path.name}"
            )
        time.sleep(_POLL_INTERVAL)


def _read_result(handoff: MuseHandoff) -> dict:
    artifact_dir = handoff.result_path.parent
    try:
        if handoff.result_path.is_symlink() or not handoff.result_path.is_file():
            raise ValueError("result.json must be an existing regular file, not a symlink")
        with handoff.result_path.open("rb") as stream:
            data = stream.read(MAX_RESULT_BYTES + 1)
        if len(data) > MAX_RESULT_BYTES:
            raise ValueError("result.json exceeds the 4 MiB limit")
        result = json.loads(data, object_pairs_hook=_unique_object,
                            parse_constant=_invalid_constant)
        if not isinstance(result, dict):
            raise ValueError("result.json must contain a JSON object")
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProcessError(
            f"Muse turn returned an invalid result: {exc}; artifacts: {artifact_dir}"
        ) from exc
    return result


class MuseRunner:
    """Fulfill a turn through the operator instead of a CLI subprocess.

    The configured executable is a label only; it is never located or
    launched. Execution profiles are unsupported: model and effort selection
    belong to the operator's own session. The caller owns result-schema
    validation and acceptance decisions, exactly as with the CLI adapters.
    """

    def __init__(self, muse_binary: str = "muse", *, profile=None) -> None:
        if profile is not None:
            from ..routing import validate_selection
            validate_selection("muse", profile)
        if not isinstance(muse_binary, str) or not muse_binary.strip() or "\0" in muse_binary:
            raise ValueError("muse_binary must be a nonempty label")
        self.muse_binary = muse_binary

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
        try:
            handoff = build_handoff(repo, prompt, schema, artifact_dir,
                                    read_only=read_only, timeout=timeout)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            raise ProcessError(f"could not prepare Muse handoff: {exc}") from exc
        _announce(handoff)
        _await_result(handoff)
        return _read_result(handoff)


def doctor(muse_binary: str = "muse", *, probe: bool = False, timeout: float = 5.0) -> MuseDoctor:
    """Report operator-handoff availability; there is no CLI to inspect.

    The arguments exist only for a uniform adapter signature. No binary is
    located, no version or flag probe runs, and no login, model turn,
    credential inspection, or installation is attempted.
    """
    return MuseDoctor(executable=None, compatible=True)
