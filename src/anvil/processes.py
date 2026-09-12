"""Bounded, shell-free POSIX subprocesses with persistent output artifacts.

Children remain in their own process group, which is stopped even when its leader
exits successfully. This prevents ordinary background jobs from changing a
workspace after a command has been evaluated. Process groups are lifecycle
control, not a security boundary against a program deliberately escaping them.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Sequence


class ProcessError(RuntimeError):
    """A command could not be created, contained, or read successfully."""


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    timed_out: bool


def _signal_group(group: int, sig: int) -> bool:
    try:
        os.killpg(group, sig)
        return True
    except ProcessLookupError:
        return False


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the group, then reap its leader, including orphaned background work."""
    group = process.pid  # start_new_session makes the leader its group leader.
    error = None
    try:
        if _signal_group(group, signal.SIGTERM):
            deadline = time.monotonic() + 0.2
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            # The leader may exit first. Give descendants the same grace period
            # before killing them, without probing zombie groups with signal 0.
            time.sleep(max(0, deadline - time.monotonic()))
            _signal_group(group, signal.SIGKILL)
    except OSError as exc:
        error = exc
        # Fail closed on group-control errors, but still reap the direct child.
        try:
            process.kill()
        except ProcessLookupError:
            pass
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise ProcessError("command process could not be reaped after termination") from exc
    if error is not None:
        raise ProcessError(f"could not terminate command process group: {error}") from error


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: str | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
) -> ProcessOutcome:
    """Run a literal argv with an elapsed-time limit and exclusive output files.

    The output parents must exist and the files must not. Output streams go
    directly to disk; stdin uses a temporary file so neither a full output pipe
    nor a child that ignores stdin can block supervision. A timeout returns an
    outcome; interrupts are propagated after terminating the process group.
    """
    if os.name != "posix":
        raise ProcessError("process execution currently requires macOS or Linux (POSIX)")
    if (
        isinstance(argv, (str, bytes))
        or not argv
        or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        or not argv[0]
    ):
        raise ProcessError("command must be a nonempty sequence of arguments without NUL characters")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ProcessError("command timeout must be a finite number greater than zero")
    if stdin is not None and not isinstance(stdin, str):
        raise ProcessError("command stdin must be text or None")
    cwd = Path(cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ProcessError(f"command working directory does not exist: {cwd}")
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    if stdout_path.resolve() == stderr_path.resolve():
        raise ProcessError("stdout and stderr must use different artifact files")

    with ExitStack() as files:
        try:
            stdout_file = files.enter_context(stdout_path.open("xb"))
            stderr_file = files.enter_context(stderr_path.open("xb"))
            if stdin is None:
                input_file = subprocess.DEVNULL
            else:
                input_file = files.enter_context(tempfile.TemporaryFile(mode="w+b"))
                input_file.write(stdin.encode("utf-8"))
                input_file.seek(0)
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=input_file,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError, UnicodeError) as exc:
            raise ProcessError(f"could not start command or create output artifacts: {exc}") from exc
        timed_out = False
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            # This runs on success, failure, timeout, and KeyboardInterrupt. A
            # successful leader can leave background children, so always clean up.
            _stop_group(process)
        return ProcessOutcome(returncode=process.returncode, timed_out=timed_out)
