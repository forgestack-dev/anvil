"""Real local process fixtures for shared capacity and run cancellation."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from anvil.processes import ProcessCancelled, ProcessError, ProcessScope, _defer_sigint, run_process


@unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
class ParallelProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="anvil parallel process ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_command(self, name, code, *, scope=None, timeout=5):
        kwargs = dict(
            cwd=self.root, stdin=None, timeout=timeout,
            stdout_path=self.root / f"{name}.stdout",
            stderr_path=self.root / f"{name}.stderr",
        )
        argv = [sys.executable, "-c", code]
        if scope is None:
            return run_process(argv, **kwargs)
        with scope.activate():
            return run_process(argv, **kwargs)

    def await_file(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"child did not create {path.name}")

    def clean_process_group(self, process):
        # Keep fixtures contained even if an assertion fails after the OS child
        # exists but before Popen has returned ownership to run_process.
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            process.wait(timeout=3)

    def gated_command(self, name):
        return (
            "import pathlib,time; "
            f"pathlib.Path({name + '.started'!r}).write_text('started'); "
            f"exec(\"while not pathlib.Path({name + '.release'!r}).exists(): time.sleep(0.01)\"); "
            f"pathlib.Path({name + '.finished'!r}).write_text('finished')"
        )

    def test_capacity_is_shared_by_overlapping_workers_and_main_thread(self):
        scope = ProcessScope(2)
        (self.root / "main.release").touch()
        with ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(self.run_command, "first", self.gated_command("first"), scope=scope)
            second = executor.submit(self.run_command, "second", self.gated_command("second"), scope=scope)
            try:
                self.await_file(self.root / "first.started")
                self.await_file(self.root / "second.started")
                # Both workers must actually overlap before the supervisor tries
                # its own managed command; that command shares their capacity.
                self.assertFalse(first.done())
                self.assertFalse(second.done())

                def release_one_worker():
                    time.sleep(0.2)
                    try:
                        self.assertFalse((self.root / "main.started").exists())
                    finally:
                        (self.root / "first.release").touch()

                controller = executor.submit(release_one_worker)
                result = self.run_command("main", self.gated_command("main"), scope=scope)
                controller.result(timeout=3)
                self.assertEqual(result.returncode, 0)
                self.assertTrue((self.root / "first.finished").exists())
                self.assertFalse(second.done())
            finally:
                (self.root / "first.release").touch()
                (self.root / "second.release").touch()
            self.assertEqual(first.result(timeout=3).returncode, 0)
            self.assertEqual(second.result(timeout=3).returncode, 0)

    def test_cancel_stops_running_group_and_waiter_without_spawning_waiter(self):
        scope = ProcessScope(1)
        descendant = (
            "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path('descendant.ready').touch(); "
            "exec(\"while not pathlib.Path('descendant.release').exists(): time.sleep(0.01)\"); "
            "pathlib.Path('late-mutation').write_text('bad')"
        )
        leader = (
            "import subprocess,sys,signal,time,pathlib,os; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
            "pathlib.Path('leader.pid').write_text(str(os.getpid())); time.sleep(30)"
        )
        waiting = threading.Event()

        def queued_command():
            waiting.set()
            return self.run_command("queued", "from pathlib import Path; Path('queued.started').touch()", scope=scope)

        with ThreadPoolExecutor(max_workers=2) as executor:
            running = executor.submit(self.run_command, "running", leader, scope=scope)
            try:
                self.await_file(self.root / "descendant.ready")
                queued = executor.submit(queued_command)
                self.assertTrue(waiting.wait(timeout=3))
                time.sleep(0.1)
                self.assertFalse(queued.done())
                started = time.monotonic()
                scope.cancel()
                for future in (running, queued):
                    with self.assertRaises(ProcessCancelled):
                        future.result(timeout=3)
                self.assertLess(time.monotonic() - started, 3)
            finally:
                scope.cancel()
        self.assertTrue(scope.cancelled)
        self.assertFalse((self.root / "queued.started").exists())
        self.assertFalse((self.root / "queued.stdout").exists())
        (self.root / "descendant.release").touch()
        time.sleep(0.2)
        self.assertFalse((self.root / "late-mutation").exists())
        with self.assertRaises(ChildProcessError):
            os.waitpid(int((self.root / "leader.pid").read_text()), os.WNOHANG)

    def test_cancel_during_creation_reaps_child_before_propagating(self):
        scope = ProcessScope(1)
        original_execute = subprocess.Popen._execute_child
        spawned = []
        code = (
            "import pathlib,time,signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path('child.ready').touch(); time.sleep(30)"
        )

        def cancel_after_spawn(process, *args, **kwargs):
            original_execute(process, *args, **kwargs)
            spawned.append(process)
            self.addCleanup(self.clean_process_group, process)
            self.await_file(self.root / "child.ready")
            scope.cancel()

        with patch.object(subprocess.Popen, "_execute_child", cancel_after_spawn):
            with self.assertRaises(ProcessCancelled):
                self.run_command("creation", code, scope=scope)
        with self.assertRaises(ChildProcessError):
            os.waitpid(spawned[0].pid, os.WNOHANG)
        self.assertEqual(spawned[0].returncode, -signal.SIGKILL)

    def test_scope_activation_restores_previous_scope_and_ordinary_execution(self):
        cancelled = ProcessScope(1)
        cancelled.cancel()
        live = ProcessScope(1)
        with cancelled.activate():
            with live.activate():
                self.assertEqual(self.run_command("nested", "pass").returncode, 0)
            with self.assertRaises(ProcessCancelled):
                self.run_command("cancelled", "raise AssertionError('must not spawn')")
        self.assertFalse((self.root / "cancelled.stdout").exists())
        self.assertEqual(self.run_command("ordinary", "pass").returncode, 0)

    def test_timeout_and_creation_failure_release_capacity_for_next_command(self):
        scope = ProcessScope(1)
        with scope.activate():
            outcome = self.run_command("timeout", "import time; time.sleep(30)", timeout=0.05)
            self.assertTrue(outcome.timed_out)
            with self.assertRaisesRegex(ProcessError, "could not start command"):
                run_process(
                    [str(self.root / "missing")], cwd=self.root, stdin=None, timeout=3,
                    stdout_path=self.root / "missing.stdout", stderr_path=self.root / "missing.stderr",
                )
            self.assertEqual(self.run_command("after", "pass").returncode, 0)

    def test_repeated_sigint_can_be_deferred_until_cancelled_workers_join(self):
        scope = ProcessScope(2)
        original_handler = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, original_handler)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        code = (
            "import pathlib,time,signal,os; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path(str(os.getpid()) + '.ready').touch(); time.sleep(30)"
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.run_command, str(index), code, scope=scope) for index in range(2)]
            try:
                deadline = time.monotonic() + 3
                while len(list(self.root.glob("*.ready"))) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(list(self.root.glob("*.ready"))), 2)
                with self.assertRaises(KeyboardInterrupt):
                    with _defer_sigint() as checkpoint:
                        try:
                            os.kill(os.getpid(), signal.SIGINT)
                            checkpoint()
                        except KeyboardInterrupt:
                            scope.cancel()
                            # Repeated Ctrl-C while joining remains pending until
                            # every managed process has completed group cleanup.
                            os.kill(os.getpid(), signal.SIGINT)
                            os.kill(os.getpid(), signal.SIGINT)
                            for future in futures:
                                with self.assertRaises(ProcessCancelled):
                                    future.result(timeout=3)
                            for ready in self.root.glob("*.ready"):
                                with self.assertRaises(ChildProcessError):
                                    os.waitpid(int(ready.stem), os.WNOHANG)
            finally:
                scope.cancel()
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_invalid_capacity_is_rejected(self):
        for capacity in (0, -1, True, 1.5, "2", None):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                ProcessScope(capacity)


if __name__ == "__main__":
    unittest.main()
