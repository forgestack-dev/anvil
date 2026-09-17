"""A peer failure must remain observable while supervisor Git waits for capacity."""

from dataclasses import replace
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from anvil.config import WorkerConfig
from anvil.environment import managed_environment
from anvil.parallel import run_parallel
from anvil.processes import run_process
from anvil.workspaces import Repository
import test_parallel_execution as execution_fixture


class ParallelFailureObservationTests(unittest.TestCase):
    def test_peer_failure_cancels_commands_while_candidate_git_waits_for_capacity(self):
        self.assert_peer_stop_interrupts_capacity_wait(blocked=False)

    def test_blocked_worker_result_cancels_commands_while_candidate_git_waits_for_capacity(self):
        self.assert_peer_stop_interrupts_capacity_wait(blocked=True)

    def assert_peer_stop_interrupts_capacity_wait(self, *, blocked):
        fixture = execution_fixture.ParallelExecutionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write_tickets(*(execution_fixture.ticket(name, worker=name) for name in "abc"))
        config = replace(fixture.config, workers=tuple(WorkerConfig(name) for name in "abc"),
                         max_processes=1)
        started, completed = fixture.root / "started", fixture.root / "completed"
        fail_b = threading.Event()
        failure_at = []

        class Runner:
            def __init__(self, name):
                self.name = name

            def run(self, **kwargs):
                if kwargs.get("read_only"):
                    return execution_fixture.result_for(self.name, review=True)
                if self.name == "b":
                    if not fail_b.wait(5):
                        raise AssertionError("coordinator never attempted candidate Git")
                    failure_at.append(time.monotonic())
                    if blocked:
                        return {"status": "blocked", "summary": "A decision is required", "acceptance": [],
                                "blockers": ["worker b blocked while supervisor Git awaited capacity"]}
                    raise RuntimeError("worker b failed while supervisor Git awaited capacity")
                if self.name == "c":
                    kwargs["artifact_dir"].mkdir(parents=True)
                    command = [sys.executable, "-c",
                               "from pathlib import Path; import os,sys,time; "
                               "Path(sys.argv[1]).write_text(str(os.getpid())); "
                               "time.sleep(5); Path(sys.argv[2]).write_text('completed')",
                               str(started), str(completed)]
                    run_process(command, cwd=kwargs["repo"], stdin=None,
                                stdout_path=kwargs["artifact_dir"] / "out",
                                stderr_path=kwargs["artifact_dir"] / "err", timeout=8,
                                env=managed_environment())
                (kwargs["repo"] / (self.name + ".txt")).write_text(self.name)
                return execution_fixture.result_for(self.name)

        original = Repository.commit_candidate

        def candidate_waits_for_peer(repo, workspace, base, message):
            if "Anvil: a " in message:
                deadline = time.monotonic() + 5
                while not started.exists():
                    if time.monotonic() >= deadline:
                        raise AssertionError("peer never occupied command capacity")
                    time.sleep(0.01)
                fail_b.set()
            return original(repo, workspace, base, message)

        with patch.object(Repository, "commit_candidate", candidate_waits_for_peer):
            result = run_parallel(config, runners={name: Runner(name) for name in "abc"},
                                  review_runner=Runner("review"))
        status = "blocked" if blocked else "failed"
        self.assertEqual(result["status"], status)
        self.assertIn(f"worker b {status}", result["error"])
        self.assertEqual([task["status"] for task in result["tasks"]],
                         ["interrupted", status, "interrupted"])
        if blocked:
            self.assertEqual(result["tasks"][1]["details"]["worker"]["status"], "blocked")
        self.assertLess(time.monotonic() - failure_at[0], 3,
                        "a known failure waited for an unrelated command to finish")
        self.assertFalse(completed.exists(), "the unrelated command completed instead of being cancelled")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(started.read_text()), 0)
        self.assertEqual(execution_fixture.git(fixture.repo, "rev-parse", result["branch"]), fixture.base)
        fixture.assert_clean_original()


if __name__ == "__main__":
    unittest.main()
