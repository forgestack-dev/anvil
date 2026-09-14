"""Claude Code print-mode adapter with bounded tools and structured evidence.

CLI contract: https://code.claude.com/docs/en/cli-reference
Result envelope: https://code.claude.com/docs/en/agent-sdk/typescript
The worker edits files; the supervisor runs checks. Reviewer model tools only
read files. Managed hooks and authentication helpers remain trusted host code,
so this is not an operating-system sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
from tempfile import TemporaryDirectory

from anvil.environment import managed_environment
from anvil.processes import ProcessError, run_process


MINIMUM_VERSION = (2, 1, 260)
MAX_TURNS = 32
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
_REQUIRED_FLAGS = (
    "--print", "--input-format", "--output-format", "--verbose", "--json-schema",
    "--no-session-persistence", "--safe-mode", "--strict-mcp-config", "--mcp-config",
    "--disable-slash-commands", "--no-chrome", "--permission-prompts",
    "--permission-mode", "--tools", "--disallowedTools",
)


@dataclass(frozen=True)
class ClaudeInvocation:
    argv: tuple[str, ...]
    stdin: str


@dataclass(frozen=True)
class ClaudeDoctor:
    """Local CLI availability; probing checks compatibility, not model access."""

    executable: str | None
    version: str | None = None
    compatible: bool | None = None
    missing_flags: tuple[str, ...] = ()
    error: str | None = None


def _validate_binary(claude_binary: str) -> None:
    if not isinstance(claude_binary, str) or not claude_binary.strip() or "\0" in claude_binary:
        raise ValueError("claude_binary must be a nonempty executable name or path")


def _locate_executable(claude_binary: str) -> str | None:
    """Locate from the caller's directory while preserving the invoked alias."""
    executable = shutil.which(claude_binary)
    # absolute() anchors relative PATH entries without dereferencing symlinks
    # or collapsing '..' across a symlinked directory.
    return str(Path(executable).absolute()) if executable is not None else None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds the finite numeric range")
    return number


