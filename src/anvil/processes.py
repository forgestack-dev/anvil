"""Bounded, shell-free POSIX subprocesses with persistent output artifacts.

Children remain in their own process group, which is stopped even when its leader
exits successfully. This prevents ordinary background jobs from changing a
workspace after a command has been evaluated. Process groups are lifecycle
control, not a security boundary against a program deliberately escaping them.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
import math
import uuid
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Sequence


class ProcessError(RuntimeError):
    """A command could not be created, contained, or read successfully."""


class ProcessCancelled(ProcessError):
    """The owning run cancelled a queued or executing command."""


class InvocationExhausted(ProcessError):
    """An agent invocation hit a configured ceiling before finishing.

    category is "turn_exhaustion" or "budget_exhaustion", the store.py
    failure categories this classification exists to reach. Raised only
    when an adapter's own result envelope names the ceiling; an ordinary
    nonzero exit, a timeout, or a stream the adapter cannot parse stays a
    plain ProcessError.
    """

    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.category = category


_active_scope: ContextVar[ProcessScope | None] = ContextVar("anvil_process_scope", default=None)


class ProcessScope:
    """Share command capacity and cancellation across a run's calling threads.

    Each supervisor or executor thread must enter ``activate()`` explicitly.
    Capacity includes the entire process group until cleanup has completed.
    Cancellation is permanent; a new run needs a new scope.
    """

    def __init__(self, max_processes: int):
        if isinstance(max_processes, bool) or not isinstance(max_processes, int) or max_processes < 1:
            raise ValueError("max_processes must be a positive integer")
        self.max_processes = max_processes
        self._slots = threading.BoundedSemaphore(max_processes)
        self._cancelled = threading.Event()
        self.command_dir = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    @contextmanager
    def activate(self):
        token = _active_scope.set(self)
        try:
            yield self
        finally:
            _active_scope.reset(token)

    def _check_cancelled(self) -> None:
        if self.cancelled:
            raise ProcessCancelled("command cancelled by its owning run")

    @contextmanager
    def _slot(self, check_interrupt):
        # run_process defers main-thread SIGINT across acquisition and release,
        # so a signal cannot lose ownership immediately after acquire succeeds.
        while True:
            check_interrupt()
            self._check_cancelled()
            if self._slots.acquire(timeout=0.1):
                break
        try:
            self._check_cancelled()
            yield
        finally:
            self._slots.release()


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    timed_out: bool


@contextmanager
def _defer_sigint():
    """Deliver Ctrl-C at checkpoints or after cleanup, never inside ownership changes."""
    handler = signal.getsignal(signal.SIGINT)
    pending = None

    def remember(signum, frame):
        nonlocal pending
        pending = (signum, frame)

    def checkpoint():
        nonlocal pending
        if pending is not None:
            received, pending = pending, None
            handler(*received)

    # Python dispatches signal handlers only on the main thread. Preserve callers'
    # explicit ignored/default dispositions, including their inheritance by exec.
    if threading.current_thread() is not threading.main_thread() or not callable(handler):
        yield checkpoint
        return
    signal.signal(signal.SIGINT, remember)
    try:
        yield checkpoint
    finally:
        signal.signal(signal.SIGINT, handler)
        checkpoint()


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
    env: Mapping[str, str],
) -> ProcessOutcome:
    """Run a literal argv with an elapsed-time limit and exclusive output files.

    The output parents must exist and the files must not. Output streams go
    directly to disk; stdin uses a temporary file so neither a full output pipe
    nor a child that ignores stdin can block supervision. A timeout returns an
    outcome; interrupts are propagated after terminating the process group.
    env is required and replaces the inherited environment without merging it:
    a launch that omits it is a launch nothing withheld a credential from, so
    the omission is a type error rather than a silent inheritance. Callers build
    it with environment.managed_environment(), passing the run's configured
    credential_exclusion where one exists.
    An active ProcessScope also bounds concurrent groups and allows the owning
    run to cancel queued or executing commands. Queue time is outside timeout.
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
    if env is not None and (
        not isinstance(env, Mapping)
        or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
               or not isinstance(value, str) or "\0" in value for key, value in env.items())
    ):
        raise ProcessError("command environment must map valid variable names to text without NUL")
    cwd = Path(cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ProcessError(f"command working directory does not exist: {cwd}")
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    if stdout_path.resolve() == stderr_path.resolve():
        raise ProcessError("stdout and stderr must use different artifact files")

    scope = _active_scope.get()

    def check_cancelled():
        if scope is not None:
            scope._check_cancelled()

    # Keep the guard installed across capacity acquisition, spawn, waiting,
    # release, and cleanup. Children must not inherit a blocked SIGINT mask.
    with _defer_sigint() as check_interrupt, ExitStack() as files:
        if scope is not None:
            files.enter_context(scope._slot(check_interrupt))
        check_interrupt()
        check_cancelled()
        try:
            stdout_file = files.enter_context(stdout_path.open("xb"))
            stderr_file = files.enter_context(stderr_path.open("xb"))
            if stdin is None:
                input_file = subprocess.DEVNULL
            else:
                input_file = files.enter_context(tempfile.TemporaryFile(mode="w+b"))
                input_file.write(stdin.encode("utf-8"))
                input_file.seek(0)
        except (OSError, ValueError, UnicodeError) as exc:
            raise ProcessError(f"could not start command or create output artifacts: {exc}") from exc
        process = None
        record = None
        if scope is not None and scope.command_dir is not None:
            from .ticket_status import atomic, encoded
            record = scope.command_dir / (uuid.uuid4().hex + ".json")
            atomic(record, encoded({"phase": "spawning", "cwd": str(cwd)}))
        timed_out = False
        try:
            check_interrupt()
            check_cancelled()
            try:
                process = subprocess.Popen(
                    list(argv),
                    cwd=cwd,
                    stdin=input_file,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=True,
                    env=env,
                )
            except (OSError, ValueError, UnicodeError) as exc:
                raise ProcessError(f"could not start command or create output artifacts: {exc}") from exc
            if record is not None:
                atomic(record, encoded({"phase": "running", "pgid": process.pid, "cwd": str(cwd)}))
            deadline = time.monotonic() + timeout
            while True:
                check_interrupt()
                check_cancelled()
                try:
                    process.wait(timeout=min(0.1, max(0, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
        finally:
            # A successful leader can leave descendants; always stop its
            # group before propagating any interrupts received during cleanup.
            if process is not None:
                _stop_group(process)
            if record is not None:
                record.unlink()
        check_cancelled()
        return ProcessOutcome(returncode=process.returncode, timed_out=timed_out)
