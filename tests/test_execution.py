import json
import os
from pathlib import Path
import signal
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

    def test_startup_stops_are_persisted_before_any_work_is_launched(self):
        initialize, set_run = RunStore.initialize, RunStore.set_run
        for stage in ("after-initialize", "before-running", "after-running"):
            for cause, status in (("sigint", "interrupted"), ("oserror", "failed")):
                with self.subTest(stage=stage, cause=cause):
                    def stop():
                        if cause == "sigint":
                            os.kill(os.getpid(), signal.SIGINT)
                        else:
                            raise OSError("startup failed")

                    def initialize_and_stop(store, **kwargs):
                        initialize(store, **kwargs)
                        if stage == "after-initialize":
                            stop()

                    def set_run_and_stop(store, next_status, **kwargs):
                        if next_status == "running" and stage == "before-running":
                            stop()
                        set_run(store, next_status, **kwargs)
                        if next_status == "running" and stage == "after-running":
                            stop()

                    runner = FakeRunner()
                    original_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
                    try:
                        with patch.object(RunStore, "initialize", initialize_and_stop), \
                                patch.object(RunStore, "set_run", set_run_and_stop):
                            try:
                                result = run_serial(self.config, runner=runner)
                            except KeyboardInterrupt:
                                self.fail("startup SIGINT escaped without a persisted interrupted report")
                    finally:
                        signal.signal(signal.SIGINT, original_handler)

                    self.assertEqual(result["status"], status)
                    self.assertTrue(result["error"])
                    self.assertEqual([task["status"] for task in result["tasks"]], ["pending", "pending"])
                    self.assertTrue(all(task["attempt_id"] is None for task in result["tasks"]))
                    self.assertEqual(result["attempts"], [])
                    self.assertEqual(runner.workers, [])
                    self.assertEqual(runner.reviews, [])
                    run_dir = Path(result["run_dir"])
                    self.assertFalse((run_dir / "integration").exists())
                    self.assertFalse((run_dir / "workers").exists())
                    self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/anvil/"), "")
                    self.assertEqual(RunStore.read(run_dir / "state.sqlite"),
                                     {key: value for key, value in result.items() if key != "run_dir"})
                    self.assertEqual(json.loads((run_dir / "report.json").read_text()), result)
                    run_events = [event for event in result["events"] if event["kind"] == "run"]
                    expected_states = ["created", "running", status] if stage == "after-running" else ["created", status]
                    self.assertEqual([event["to_status"] for event in run_events], expected_states)
                    self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
                    self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                    self.assertEqual((self.repo / "README.md").read_text(), "Fixture")
                    self.assertFalse((self.repo / "value.txt").exists())
                    with RepositoryLock(Repository(self.repo)):
                        pass

    def test_failure_before_initialization_preserves_original_exception(self):
        for error in (KeyboardInterrupt(), OSError("cannot initialize the ledger")):
            with self.subTest(error=type(error).__name__):
                previous_runs = set(self.config.state_dir.glob("*"))
                runner = FakeRunner()
                with patch.object(RunStore, "initialize", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        run_serial(self.config, runner=runner)
                self.assertIs(raised.exception, error)
                self.assertEqual(runner.workers, [])
                self.assertEqual(runner.reviews, [])
                new_runs = set(self.config.state_dir.glob("*")) - previous_runs
                self.assertEqual(len(new_runs), 1)
                run_dir = new_runs.pop()
                self.assertFalse((run_dir / "report.json").exists())
                self.assertFalse((run_dir / "integration").exists())
                self.assertFalse((run_dir / "workers").exists())
                self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/anvil/"), "")
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                with RepositoryLock(Repository(self.repo)):
                    pass

    def test_stop_after_attempt_start_uses_persisted_owner_before_return(self):
        start_attempt = RunStore.start_attempt
        for exception, status in ((KeyboardInterrupt(), "interrupted"), (OSError("start failed"), "failed")):
            with self.subTest(status=status):
                def stop_after_commit(store, *args, **kwargs):
                    start_attempt(store, *args, **kwargs)
                    raise exception

                runner = FakeRunner()
                with patch.object(RunStore, "start_attempt", stop_after_commit):
                    result = run_serial(self.config, runner=runner)
                self.assertEqual(result["status"], status)
                self.assertEqual([task["status"] for task in result["tasks"]], [status, "pending"])
                self.assertEqual(result["attempts"][0]["status"], status)
                self.assertIsNotNone(result["attempts"][0]["finished_at"])
                self.assertEqual(runner.workers, [])
                self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)
                report = json.loads((Path(result["run_dir"]) / "report.json").read_text())
                self.assertEqual(report, result)

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
        for attempt in result["attempts"]:
            for role in ("worker", "review"):
                artifact_dir = Path(result["run_dir"]) / "artifacts" / attempt["id"] / role
                self.assertTrue((artifact_dir / "result.json").is_file())
                self.assertTrue((artifact_dir / "schema.json").is_file())
                self.assertTrue((artifact_dir / "events.jsonl").is_file())

    def test_case_distinct_ticket_ids_use_distinct_workspaces_and_evidence(self):
        document = json.loads(self.tickets.read_text())
        document["tasks"][0]["id"] = "Ticket"
        document["tasks"][1]["id"] = "ticket"
        document["tasks"][1]["depends_on"] = ["Ticket"]
        self.tickets.write_text(json.dumps(document))
        result, runner = self.run_mode("success")
        self.assertEqual(result["status"], "success")
        self.assertEqual([(t["id"], t["status"]) for t in result["tasks"]],
                         [("Ticket", "done"), ("ticket", "done")])
        self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "2")
        self.assertEqual(runner.workers[1], result["tasks"][0]["details"]["integrated_sha"])
        workers, artifacts = [], []
        for attempt in result["attempts"]:
            workspace = Path(attempt["workspace"])
            evidence = Path(result["run_dir"]) / "artifacts" / attempt["id"]
            self.assertEqual(workspace.name, attempt["id"])
            self.assertTrue(workspace.is_dir())
            for role in ("worker", "review", "verification"):
                self.assertTrue((evidence / role).is_dir())
            self.assertTrue((evidence / "verification" / "1.stdout.log").is_file())
            workers.append(str(workspace).casefold())
            artifacts.append(str(evidence).casefold())
        self.assertEqual(len(set(workers)), 2)
        self.assertEqual(len(set(artifacts)), 2)

    def test_baseline_failure_never_launches_worker(self):
        config = RunConfig(self.repo, self.tickets, ((sys.executable, "-c", "raise SystemExit(1)"),),
                           self.root / "baseline-failure")
        runner = FakeRunner()
        result = run_serial(config, runner=runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner.workers, [])

    def test_git_root_verification_ignores_inherited_checkout_overrides(self):
        document = json.loads(self.tickets.read_text())
        document["tasks"] = document["tasks"][:1]
        self.tickets.write_text(json.dumps(document))
        command = (
            "import os, subprocess; from pathlib import Path; "
            "assert os.environ['ANVIL_TEST_PROJECT_SETTING'] == 'preserved'; "
            "root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()); "
            "print(root, flush=True); "
            "value = root / 'value.txt'; "
            "assert not value.exists() or value.read_text() == '1'"
        )
        config = RunConfig(self.repo, self.tickets, ((sys.executable, "-c", command),),
                           self.root / "git-context", agent_timeout=10, check_timeout=10)
        inherited = {"GIT_DIR": str(self.repo / ".git"), "GIT_WORK_TREE": str(self.repo),
                     "ANVIL_TEST_PROJECT_SETTING": "preserved"}
        for mode, status in (("bad-check", "failed"), ("success", "success")):
            with self.subTest(mode=mode):
                runner = FakeRunner(mode)
                with patch.dict(os.environ, inherited):
                    result = run_serial(config, runner=runner)
                self.assertEqual(result["status"], status)
                self.assertEqual(len(runner.workers), 1)
                self.assertEqual(len(runner.reviews), 1)
                run_dir = Path(result["run_dir"])
                check_dir = run_dir / "artifacts" / result["attempts"][0]["id"] / "verification"
                self.assertEqual(Path((check_dir / "1.stdout.log").read_text().strip()),
                                 run_dir / "integration")
                if mode == "bad-check":
                    self.assertEqual(result["tasks"][0]["status"], "failed")
                    self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)
                    self.assertEqual((run_dir / "integration" / "value.txt").read_text(), "invalid")
                else:
                    self.assertEqual(result["tasks"][0]["status"], "done")
                    self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "1")
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                self.assertFalse((self.repo / "value.txt").exists())

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
