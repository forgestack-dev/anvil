"""Exercise real local processes without using model workers or the network."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from anvil.processes import ProcessError, run_process


@unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="anvil process ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_command(self, code, *, stdin=None, timeout=3, args=()):
        return run_process(
            [sys.executable, "-c", code, *args], cwd=self.root,
            stdin=stdin, stdout_path=self.root / "stdout.log",
            stderr_path=self.root / "stderr.log", timeout=timeout,
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

    def test_creation_failure_is_descriptive(self):
        with self.assertRaisesRegex(ProcessError, "could not start command"):
            run_process(
                [str(self.root / "no-such-executable")], cwd=self.root, stdin=None,
                stdout_path=self.root / "stdout.log", stderr_path=self.root / "stderr.log", timeout=1,
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
                run_process(argv, cwd=self.root, stdin=None,
                            stdout_path=self.root / "stdout.log", stderr_path=self.root / "stderr.log", timeout=1)
        with patch("anvil.processes.os.name", "nt"), self.assertRaisesRegex(ProcessError, "POSIX"):
            self.run_command("pass")
        self.assertFalse((self.root / "stdout.log").exists())


if __name__ == "__main__":
    unittest.main()
