"""Exercise real local processes without using model workers or the network."""

import os
import signal
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from anvil.environment import managed_environment
from anvil.processes import ProcessError, run_process


@unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="anvil process ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original_sigint = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, self.original_sigint)

    def run_command(self, code, *, stdin=None, timeout=3, args=()):
        return run_process(
            [sys.executable, "-c", code, *args], cwd=self.root,
            stdin=stdin, stdout_path=self.root / "stdout.log",
            stderr_path=self.root / "stderr.log", timeout=timeout,
            env=managed_environment(),
        )

    def test_literal_arguments_and_large_streams_do_not_deadlock(self):
        prompt = 'literal $(touch injected) `echo nope`\n"quotes"' * 10000
        argument = "one argument; $(touch injected)"
        outcome = self.run_command(
            "import sys; print(sys.argv[1]); "
            "sys.stderr.write('x' * 300000); "
            "sys.stdout.write(sys.stdin.read())",
            stdin=prompt, args=(argument,),
        )
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.timed_out)
        self.assertEqual((self.root / "stdout.log").read_text(), argument + "\n" + prompt)
        self.assertEqual((self.root / "stderr.log").stat().st_size, 300000)
        self.assertFalse((self.root / "injected").exists())

    def test_nonzero_exit_is_returned_and_none_stdin_is_eof(self):
        outcome = self.run_command("import sys; assert sys.stdin.read() == ''; sys.exit(7)")
        self.assertEqual(outcome.returncode, 7)
        self.assertFalse(outcome.timed_out)

    def test_an_omitted_environment_is_a_type_error_rather_than_inheritance(self):
        # Popen(env=None) inherits everything, so a default here would make a
        # forgotten argument leak silently. The caller must name the environment.
        with self.assertRaises(TypeError):
            run_process(
                [sys.executable, "-c", "pass"], cwd=self.root, stdin=None,
                stdout_path=self.root / "omitted.stdout",
                stderr_path=self.root / "omitted.stderr", timeout=3,
            )
        self.assertFalse((self.root / "omitted.stdout").exists())

    def test_explicit_environment_is_literal_and_does_not_merge_parent_values(self):
        literal = 'spaces; $(touch injected)\n"quoted"'
        with patch.dict(os.environ, {"ANVIL_SHOULD_NOT_LEAK": "parent"}):
            outcome = run_process(
                [sys.executable, "-c", "import os; print(os.environ['ANVIL_LITERAL_VALUE']); "
                 "assert 'ANVIL_SHOULD_NOT_LEAK' not in os.environ"],
                cwd=self.root, stdin=None, stdout_path=self.root / "stdout.log",
                stderr_path=self.root / "stderr.log", timeout=3,
                env={"ANVIL_LITERAL_VALUE": literal},
            )
        self.assertEqual(outcome.returncode, 0)
        self.assertEqual((self.root / "stdout.log").read_text(), literal + "\n")
        self.assertFalse((self.root / "injected").exists())

    def test_timeout_also_bounds_a_child_that_never_reads_stdin(self):
        started = time.monotonic()
        outcome = self.run_command("import time; time.sleep(30)", stdin="x" * 2000000, timeout=0.1)
        self.assertTrue(outcome.timed_out)
        self.assertLess(time.monotonic() - started, 3)

    def background_code(self, *, leader_exits):
        # Both leader and descendant ignore TERM so the escalation is exercised.
        child = (
            "import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(1.0); pathlib.Path('late-mutation').write_text('bad')"
        )
        return (
            "import subprocess,sys,signal,time,pathlib,os; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"child=subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "pathlib.Path('leader.pid').write_text(str(os.getpid())); "
            "pathlib.Path('child.pid').write_text(str(child.pid)); "
            + ("sys.exit(0)" if leader_exits else "time.sleep(30)")
        )

    def assert_children_stopped(self):
        time.sleep(1.05)
        self.assertFalse((self.root / "late-mutation").exists())
        # The direct child must have been reaped by the supervisor.
        with self.assertRaises(ChildProcessError):
            os.waitpid(int((self.root / "leader.pid").read_text()), os.WNOHANG)

    def test_timeout_kills_descendants_ignoring_termination(self):
        outcome = self.run_command(self.background_code(leader_exits=False), timeout=0.2)
        self.assertTrue(outcome.timed_out)
        self.assert_children_stopped()

    def test_success_also_stops_background_descendants(self):
        outcome = self.run_command(self.background_code(leader_exits=True))
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.timed_out)
        self.assert_children_stopped()

    def test_keyboard_interrupt_cleans_up_and_propagates(self):
        original_wait = subprocess.Popen.wait
        interrupted = False

        def interrupt_wait(process, *args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                deadline = time.monotonic() + 3
                while not (self.root / "child.pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise KeyboardInterrupt
            return original_wait(process, *args, **kwargs)

        with patch.object(subprocess.Popen, "wait", interrupt_wait):
            with self.assertRaises(KeyboardInterrupt):
                self.run_command(self.background_code(leader_exits=False))
        self.assert_children_stopped()

    def await_file(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"child did not create {path.name}")

    def clean_process_group(self, process):
        # Reap even on a regression, so a failed lifecycle assertion cannot leave
        # real fixture children running after the temporary directory disappears.
        process.poll()
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            process.wait(timeout=3)

    def waiting_child_code(self):
        return (
            "import signal,time,pathlib; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "pathlib.Path('child.ready').write_text('ready'); "
            "exec(\"while not pathlib.Path('child.release').exists(): time.sleep(0.01)\"); "
            "time.sleep(0.1); pathlib.Path('late-mutation').write_text('bad')"
        )

    def assert_waiting_child_stopped(self, process):
        (self.root / "child.release").write_text("release")
        time.sleep(0.3)
        self.assertFalse((self.root / "late-mutation").exists())
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    def test_sigint_during_creation_stops_child_before_propagating(self):
        signal.signal(signal.SIGINT, signal.default_int_handler)
        original_execute = subprocess.Popen._execute_child
        processes = []

        def interrupt_after_spawn(process, *args, **kwargs):
            original_execute(process, *args, **kwargs)
            processes.append(process)
            self.addCleanup(self.clean_process_group, process)
            self.await_file(self.root / "child.ready")
            # The OS child exists, but Popen has not returned its handle yet.
            os.kill(os.getpid(), signal.SIGINT)

        with patch.object(subprocess.Popen, "_execute_child", interrupt_after_spawn):
            with self.assertRaises(KeyboardInterrupt):
                self.run_command(self.waiting_child_code())
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assert_waiting_child_stopped(processes[0])

    def test_repeated_sigint_during_cleanup_still_kills_orphaned_descendant(self):
        signal.signal(signal.SIGINT, signal.default_int_handler)
        original_execute = subprocess.Popen._execute_child
        processes = []
        interrupts = 0

        def capture_spawn(process, *args, **kwargs):
            original_execute(process, *args, **kwargs)
            processes.append(process)
            self.addCleanup(self.clean_process_group, process)

        def interrupt_grace_period(duration):
            nonlocal interrupts
            for _ in range(2):
                interrupts += 1
                os.kill(os.getpid(), signal.SIGINT)
            time.sleep(duration)

        leader = (
            "import subprocess,sys,pathlib,time; "
            f"subprocess.Popen([sys.executable, '-c', {self.waiting_child_code()!r}]); "
            "exec(\"while not pathlib.Path('child.ready').exists(): time.sleep(0.01)\"); "
            "sys.exit(0)"
        )
        # Replace the module reference rather than the global time.sleep, so
        # synchronization and subprocess internals retain their real clock.
        clock = SimpleNamespace(monotonic=time.monotonic, sleep=interrupt_grace_period)
        with patch.object(subprocess.Popen, "_execute_child", capture_spawn), patch("anvil.processes.time", clock):
            with self.assertRaises(KeyboardInterrupt):
                self.run_command(leader)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assert_waiting_child_stopped(processes[0])
        self.assertEqual(interrupts, 2)

    def test_custom_sigint_handler_can_return_and_allow_command_to_complete(self):
        received = []
        processes = []
        original_execute = subprocess.Popen._execute_child

        def custom_handler(signum, frame):
            received.append(signum)
            (self.root / "child.release").write_text("release")

        def interrupt_after_spawn(process, *args, **kwargs):
            original_execute(process, *args, **kwargs)
            processes.append(process)
            self.addCleanup(self.clean_process_group, process)
            self.await_file(self.root / "child.ready")
            os.kill(os.getpid(), signal.SIGINT)

        signal.signal(signal.SIGINT, custom_handler)
        code = self.waiting_child_code().replace("'late-mutation'", "'completed'")
        with patch.object(subprocess.Popen, "_execute_child", interrupt_after_spawn):
            outcome = self.run_command(code)
        self.assertEqual(received, [signal.SIGINT])
        self.assertEqual(outcome.returncode, 0)
        self.assertFalse(outcome.timed_out)
        self.assertTrue((self.root / "completed").exists())
        self.assertEqual(signal.getsignal(signal.SIGINT), custom_handler)
        with self.assertRaises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    def test_sigint_disposition_is_restored_on_success_and_creation_failure(self):
        def custom_handler(signum, frame):
            pass

        for index, disposition in enumerate((signal.default_int_handler, signal.SIG_IGN, custom_handler)):
            for succeeds in (True, False):
                with self.subTest(disposition=disposition, succeeds=succeeds):
                    signal.signal(signal.SIGINT, disposition)
                    argv = [sys.executable, "-c", "pass"] if succeeds else [str(self.root / "missing")]
                    kwargs = dict(
                        cwd=self.root, stdin=None, timeout=3,
                        stdout_path=self.root / f"stdout-{index}-{succeeds}",
                        stderr_path=self.root / f"stderr-{index}-{succeeds}",
                        env=managed_environment(),
                    )
                    if succeeds:
                        self.assertEqual(run_process(argv, **kwargs).returncode, 0)
                    else:
                        with self.assertRaises(ProcessError):
                            run_process(argv, **kwargs)
                    self.assertEqual(signal.getsignal(signal.SIGINT), disposition)

    def test_creation_failure_is_descriptive(self):
        with self.assertRaisesRegex(ProcessError, "could not start command"):
            run_process(
                [str(self.root / "no-such-executable")], cwd=self.root, stdin=None,
                stdout_path=self.root / "stdout.log", stderr_path=self.root / "stderr.log", timeout=1,
                env=managed_environment(),
            )

    def test_existing_outputs_and_symlinks_are_not_overwritten(self):
        stdout = self.root / "stdout.log"
        stdout.write_text("existing evidence")
        with self.assertRaises(ProcessError):
            self.run_command("raise SystemExit(0)")
        self.assertEqual(stdout.read_text(), "existing evidence")
        stdout.unlink()
        stdout.symlink_to(self.root / "missing-target")
        with self.assertRaises(ProcessError):
            self.run_command("raise SystemExit(0)")
        self.assertFalse((self.root / "missing-target").exists())

    def test_invalid_timeout_arguments_and_platform_fail_before_creation(self):
        for timeout in (0, -1, float("nan"), float("inf"), True, "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ProcessError):
                self.run_command("pass", timeout=timeout)
        for argv in ([], "echo hello", [""], ["echo", "bad\0argument"]):
            with self.subTest(argv=argv), self.assertRaises(ProcessError):
                run_process(argv, cwd=self.root, stdin=None, env=managed_environment(),
                            stdout_path=self.root / "stdout.log", stderr_path=self.root / "stderr.log", timeout=1)
        with patch("anvil.processes.os.name", "nt"), self.assertRaisesRegex(ProcessError, "POSIX"):
            self.run_command("pass")
        self.assertFalse((self.root / "stdout.log").exists())


if __name__ == "__main__":
    unittest.main()
