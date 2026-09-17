"""Fail-closed integration and cancellation for the mixed-agent coordinator."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from anvil.config import RunConfig, WorkerConfig
from anvil.contracts import ContractError
from anvil.execution import verify
from anvil.parallel import run_parallel
from anvil.environment import managed_environment
from anvil.processes import ProcessError, run_process
from anvil.store import RunStore


def claims():
    return {"status": "completed", "summary": "Implemented the requested change",
            "acceptance": [{"criterion": 1, "evidence": "The requested file was changed"}],
            "blockers": []}


class Worker:
    def __init__(self, action):
        self.action = action
        self.workspaces = []

    def run(self, **kwargs):
        self.workspaces.append(kwargs["repo"])
        result = self.action(kwargs["repo"], kwargs["artifact_dir"])
        return claims() if result is None else result


class Reviewer:
    def __init__(self, mode="approve"):
        self.mode = mode
        self.workspaces = []

    def run(self, **kwargs):
        self.workspaces.append(kwargs["repo"])
        if self.mode == "mutates":
            (kwargs["repo"] / "README.md").write_text("Unreviewed mutation\n")
        if self.mode == "rejects":
            return {"verdict": "request_changes", "summary": "The change is incomplete",
                    "acceptance": [], "findings": ["The acceptance criterion is not met"]}
        return {"verdict": "approve", "summary": "The candidate meets its criterion",
                "acceptance": [{"criterion": 1, "satisfied": True, "evidence": "Changed file inspected"}],
                "findings": []}


class ParallelFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git_binary = shutil.which("git")
        subprocess.run([self.git_binary, "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("Fixture\n")
        (self.repo / "shared.txt").write_text("original\n")
        (self.repo / "left.txt").write_text("original\n")
        (self.repo / "right.txt").write_text("original\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "Initial fixture")
        self.base = self.git("rev-parse", "HEAD")
        self.tickets = self.root / "tickets.json"
        self.write_tickets([
            self.ticket("a", "codex-worker"), self.ticket("b", "claude-worker"),
            self.ticket("dependent", "codex-worker", ["a", "b"]),
        ])
        self.config = RunConfig(
            self.repo, self.tickets, ((sys.executable, "-c", "pass"),), self.root / "state",
            agent_timeout=20, check_timeout=10,
            workers=(WorkerConfig("codex-worker", "codex"), WorkerConfig("claude-worker", "claude-code")),
        )

    def git(self, *args, repo=None):
        return subprocess.check_output([self.git_binary, "-C", str(repo or self.repo), *args], text=True).strip()

    @staticmethod
    def ticket(task_id, worker, dependencies=None):
        return {"id": task_id, "title": f"Implement {task_id}", "objective": f"Deliver {task_id}",
                "acceptance_criteria": [f"{task_id} works"], "worker": worker,
                "depends_on": dependencies or []}

    def write_tickets(self, tasks):
        self.tickets.write_text(json.dumps({"version": 1, "tasks": tasks}))

    def assert_saved_stop(self, result, expected_statuses, accepted=None):
        self.assertEqual([task["status"] for task in result["tasks"]], expected_statuses)
        self.assertFalse(any(task["status"] in {"running", "candidate", "reviewed", "verified", "integrating"}
                             for task in result["tasks"]))
        self.assertTrue(all(attempt["finished_at"] for attempt in result["attempts"]))
        self.assertEqual(self.git("rev-parse", result["branch"]), accepted or self.base)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        run_dir = Path(result["run_dir"])
        self.assertEqual(RunStore.read(run_dir / "state.sqlite"),
                         {key: value for key, value in result.items() if key != "run_dir"})
        self.assertEqual(json.loads((run_dir / "report.json").read_text()), result)

    def staggered_workers(self, first_file, second_file):
        accepted = Event()

        def first(repo, artifacts):
            (repo / first_file).write_text("first\n")

        def second(repo, artifacts):
            if not accepted.wait(timeout=10):
                raise RuntimeError("first ticket was never accepted")
            (repo / second_file).write_text("second\n")

        runners = {"codex-worker": Worker(first), "claude-worker": Worker(second)}
        progress = lambda message: accepted.set() if message == "a: done" else None
        return runners, progress

    def test_conflicting_stale_candidate_preserves_first_accepted_commit(self):
        runners, progress = self.staggered_workers("shared.txt", "shared.txt")
        reviewer = Reviewer()
        result = run_parallel(self.config, runners=runners, review_runner=reviewer, progress=progress)
        self.assertEqual(result["status"], "failed")
        first = result["tasks"][0]["details"]["integrated_sha"]
        self.assert_saved_stop(result, ["done", "failed", "pending"], accepted=first)
        self.assertEqual(self.git("show", f"{result['branch']}:shared.txt"), "first")
        self.assertEqual(len(reviewer.workspaces), 1)
        second = result["tasks"][1]
        self.assertIn("candidate_sha", second["details"])
        self.assertNotIn("reviewed_sha", second["details"])
        attempts = {attempt["task_id"]: attempt for attempt in result["attempts"]}
        self.assertEqual(attempts["a"]["base_sha"], self.base)
        self.assertEqual(attempts["b"]["base_sha"], self.base)
        self.assertIn("conflict", result["error"].lower())

    def test_clean_cherry_pick_requires_checks_on_combined_changes(self):
        runners, progress = self.staggered_workers("left.txt", "right.txt")
        check = (sys.executable, "-c", "from pathlib import Path; "
                 "assert not (Path('left.txt').read_text() == 'first\\n' and "
                 "Path('right.txt').read_text() == 'second\\n'), 'combined behavior is invalid'")
        config = replace(self.config, verification=(check,))
        reviewer = Reviewer()
        result = run_parallel(config, runners=runners, review_runner=reviewer, progress=progress)
        self.assertEqual(result["status"], "failed")
        first = result["tasks"][0]["details"]["integrated_sha"]
        self.assert_saved_stop(result, ["done", "failed", "pending"], accepted=first)
        # The combined change fails the checks, so its review is never dispatched:
        # only the first, accepted candidate was reviewed. docs/ACCEPTANCE.md
        self.assertEqual(len(reviewer.workspaces), 1)
        second = result["tasks"][1]["details"]
        self.assertNotIn("reviewed_sha", second)
        self.assertNotIn("verified_sha", second)
        self.assertTrue(any(record["returncode"] != 0 for record in second["verification"]))
        combined = result["attempts"][1]["details"]["candidate_sha"]
        self.assertEqual(self.git("show", f"{combined}:right.txt"), "second")
        self.assertEqual(self.git("show", f"{result['branch']}:right.txt"), "original")
        # Each stale worker's change passes by itself; only their combination fails.
        subprocess.run(check, cwd=runners["claude-worker"].workspaces[0], check=True)

    def test_invalid_worker_evidence_never_reaches_review(self):
        self.write_tickets([self.ticket("a", "codex-worker"),
                            self.ticket("dependent", "claude-worker", ["a"])])

        def invalid(repo, artifacts):
            (repo / "left.txt").write_text("changed\n")
            return claims() | {"acceptance": []}

        reviewer = Reviewer()
        peer = Worker(lambda repo, artifacts: self.fail("dependent was dispatched"))
        result = run_parallel(self.config, runners={"codex-worker": Worker(invalid), "claude-worker": peer},
                              review_runner=reviewer)
        self.assertEqual(result["status"], "failed")
        self.assert_saved_stop(result, ["failed", "pending"])
        self.assertEqual(reviewer.workspaces, [])
        self.assertNotIn("candidate_sha", result["tasks"][0]["details"])

    def test_reviewer_rejection_or_mutation_cannot_advance_the_branch(self):
        self.write_tickets([self.ticket("a", "codex-worker"),
                            self.ticket("dependent", "claude-worker", ["a"])])

        def implement(repo, artifacts):
            (repo / "left.txt").write_text("changed\n")

        for mode, status in (("rejects", "blocked"), ("mutates", "failed")):
            with self.subTest(mode=mode):
                worker = Worker(implement)
                result = run_parallel(self.config, runners={"codex-worker": worker, "claude-worker": worker},
                                      review_runner=Reviewer(mode))
                self.assertEqual(result["status"], status)
                self.assert_saved_stop(result, [status, "pending"])
                self.assertNotIn("reviewed_sha", result["tasks"][0]["details"])

    def test_known_peer_failure_prevents_accepting_a_verifying_candidate(self):
        verification_started = Event()

        def first(repo, artifacts):
            (repo / "left.txt").write_text("first\n")

        def fail_during_verification(repo, artifacts):
            if not verification_started.wait(timeout=10):
                raise RuntimeError("verification never started")
            raise ProcessError("peer worker failed during verification")

        def checks(config, workspace, artifacts):
            if artifacts.name == "verification":
                verification_started.set()
            return verify(config, workspace, artifacts)

        runners = {"codex-worker": Worker(first), "claude-worker": Worker(fail_during_verification)}
        with patch("anvil.parallel.verify", checks):
            result = run_parallel(self.config, runners=runners, review_runner=Reviewer())
        self.assertTrue(verification_started.is_set())
        self.assertEqual(result["status"], "failed")
        self.assert_saved_stop(result, ["interrupted", "failed", "pending"])
        self.assertIn("peer worker failed", result["error"])
        self.assertNotIn("integrated_sha", result["tasks"][0]["details"])

    def test_failed_peer_cancels_and_reaps_running_agent_process(self):
        started = self.root / "child.pid"
        stopped = Event()

        def fail_after_peer_starts(repo, artifacts):
            deadline = time.monotonic() + 10
            while not started.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("peer process did not start")
                time.sleep(0.01)
            raise ProcessError("injected worker failure")

        def long_running(repo, artifacts):
            artifacts.mkdir(parents=True)
            program = ("import os, time; from pathlib import Path; "
                       f"Path({str(started)!r}).write_text(str(os.getpid())); time.sleep(60)")
            try:
                run_process([sys.executable, "-c", program], cwd=repo, stdin=None,
                            stdout_path=artifacts / "stdout", stderr_path=artifacts / "stderr",
                            timeout=20, env=managed_environment())
            finally:
                stopped.set()

        before = time.monotonic()
        result = run_parallel(
            self.config, runners={"codex-worker": Worker(fail_after_peer_starts),
                                  "claude-worker": Worker(long_running)}, review_runner=Reviewer())
        elapsed = time.monotonic() - before
        self.assertEqual(result["status"], "failed")
        self.assert_saved_stop(result, ["failed", "interrupted", "pending"])
        self.assertTrue(stopped.is_set())
        self.assertLess(elapsed, 10, "cancellation waited for the worker's 20-second timeout")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(started.read_text()), 0)

    def test_unknown_worker_is_rejected_before_creating_run_state(self):
        self.write_tickets([self.ticket("a", "missing-worker")])
        with patch("anvil.parallel.create_runner", side_effect=AssertionError("runner creation was premature")):
            with self.assertRaisesRegex(ContractError, "unknown worker"):
                run_parallel(self.config)
        self.assertFalse(self.config.state_dir.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.git("for-each-ref", "refs/heads/anvil"), "")

    def test_missing_worker_or_reviewer_binary_does_not_fall_back_or_dispatch(self):
        private_bin = self.root / "bin"
        private_bin.mkdir()
        (private_bin / "git").symlink_to(self.git_binary)
        marker = self.root / "unexpected-launch"
        for name in ("codex", "claude"):
            binary = private_bin / name
            binary.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
            binary.chmod(0o755)
        missing = str(private_bin / "missing-agent")
        worker_missing = replace(self.config, workers=(self.config.workers[0],
                                 WorkerConfig("claude-worker", "claude-code", missing)))
        reviewer_missing = replace(self.config, agent_binary=missing)
        with patch.dict(os.environ, {"PATH": str(private_bin)}):
            for config in (worker_missing, reviewer_missing):
                with self.subTest(reviewer=config.executable), self.assertRaisesRegex(ContractError, "not found"):
                    run_parallel(config)
                self.assertFalse(marker.exists())
                self.assertFalse(config.state_dir.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual(self.git("for-each-ref", "refs/heads/anvil"), "")


if __name__ == "__main__":
    unittest.main()