def _read_regular(path: Path, limit: int) -> bytes:
    """Bound reads and reject replaced streams, including symlinks and FIFOs."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"{path.name} must be a regular file, not a symlink")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path.name} must be a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds the {limit // (1024 * 1024)} MiB limit")
    return data


def build_invocation(
    repo: Path,
    prompt: str,
    schema: dict,
    claude_binary: str = "claude",
    *,
    read_only: bool = False,
) -> ClaudeInvocation:
    """Build a shell-free invocation; the caller must launch with cwd=repo."""
    _validate_binary(claude_binary)
    if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt:
        raise ValueError("prompt must be nonempty text without NUL characters")
    if not isinstance(read_only, bool):
        raise ValueError("read_only must be a boolean")
    if not isinstance(schema, dict):
        raise ValueError("Claude Code result schema must be a JSON object")
    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"repository directory does not exist: {repo}")
    marker = repo / ".git"
    if not (marker.is_file() or marker.is_dir()):
        raise ValueError(f"repository must have a .git directory or worktree file: {repo}")
    schema_text = json.dumps(schema, allow_nan=False, separators=(",", ":"))
    tools = "Read,Glob,Grep" if read_only else "Read,Glob,Grep,Edit,Write"
    return ClaudeInvocation(
        argv=(
            claude_binary, "--print", "--input-format", "text", "--output-format", "stream-json",
            "--verbose", "--json-schema", schema_text, "--max-turns", str(MAX_TURNS),
            "--no-session-persistence", "--safe-mode", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--no-chrome",
            "--permission-prompts", "none", "--permission-mode", "dontAsk" if read_only else "acceptEdits",
            "--tools", tools, "--disallowedTools", "Bash,PowerShell,Agent,Task,Skill,mcp__*"
            + (",Edit,Write,NotebookEdit" if read_only else ""),
        ),
        stdin=prompt,
    )


def _extract_result(data: bytes) -> dict:
    terminal = None
    for line in data.splitlines():
        event = json.loads(line, object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant, parse_float=_finite_float)
        if not isinstance(event, dict):
            raise ValueError("each stream event must be a JSON object")
        if event.get("type") == "system" and event.get("subtype") == "permission_denied":
            raise ValueError("Claude Code reported denied permissions")
        if terminal is not None:
            # Claude Code 2.1.260 can emit task metadata after its result. Parse
            # the entire stream and allow only this known informational trailer;
            # a second result or a later error must never be hidden by success.
            if event.get("type") == "system" and event.get("subtype") == "task_summary":
                continue
            raise ValueError("result must occur exactly once and may only be followed by system/task_summary events")
        if event.get("type") == "result":
            terminal = event
    if terminal is None:
        raise ValueError("stream has no terminal result")
    if terminal.get("subtype") != "success" or terminal.get("is_error") is not False:
        raise ValueError("Claude Code did not finish successfully")
    denials = terminal.get("permission_denials")
    if not isinstance(denials, list):
        raise ValueError("result must include a permission_denials array")
    if denials:
        raise ValueError("Claude Code reported denied permissions")
    result = terminal.get("structured_output")
    if not isinstance(result, dict):
        raise ValueError("result must include a structured_output JSON object")
    return result


class ClaudeRunner:
    """Return unverified schema-shaped claims from a fresh Claude Code process.

    Model controls come only from explicit profiles; authentication is inherited. The explicit tool set
    excludes shell execution and delegation; acceptance belongs to the caller.
    """

    def __init__(self, claude_binary: str = "claude", *, profile=None) -> None:
        if profile is not None:
            from ..routing import validate_selection
            validate_selection("claude-code", profile)
        self.profile = dict(profile) if profile is not None else None
        _validate_binary(claude_binary)
        self.claude_binary = claude_binary
        # Select once in the supervisor's directory, before any worktree runs.
        # Later cwd/PATH changes must not select a different worker or reviewer.
        self.executable = _locate_executable(claude_binary)

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
        if self.executable is None:
            raise ProcessError(f"could not start command: Claude Code executable was not found: {self.claude_binary}")
        try:
            repo = Path(repo).expanduser().resolve()
            invocation = build_invocation(repo, prompt, schema, self.executable, read_only=read_only)
            artifact_dir = Path(artifact_dir).expanduser()
            artifact_dir.mkdir(parents=True, exist_ok=False)
            artifact_dir = artifact_dir.resolve()
            with (artifact_dir / "schema.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(schema, indent=2, allow_nan=False) + "\n")
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ProcessError(f"could not prepare Claude Code execution: {exc}") from exc
        if self.profile is not None:
            from dataclasses import replace
            invocation = replace(invocation, argv=(*invocation.argv, "--model", self.profile["model"],
                                                   "--effort", self.profile["effort"]))
        if self.profile is not None and "max_budget_usd" in self.profile:
            invocation = replace(invocation, argv=(*invocation.argv, "--max-budget-usd", str(self.profile["max_budget_usd"])))
        environment = managed_environment()
        if self.profile is not None:
            # Claude's effort environment override takes precedence over the CLI flag.
            environment["CLAUDE_CODE_EFFORT_LEVEL"] = self.profile["effort"]
        outcome = run_process(
            invocation.argv, cwd=repo, stdin=invocation.stdin,
            stdout_path=artifact_dir / "events.jsonl", stderr_path=artifact_dir / "stderr.log",
            timeout=timeout, env=environment,
        )
        if outcome.timed_out:
            raise ProcessError(f"Claude Code execution timed out after {timeout} seconds; artifacts: {artifact_dir}")
        if outcome.returncode:
            raise ProcessError(f"Claude Code execution exited with code {outcome.returncode}; artifacts: {artifact_dir}")
        try:
            result = _extract_result(_read_regular(artifact_dir / "events.jsonl", MAX_STREAM_BYTES))
            normalized = (json.dumps(result, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(normalized) > MAX_RESULT_BYTES:
                raise ValueError("structured_output exceeds the 4 MiB limit")
            with (artifact_dir / "result.json").open("xb") as stream:
                stream.write(normalized)
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ProcessError(f"Claude Code returned an invalid result: {exc}; artifacts: {artifact_dir}") from exc
        return result


def doctor(
    claude_binary: str = "claude", *, probe: bool = False, timeout: float = 5.0
) -> ClaudeDoctor:
    """Locate Claude Code; optionally run only bounded version/help probes.

    Version 2.1.260 is the minimum inspected contract. --max-turns is deliberately
    absent from the advertised-flag check: this supported print-mode flag is
    omitted from help. Probe commands never initiate login or a model turn.
    """
    _validate_binary(claude_binary)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError("timeout must be greater than zero and at most 30 seconds")
    executable = _locate_executable(claude_binary)
    if executable is None:
        return ClaudeDoctor(None, error="Claude Code executable was not found")
    if not probe:
        return ClaudeDoctor(executable)
    version = None
    try:
        with TemporaryDirectory(prefix="anvil-claude-probe-") as temporary:
            root = Path(temporary)

            def run_probe(flag: str) -> str:
                output = root / f"{flag[2:]}.stdout"
                outcome = run_process(
                    (executable, flag), cwd=root, stdin=None,
                    stdout_path=output, stderr_path=root / f"{flag[2:]}.stderr",
                    timeout=timeout, env=managed_environment(),
                )
                if outcome.timed_out:
                    raise ProcessError(f"{flag} probe timed out")
                if outcome.returncode:
                    raise ProcessError(f"{flag} probe exited with code {outcome.returncode}")
                return _read_regular(output, 1024 * 1024).decode("utf-8")

            version = run_probe("--version").strip()[:200] or None
            match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?: \(Claude Code\))?", version or "")
            if match is None or tuple(map(int, match.groups())) < MINIMUM_VERSION:
                return ClaudeDoctor(executable, version, False, error="Claude Code 2.1.260 or newer is required")
            help_text = run_probe("--help")
    except (OSError, ValueError, UnicodeError, ProcessError) as exc:
        return ClaudeDoctor(executable, version, False, error=f"Claude Code capability probe failed: {exc}")
    missing = tuple(flag for flag in _REQUIRED_FLAGS if not re.search(re.escape(flag) + r"\b", help_text))
    return ClaudeDoctor(
        executable, version, not missing, missing,
        "Claude Code does not advertise all required flags" if missing else None,
    )
