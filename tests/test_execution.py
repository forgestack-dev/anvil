import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from anvil.config import RunConfig
from anvil.adapters.codex import CodexRunner
from anvil.contracts import ContractError
from anvil.evidence import validate_result
from anvil.execution import run_serial
from anvil.store import RunStore
from anvil.workspaces import Repository, RepositoryLock, WorkspaceError


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class FakeRunner:
    def __init__(self, mode="success"):
        self.mode, self.workers, self.reviews = mode, [], []

    def run(self, **kwargs):
        workspace = kwargs["repo"]
        kwargs["artifact_dir"].mkdir(parents=True)
        if kwargs.get("read_only"):
            self.reviews.append(git(workspace, "rev-parse", "HEAD"))
            if self.mode == "review-mutates":
                (workspace / "README.md").write_text("changed after candidate creation")
            if self.mode == "review-rejects":
                return {"verdict": "request_changes", "summary": "Incorrect behavior",
                        "acceptance": [], "findings": ["The change does not meet the ticket"]}
            return {"verdict": "approve", "summary": "Diff and acceptance checked",
                    "acceptance": [{"criterion": 1, "satisfied": True, "evidence": "value.txt inspected"}],
                    "findings": []}
        self.workers.append(git(workspace, "rev-parse", "HEAD"))
        if self.mode == "interrupt":
            raise KeyboardInterrupt
        if self.mode == "blocked":
            return {"status": "blocked", "summary": "Need a decision", "acceptance": [],
                    "blockers": ["Clarify the output format"]}
        value = workspace / "value.txt"
        previous = int(value.read_text()) if value.exists() else 0
        value.write_text(str(previous + 1))
        if self.mode == "bad-check" or (self.mode == "second-fails" and len(self.workers) == 2):
            value.write_text("invalid")
        if self.mode == "worker-commits":
            git(workspace, "add", ".")
            git(workspace, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "unauthorized worker commit")
        claims = {"status": "completed", "summary": "Updated the value",
                  "acceptance": [{"criterion": 1, "evidence": "value.txt contains requested number"}],
                  "blockers": []}
        if self.mode == "missing-evidence":
            claims["acceptance"] = []
        return claims


class SerialExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "README.md").write_text("Fixture")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Initial fixture")
        self.base = git(self.repo, "rev-parse", "HEAD")
        tasks = [{"id": f"t{i}", "title": f"Set value {i}", "objective": f"Set value to {i}",
                  "depends_on": [] if i == 1 else ["t1"], "acceptance_criteria": [f"Value is {i}"]}
                 for i in (1, 2)]
        self.tickets = self.root / "tickets.json"
        self.tickets.write_text(json.dumps({"version": 1, "tasks": tasks}))
        self.config = RunConfig(self.repo, self.tickets,
            ((sys.executable, "-c", "from pathlib import Path; p=Path('value.txt'); assert not p.exists() or p.read_text() in ('1', '2')"),),
            self.root / "state", agent_timeout=10, check_timeout=10)

    def run_mode(self, mode):
        runner = FakeRunner(mode)
        result = run_serial(self.config, runner=runner)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse((self.repo / "value.txt").exists())
        return result, runner

    def test_serial_success_is_persisted_and_dependencies_use_integrated_commit(self):
        result, runner = self.run_mode("success")
        self.assertEqual(result["status"], "success")
        self.assertEqual([task["status"] for task in result["tasks"]], ["done", "done"])
        self.assertEqual(runner.workers[0], self.base)
        self.assertEqual(runner.workers[1], result["tasks"][0]["details"]["integrated_sha"])
        self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "2")
        saved = RunStore.read(Path(result["run_dir"]) / "state.sqlite")
        self.assertEqual(saved["status"], "success")
        for task, reviewed in zip(saved["tasks"], runner.reviews):
            details = task["details"]
            self.assertEqual(details["integrated_sha"], reviewed)
            self.assertEqual(details["verified_sha"], reviewed)
            self.assertEqual(details["reviewed_sha"], reviewed)
        self.assertEqual(json.loads((Path(result["run_dir"]) / "report.json").read_text())["status"], "success")

    def test_failures_never_advance_branch_or_unlock_dependents(self):
        for mode in ("bad-check", "missing-evidence", "review-mutates", "worker-commits"):
            with self.subTest(mode=mode):
                result, runner = self.run_mode(mode)
                self.assertEqual(result["status"], "failed")
                self.assertEqual([task["status"] for task in result["tasks"]], ["failed", "pending"])
                self.assertEqual(len(runner.workers), 1)
                self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)

    def test_blocked_worker_or_review_stops_with_reason(self):
        for mode in ("blocked", "review-rejects"):
            result, runner = self.run_mode(mode)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["tasks"][0]["status"], "blocked")
            self.assertTrue(result["error"])
            self.assertEqual(len(runner.workers), 1)
            self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)

    def test_partial_success_preserves_only_verified_work(self):
        result, runner = self.run_mode("second-fails")
        self.assertEqual(result["status"], "failed")
        self.assertEqual([task["status"] for task in result["tasks"]], ["done", "failed"])
        self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "1")

    def test_interrupt_is_persisted_without_success(self):
        result, runner = self.run_mode("interrupt")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["tasks"][0]["status"], "interrupted")

    def test_stop_after_done_notification_preserves_integrated_task(self):
        for exception, status in ((KeyboardInterrupt(), "interrupted"), (OSError("progress failed"), "failed")):
            with self.subTest(status=status):
                def stop_after_done(message):
                    if message.endswith(": done"):
                        raise exception

                result = run_serial(self.config, runner=FakeRunner(), progress=stop_after_done)
                self.assertEqual(result["status"], status)
                self.assertEqual([task["status"] for task in result["tasks"]], ["done", "pending"])
                self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "1")
                report = json.loads((Path(result["run_dir"]) / "report.json").read_text())
                self.assertEqual(report["status"], status)

    def test_interrupt_after_done_transaction_preserves_persisted_completion(self):
        transition = RunStore.transition

        def interrupt_after_commit(store, task_id, status, **kwargs):
            transition(store, task_id, status, **kwargs)
            if status == "done":
                raise KeyboardInterrupt

        with patch.object(RunStore, "transition", interrupt_after_commit):
            result = run_serial(self.config, runner=FakeRunner())
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual([task["status"] for task in result["tasks"]], ["done", "pending"])
        self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "1")
        self.assertTrue((Path(result["run_dir"]) / "report.json").is_file())

    def test_interrupt_after_success_transaction_preserves_completed_run(self):
        set_run = RunStore.set_run

        def interrupt_after_commit(store, status, **kwargs):
            set_run(store, status, **kwargs)
            if status == "success":
                raise KeyboardInterrupt

        with patch.object(RunStore, "set_run", interrupt_after_commit):
            result = run_serial(self.config, runner=FakeRunner())
        self.assertEqual(result["status"], "success")
        self.assertEqual([task["status"] for task in result["tasks"]], ["done", "done"])
        self.assertTrue((Path(result["run_dir"]) / "report.json").is_file())

    def test_real_codex_runner_completes_serial_run_through_fake_executable(self):
        binary = self.root / "fake codex"
        binary.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = sys.stdin.read()\n"
            "assert prompt\n"
            "output = pathlib.Path(args[args.index('-o') + 1])\n"
            "value = pathlib.Path('value.txt')\n"
            "if args[args.index('--sandbox') + 1] == 'read-only':\n"
            "    assert value.read_text() in ('1', '2')\n"
            "    result = {'verdict': 'approve', 'summary': 'Inspected value', 'findings': [],\n"
            "              'acceptance': [{'criterion': 1, 'satisfied': True, 'evidence': 'Value inspected'}]}\n"
            "else:\n"
            "    previous = int(value.read_text()) if value.exists() else 0\n"
            "    value.write_text(str(previous + 1))\n"
            "    result = {'status': 'completed', 'summary': 'Updated value', 'blockers': [],\n"
            "              'acceptance': [{'criterion': 1, 'evidence': 'Value updated'}]}\n"
            "output.write_text(json.dumps(result))\n"
            "print(json.dumps({'event': 'fake agent complete'}))\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        result = run_serial(self.config, runner=CodexRunner(str(binary)))
        self.assertEqual(result["status"], "success")
        self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "2")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        for task_id in ("t1", "t2"):
            for role in ("worker", "review"):
                artifact_dir = Path(result["run_dir"]) / "artifacts" / task_id / role
                self.assertTrue((artifact_dir / "result.json").is_file())
                self.assertTrue((artifact_dir / "schema.json").is_file())
                self.assertTrue((artifact_dir / "events.jsonl").is_file())

    def test_baseline_failure_never_launches_worker(self):
        config = RunConfig(self.repo, self.tickets, ((sys.executable, "-c", "raise SystemExit(1)"),),
                           self.root / "baseline-failure")
        runner = FakeRunner()
        result = run_serial(config, runner=runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.workers, [])

    def test_verification_cannot_change_the_reviewed_tree(self):
        command = "from pathlib import Path; p=Path('value.txt'); p.exists() and Path('README.md').write_text('mutated')"
        config = RunConfig(self.repo, self.tickets, ((sys.executable, "-c", command),), self.root / "mutation")
        result = run_serial(config, runner=FakeRunner())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)

    def test_dirty_target_and_competing_run_are_rejected_without_state(self):
        (self.repo / "untracked").write_text("user work")
        with self.assertRaises(WorkspaceError):
            run_serial(self.config, runner=FakeRunner())
        self.assertFalse(self.config.state_dir.exists())
        (self.repo / "untracked").unlink()
        with RepositoryLock(Repository(self.repo)):
            with self.assertRaises(WorkspaceError):
                run_serial(self.config, runner=FakeRunner())

    def test_unavailable_skills_fail_before_execution(self):
        document = json.loads(self.tickets.read_text())
        document["tasks"][0]["skills"] = ["implement"]
        self.tickets.write_text(json.dumps(document))
        with self.assertRaisesRegex(ContractError, "skill resolution"):
            run_serial(self.config, runner=FakeRunner())


class EvidenceTests(unittest.TestCase):
    def test_review_cannot_approve_missing_or_unsatisfied_criteria(self):
        from anvil.contracts import Task
        task = Task("t", "Title", "Objective", (), ("One", "Two"))
        result = {"verdict": "approve", "summary": "Looks good", "acceptance": [
            {"criterion": 1, "satisfied": True, "evidence": "Checked"}], "findings": []}
        with self.assertRaises(ContractError):
            validate_result(result, task, review=True)
        result["acceptance"].append({"criterion": 2, "satisfied": False, "evidence": "Failed"})
        with self.assertRaises(ContractError):
            validate_result(result, task, review=True)


if __name__ == "__main__":
    unittest.main()
