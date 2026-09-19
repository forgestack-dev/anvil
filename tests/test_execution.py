from contextlib import chdir
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
from textwrap import dedent
import unittest
from unittest.mock import patch

from anvil.config import RunConfig
from anvil.adapters.codex import CodexRunner
from anvil.contracts import ContractError
from anvil.contracts import Task
from anvil.evidence import format_findings, validate_result
from anvil.execution import run_serial
from anvil.processes import InvocationExhausted
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
                        "acceptance": [{"criterion": 1, "satisfied": False,
                                        "evidence": "value.txt holds the wrong number"}],
                        "findings": [{"criterion": 1, "location": "value.txt:1",
                                      "finding": "The change does not meet the ticket"}]}
            return {"verdict": "approve", "summary": "Diff and acceptance checked",
                    "acceptance": [{"criterion": 1, "satisfied": True, "evidence": "value.txt inspected"}],
                    "findings": []}
        self.workers.append(git(workspace, "rev-parse", "HEAD"))
        if self.mode == "interrupt":
            raise KeyboardInterrupt
        if self.mode == "turn-exhaustion":
            raise InvocationExhausted("stopped after exhausting its configured turns", "turn_exhaustion")
        if self.mode == "budget-exhaustion":
            raise InvocationExhausted("stopped after exhausting its configured budget", "budget_exhaustion")
        if self.mode == "turn-exhaustion-partial":
            (workspace / "partial.txt").write_text("interrupted mid-turn")
            raise InvocationExhausted("stopped after exhausting its configured turns", "turn_exhaustion")
        if self.mode == "blocked":
            return {"status": "blocked", "summary": "Need a decision", "acceptance": [],
                    "blockers": ["Clarify the output format"]}
        value = workspace / "value.txt"
        # The target is a function of the base this attempt was given, not of
        # whatever the worktree happens to hold. An amend retry restores the
        # rejected candidate's tree into the worktree, and a fake worker that
        # counted from there would count its own previous attempt again.
        committed = git(workspace, "ls-tree", "--name-only", "HEAD", "value.txt")
        previous = int(git(workspace, "show", "HEAD:value.txt")) if committed else 0
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

    def test_a_classified_invocation_exhaustion_reaches_the_ledger_as_its_category(self):
        for mode, category in (("turn-exhaustion", "turn_exhaustion"),
                               ("budget-exhaustion", "budget_exhaustion")):
            with self.subTest(mode=mode):
                result, runner = self.run_mode(mode)
                self.assertEqual(result["status"], "failed")
                failed_task = next(task for task in result["tasks"] if task["status"] == "failed")
                self.assertEqual(failed_task["details"]["failure_category"], category)
                # This fake worker exhausts before writing anything: nothing
                # changed, so no revision is recorded for it.
                self.assertNotIn("exhausted_sha", failed_task["details"])
                saved = RunStore.read(Path(result["run_dir"]) / "state.sqlite")
                failed_attempt = next(a for a in saved["attempts"] if a["status"] == "failed")
                self.assertEqual(failed_attempt["details"]["failure_category"], category)
                self.assertNotIn("exhausted_sha", failed_attempt["details"])

    def test_an_exhausted_worker_with_a_partial_tree_retains_its_own_revision(self):
        """RUN_ECONOMICS.md slice 4: an exhausted tree is not a candidate.

        The run stops exactly as it does today -- same status, same error,
        same failure_category -- and the only new thing is the revision the
        supervisor commits for the partial tree, on the attempt's base, under
        its own retained kind.
        """
        result, runner = self.run_mode("turn-exhaustion-partial")
        self.assertEqual(result["status"], "failed")
        failed_task = next(task for task in result["tasks"] if task["status"] == "failed")
        self.assertEqual(failed_task["details"]["failure_category"], "turn_exhaustion")
        revision = failed_task["details"]["exhausted_sha"]
        self.assertEqual(git(self.repo, "show", "-s", "--format=%P", revision), self.base)
        self.assertEqual(git(self.repo, "show", f"{revision}:partial.txt"), "interrupted mid-turn")
        # Never a candidate: no candidate_sha, no acceptance claims, no
        # review, and the task never reached the candidate phase.
        self.assertNotIn("candidate_sha", failed_task["details"])
        self.assertNotIn("review", failed_task["details"])
        attempt = self.assert_retained(result, kinds=("exhausted",))
        ref = f"refs/anvil/exhausted/{result['run_id']}/{attempt}"
        self.assertEqual(git(self.repo, "rev-parse", ref), revision)
        saved = RunStore.read(Path(result["run_dir"]) / "state.sqlite")
        failed_attempt = next(a for a in saved["attempts"] if a["status"] == "failed")
        self.assertEqual(failed_attempt["details"]["exhausted_sha"], revision)

    def test_retained_exhausted_revision_is_listed_and_can_be_reaped(self):
        from anvil.retention import delete
        import shutil
        result = self.run_mode("turn-exhaustion-partial")[0]
        revision = result["tasks"][0]["details"]["exhausted_sha"]
        listed = self.retained()["items"]
        entry = next(item for item in listed if item["kind"] == "exhausted")
        self.assertEqual(entry["revision"], revision)
        self.assertFalse(entry["accepted"])
        # The ledger still names it: reaping the run's refs while it survives
        # must refuse to strand this one.
        outcome = delete(self.repo, self.config.state_dir, result["run_id"])
        self.assertEqual(outcome["removed_count"], 0)
        self.assertIn(revision, {item["revision"] for item in outcome["refused"]})
        # Once the ledger is gone nothing records the revision, so it can be
        # reaped.
        shutil.rmtree(result["run_dir"])
        outcome = delete(self.repo, self.config.state_dir, result["run_id"])
        self.assertGreater(outcome["removed_count"], 0)
        self.assertEqual(outcome["refused"], [])

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

    def claude_configuration(self, mode="success"):
        binary = self.root / f"fake claude-{mode}"
        trace = self.root / f"claude-{mode}-trace.jsonl"
        binary.write_text(
            f"#!{sys.executable}\n"
            f"MODE = {mode!r}\n"
            f"TRACE = {str(trace)!r}\n"
            + dedent("""\
                import json
                from pathlib import Path
                import subprocess
                import sys
                import uuid

                args = sys.argv[1:]
                prompt = sys.stdin.read()
                schema = json.loads(args[args.index('--json-schema') + 1])
                read_only = 'verdict' in schema['required']
                head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
                with Path(TRACE).open('a') as stream:
                    stream.write(json.dumps({'argv': args, 'prompt': prompt, 'schema': schema,
                                             'read_only': read_only, 'head': head,
                                             'cwd': str(Path.cwd()), 'entrypoint': sys.argv[0]}) + '\\n')
                value = Path('value.txt')
                if read_only:
                    result = {'verdict': 'approve', 'summary': 'Inspected candidate files',
                              'findings': [], 'acceptance': [
                                  {'criterion': 1, 'satisfied': True, 'evidence': 'Value inspected'}]}
                    if MODE == 'invalid-review':
                        result['acceptance'] = []
                    elif MODE == 'contradictory-approve':
                        result['findings'] = ['No actionable findings. Non-blocking nit: prefer a trailing newline.']
                    elif MODE == 'review-rejects':
                        result.update(verdict='request_changes',
                                      acceptance=[{'criterion': 1, 'satisfied': False,
                                                   'evidence': 'Value inspected'}],
                                      findings=[{'criterion': 1, 'location': 'value.txt:1',
                                                 'finding': 'The requested behavior is missing'}])
                    elif MODE == 'review-mutates':
                        Path('README.md').write_text('Unexpected reviewer mutation')
                else:
                    previous = int(value.read_text()) if value.exists() else 0
                    value.write_text('invalid' if MODE == 'bad-check' else str(previous + 1))
                    result = {'status': 'completed', 'summary': 'Updated value', 'blockers': [],
                              'acceptance': [{'criterion': 1, 'evidence': 'Value updated'}]}
                denials = [{'tool_name': 'Edit', 'tool_use_id': 'denied-review-edit',
                            'tool_input': {}}] if read_only and MODE == 'denied-review' else []
                session_id = str(uuid.uuid4())
                print(json.dumps({'type': 'system', 'subtype': 'init'}))
                print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                  'permission_denials': denials, 'structured_output': result,
                                  'session_id': session_id, 'num_turns': 21}))
                print(json.dumps({'type': 'system', 'subtype': 'task_summary', 'detail': None,
                                  'uuid': str(uuid.uuid4()), 'session_id': session_id}))
                """),
            encoding="utf-8",
        )
        binary.chmod(0o755)
        document = self.config.to_dict() | {
            "repo": "repo", "tickets": "tickets.json", "state_dir": f"claude-state-{mode}",
            "agent": "claude-code", "agent_binary": f"./{binary.name}",
        }
        config_path = self.root / f"claude-{mode}-run.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")
        return RunConfig.load(config_path), trace

    def test_claude_selected_by_configuration_completes_dependencies_with_exact_commit_evidence(self):
        config, trace_path = self.claude_configuration()
        result = run_serial(config)
        self.assertEqual(result["status"], "success", result["error"])
        self.assertEqual([task["status"] for task in result["tasks"]], ["done", "done"])
        self.assertEqual(result["config"]["agent"], "claude-code")
        self.assertEqual(result["config"]["agent_binary"], config.executable)
        trace = [json.loads(line) for line in trace_path.read_text().splitlines()]
        self.assertEqual([call["read_only"] for call in trace], [False, True, False, True])
        expected_base = self.base
        for number, (task, attempt) in enumerate(zip(result["tasks"], result["attempts"]), start=1):
            worker, review = trace[2 * (number - 1):2 * number]
            details = task["details"]
            integrated = details["integrated_sha"]
            self.assertEqual(attempt["base_sha"], expected_base)
            self.assertEqual(worker["head"], expected_base)
            self.assertEqual(review["head"], integrated)
            self.assertEqual(details["reviewed_sha"], integrated)
            self.assertEqual(details["verified_sha"], integrated)
            self.assertEqual(git(self.repo, "rev-parse", f"{details['candidate_sha']}^"), expected_base)
            self.assertEqual(git(self.repo, "show", f"{details['candidate_sha']}:value.txt"), str(number))
            self.assertEqual(git(self.repo, "rev-parse", f"{integrated}^"), expected_base)
            self.assertEqual(git(self.repo, "show", f"{integrated}:value.txt"), str(number))
            self.assertIn(expected_base, worker["prompt"])
            self.assertIn(expected_base, review["prompt"])
            self.assertIn(integrated, review["prompt"])
            expected_diff = git(self.repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                                "--no-renames", expected_base, integrated, "--")
            self.assertTrue(expected_diff)
            self.assertIn(expected_diff, review["prompt"])
            self.assertIn(f"+{number}", review["prompt"])
            if number == 2:
                self.assertIn("-1", review["prompt"])
            for role, call in (("worker", worker), ("review", review)):
                args = call["argv"]
                tools = set(args[args.index("--tools") + 1].split(","))
                expected_tools = {"Read", "Glob", "Grep"} | ({"Edit", "Write"} if role == "worker" else set())
                self.assertEqual(tools, expected_tools)
                self.assertIn("Bash", args[args.index("--disallowedTools") + 1].split(","))
                self.assertEqual(args[args.index("--permission-mode") + 1],
                                 "acceptEdits" if role == "worker" else "dontAsk")
                self.assertNotIn(call["prompt"], args)
                artifact_dir = Path(result["run_dir"]) / "artifacts" / attempt["id"] / role
                self.assertEqual(json.loads((artifact_dir / "schema.json").read_text()), call["schema"])
                events = [json.loads(line) for line in (artifact_dir / "events.jsonl").read_text().splitlines()]
                normalized = json.loads((artifact_dir / "result.json").read_text())
                self.assertEqual([(event["type"], event["subtype"]) for event in events],
                                 [("system", "init"), ("result", "success"), ("system", "task_summary")])
                self.assertEqual(events[-1]["session_id"], events[-2]["session_id"])
                self.assertIsNone(events[-1]["detail"])
                self.assertEqual(events[-2]["num_turns"], 21)
                self.assertEqual(events[-2]["structured_output"], normalized)
                self.assertEqual(normalized, details["worker" if role == "worker" else "review"])
                self.assertTrue((artifact_dir / "stderr.log").is_file())
            transitions = [event["to_status"] for event in result["events"] if event["task_id"] == task["id"]]
            self.assertEqual(transitions, ["running", "candidate", "verified", "reviewed", "integrating", "done"])
            self.assertTrue(all(check["returncode"] == 0 and not check["timed_out"]
                                for check in details["verification"]))
            expected_base = integrated
        self.assertEqual(git(self.repo, "rev-parse", result["branch"]), expected_base)
        self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "2")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse((self.repo / "value.txt").exists())
        self.assertEqual(RunStore.read(Path(result["run_dir"]) / "state.sqlite"),
                         {key: value for key, value in result.items() if key != "run_dir"})

    def test_claude_configuration_preserves_selected_alias_across_dependent_tickets(self):
        config, trace_path = self.claude_configuration("selected-alias")
        alias = self.root / "anvil-test-claude"
        alias.symlink_to(Path(config.executable).name)
        shadow_directory = self.root / "shadow bin"
        shadow_directory.mkdir()
        shadow = shadow_directory / alias.name
        shadow.write_text(f"#!{sys.executable}\nraise SystemExit('wrong executable selected')\n")
        shadow.chmod(0o755)
        git_directory = self.root / "git bin"
        git_directory.mkdir()
        (git_directory / "git").symlink_to(Path(shutil.which("git")).absolute())

        for binary in (alias.name, f"./{alias.name}"):
            with self.subTest(binary=binary):
                trace_path.write_text("")
                config_path = self.root / "selected-alias-run.json"
                config_path.write_text(json.dumps(config.to_dict() | {"agent_binary": binary}))
                selected_config = RunConfig.load(config_path)

                def replace_search_path_after_first_ticket(message):
                    if message == "t1: done":
                        os.environ["PATH"] = str(shadow_directory) + os.pathsep + str(git_directory)

                with chdir(self.root), patch.dict(os.environ, {"PATH": "." + os.pathsep + str(git_directory)}):
                    result = run_serial(selected_config, progress=replace_search_path_after_first_ticket)
                    final_search_path = os.environ["PATH"]

                self.assertEqual(result["status"], "success", result["error"])
                self.assertEqual(final_search_path, str(shadow_directory) + os.pathsep + str(git_directory))
                self.assertEqual([task["status"] for task in result["tasks"]], ["done", "done"])
                calls = [json.loads(line) for line in trace_path.read_text().splitlines()]
                self.assertEqual([call["read_only"] for call in calls], [False, True, False, True])
                self.assertEqual({call["entrypoint"] for call in calls}, {str(alias.parent.resolve() / alias.name)})
                self.assertEqual(calls[0]["head"], self.base)
                self.assertEqual(calls[2]["head"], result["tasks"][0]["details"]["integrated_sha"])
                for task, review in zip(result["tasks"], calls[1::2]):
                    details = task["details"]
                    self.assertEqual(details["integrated_sha"], review["head"])
                    self.assertEqual(details["reviewed_sha"], review["head"])
                    self.assertEqual(details["verified_sha"], review["head"])
                self.assertEqual(git(self.repo, "show", f"{result['branch']}:value.txt"), "2")
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_claude_review_and_verification_failures_preserve_branch_and_pending_dependency(self):
        for mode in ("invalid-review", "contradictory-approve", "denied-review", "review-mutates",
                     "review-rejects", "bad-check"):
            with self.subTest(mode=mode):
                config, trace_path = self.claude_configuration(mode)
                result = run_serial(config)
                expected_status = "blocked" if mode == "review-rejects" else "failed"
                self.assertEqual(result["status"], expected_status)
                self.assertEqual([task["status"] for task in result["tasks"]], [expected_status, "pending"])
                self.assertEqual(len(result["attempts"]), 1)
                self.assertIsNone(result["tasks"][1]["attempt_id"])
                calls = [json.loads(line) for line in trace_path.read_text().splitlines()]
                self.assertTrue(result["error"])
                details = result["tasks"][0]["details"]
                self.assertIn("candidate_sha", details)
                self.assertNotIn("integrated_sha", details)
                self.assertNotIn("reviewed_sha", details)
                if mode == "bad-check":
                    # The checks reject the candidate, so no reviewer is invoked
                    # and the run never pays for one. docs/ACCEPTANCE.md
                    self.assertEqual([call["read_only"] for call in calls], [False])
                    self.assertNotIn("verified_sha", details)
                    self.assertNotEqual(details["verification"][0]["returncode"], 0)
                else:
                    self.assertEqual([call["read_only"] for call in calls], [False, True])
                    # Every review rejection now sits beside passing checks for the same sha.
                    self.assertEqual(details["verified_sha"], calls[1]["head"])
                    self.assertEqual([r["returncode"] for r in details["verification"]], [0])
                if mode == "contradictory-approve":
                    review_path = (Path(result["run_dir"]) / "artifacts" / result["attempts"][0]["id"]
                                   / "review" / "result.json")
                    review = json.loads(review_path.read_text())
                    self.assertEqual(review["verdict"], "approve")
                    self.assertEqual([item["satisfied"] for item in review["acceptance"]], [True])
                    self.assertEqual(review["findings"],
                                     ["No actionable findings. Non-blocking nit: prefer a trailing newline."])
                    self.assertIn("contradicts", result["error"])
                self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)
                self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                self.assertFalse((self.repo / "value.txt").exists())
                self.assertEqual(json.loads((Path(result["run_dir"]) / "report.json").read_text()), result)

    # -- Stage 2: bound and located rejections (docs/ACCEPTANCE.md) ----------

    @staticmethod
    def review(acceptance, findings, verdict="request_changes"):
        return {"verdict": verdict, "summary": "Reviewed", "acceptance": acceptance,
                "findings": findings}

    def test_a_rejection_must_assess_every_criterion(self):
        """A rejection that assesses nothing cannot be acted on downstream."""
        task = Task("a", "T", "O", (), ("first", "second"))
        full = [{"criterion": 1, "satisfied": True, "evidence": "e"},
                {"criterion": 2, "satisfied": False, "evidence": "e"}]
        finding = [{"criterion": 2, "location": "value.txt", "finding": "wrong"}]
        for acceptance, why in (([], "empty map"), (full[:1], "partial map")):
            with self.subTest(why=why), self.assertRaisesRegex(ContractError, "every acceptance criterion"):
                validate_result(self.review(acceptance, finding), task, review=True)
        validate_result(self.review(full, finding), task, review=True)

    def test_a_conceded_rejection_stays_valid(self):
        """Every criterion satisfied plus a finding is the case Stage 3 acts on.

        Refusing it here would turn a conceded rejection into a contract error
        and make that stage unreachable, so the contract permits it on purpose.
        """
        task = Task("a", "T", "O", (), ("only",))
        conceded = self.review([{"criterion": 1, "satisfied": True, "evidence": "e"}],
                               [{"criterion": 1, "location": "README.md", "finding": "undocumented"}])
        self.assertEqual(validate_result(conceded, task, review=True)["verdict"], "request_changes")

    def test_findings_must_bind_to_a_criterion_of_this_ticket(self):
        task = Task("a", "T", "O", (), ("only",))
        full = [{"criterion": 1, "satisfied": False, "evidence": "e"}]
        for finding, pattern in (
                ([{"criterion": 9, "location": "x", "finding": "f"}], "criterion of this ticket"),
                (["a bare string"], "must be an object"),
                ([{"criterion": 1, "location": "/etc/passwd", "finding": "f"}], "relative path"),
                ([{"criterion": 1, "location": "x:0", "finding": "f"}], "line must be positive")):
            with self.subTest(finding=finding), self.assertRaises(ContractError) as caught:
                validate_result(self.review(full, finding), task, review=True)
            self.assertRegex(str(caught.exception), pattern)

    def test_a_phantom_location_fails_the_run_and_names_itself(self):
        """A location that does not resolve is not evidence, so it is refused."""
        class Phantom(FakeRunner):
            def run(self, **kwargs):
                outcome = super().run(**kwargs)
                if kwargs.get("read_only"):
                    outcome.update(verdict="request_changes",
                                   acceptance=[{"criterion": 1, "satisfied": False, "evidence": "e"}],
                                   findings=[{"criterion": 1, "location": "src/invented.py:5",
                                              "finding": "missing"}])
                return outcome
        result = run_serial(self.config, runner=Phantom())
        # Blocked, not failed: the run is not broken, a review produced
        # something the supervisor cannot act on. The finding is still refused.
        self.assertEqual(result["status"], "blocked")
        self.assertIn("src/invented.py:5", result["error"])
        self.assertIn("does not exist", result["error"])
        self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)
        blocked = next(task for task in result["tasks"] if task["status"] == "blocked")
        self.assertEqual(blocked["details"]["failure_category"], "unresolvable_location")
        # The review survives the stop; it is the evidence of the bad citation.
        self.assertEqual(blocked["details"]["review"]["findings"][0]["location"],
                         "src/invented.py:5")

    def test_a_finding_may_name_a_line_range_and_resolves_at_its_first_line(self):
        """A reviewer writes a span for a finding covering several lines.

        Refusing the notation ends the run before the rejection can even be
        classified: run e497e629 was lost that way, on a finding that was
        correct. One line is all the supervisor needs to prove the location
        exists at the reviewed revision.
        """
        from anvil.evidence import _location
        self.assertEqual(_location("src/anvil/store.py:413-418"), ("src/anvil/store.py", 413))
        self.assertEqual(_location("src/anvil/store.py:413"), ("src/anvil/store.py", 413))
        # A hyphen that is part of a name, and a range that is not one, are
        # unchanged: they stay whole paths for assert_located to resolve.
        for value in ("docs/a-b.md", "src/x.py:0-5", "src/x.py:9-3", "src/x.py:a-b"):
            self.assertEqual(_location(value), (value, None))

    def test_a_line_range_past_the_end_of_a_real_file_is_still_refused(self):
        class PastEndRange(FakeRunner):
            def run(self, **kwargs):
                outcome = super().run(**kwargs)
                if kwargs.get("read_only"):
                    outcome.update(verdict="request_changes",
                                   acceptance=[{"criterion": 1, "satisfied": False, "evidence": "e"}],
                                   findings=[{"criterion": 1, "location": "value.txt:9999-10000",
                                              "finding": "missing"}])
                return outcome
        result = run_serial(self.config, runner=PastEndRange())
        self.assertEqual(result["status"], "blocked")
        # The range resolved, so the refusal is the precise one a bare line
        # gets rather than the blunt "path does not exist" it used to be.
        self.assertIn("value.txt:9999-10000", result["error"])
        self.assertIn("that file has 1 lines", result["error"])
        self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)

    def test_a_line_past_the_end_of_a_real_file_is_refused(self):
        class PastEnd(FakeRunner):
            def run(self, **kwargs):
                outcome = super().run(**kwargs)
                if kwargs.get("read_only"):
                    outcome.update(verdict="request_changes",
                                   acceptance=[{"criterion": 1, "satisfied": False, "evidence": "e"}],
                                   findings=[{"criterion": 1, "location": "value.txt:9999",
                                              "finding": "missing"}])
                return outcome
        result = run_serial(self.config, runner=PastEnd())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("9999", result["error"])
        self.assertIn("lines at", result["error"])

    def test_a_rejection_reports_its_criterion_and_place(self):
        """The run error names the criterion and the file, not a prose blob."""
        result = run_serial(self.config, runner=FakeRunner("review-rejects"))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"],
                         "criterion 1 (value.txt:1): The change does not meet the ticket")
        self.assertEqual(format_findings(
            result["tasks"][0]["details"]["review"]["findings"]), result["error"])

    # -- Stage 3: a conceded rejection stops for the author -----------------

    class Conceded(FakeRunner):
        """Rejects while granting every criterion the ticket states."""
        def run(self, **kwargs):
            outcome = super().run(**kwargs)
            if kwargs.get("read_only"):
                outcome.update(verdict="request_changes",
                               findings=[{"criterion": 1, "location": "README.md",
                                          "finding": "the new field is undocumented"}])
            return outcome

    def test_a_conceded_rejection_stops_for_the_author(self):
        result = run_serial(self.config, runner=self.Conceded())
        self.assertEqual(result["status"], "blocked")
        details = result["tasks"][0]["details"]
        self.assertEqual(details["failure_category"], "unlisted_requirement")
        self.assertIn("every acceptance criterion is satisfied", result["error"])
        self.assertIn("criterion 1 (README.md): the new field is undocumented", result["error"])
        # The candidate is not accepted, and the branch does not move.
        self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)
        self.assertNotIn("integrated_sha", details)

    def test_an_ordinary_rejection_is_not_treated_as_conceded(self):
        result = run_serial(self.config, runner=FakeRunner("review-rejects"))
        self.assertEqual(result["status"], "blocked")
        details = result["tasks"][0]["details"]
        self.assertNotIn("failure_category", details)
        self.assertNotIn("every acceptance criterion is satisfied", result["error"])

    # -- Stage 4: declared sites (docs/ACCEPTANCE.md) -----------------------

    def tickets_with_sites(self, paths):
        doc = json.loads(self.tickets.read_text())
        doc["tasks"][0]["sites"] = [{"criterion": 1, "paths": list(paths)}]
        doc["tasks"] = doc["tasks"][:1]
        self.tickets.write_text(json.dumps(doc))

    class RejectsAt(FakeRunner):
        """Rejects one criterion at a caller-chosen location."""
        location = "value.txt:1"

        def run(self, **kwargs):
            outcome = super().run(**kwargs)
            if kwargs.get("read_only"):
                outcome.update(verdict="request_changes",
                               acceptance=[{"criterion": 1, "satisfied": False, "evidence": "e"}],
                               findings=[{"criterion": 1, "location": self.location,
                                          "finding": "the behavior is missing"}])
            return outcome

    def test_a_declared_site_must_exist_at_the_base_revision(self):
        """A stale or mistyped enumeration fails before the run spends anything.

        It is an authoring error in the ticket, so it raises before a run
        directory or a ledger exists, the way an invalid ticket graph does.
        """
        self.tickets_with_sites(["src/never_existed.py"])
        runner = FakeRunner()
        with self.assertRaises(ContractError) as caught:
            run_serial(self.config, runner=runner)
        self.assertIn("src/never_existed.py", str(caught.exception))
        self.assertIn("does not exist at", str(caught.exception))
        self.assertEqual(runner.workers, [])
        self.assertFalse(list((self.root / "state").glob("*/state.sqlite")))

    def test_a_finding_outside_the_declared_sites_returns_to_the_author(self):
        self.tickets_with_sites(["README.md"])
        runner = self.RejectsAt()
        runner.location = "value.txt:1"
        result = run_serial(self.config, runner=runner)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["tasks"][0]["details"]["failure_category"], "undeclared_site")
        self.assertIn("the ticket does not declare value.txt:1", result["error"])

    def test_a_finding_inside_the_declared_sites_is_an_ordinary_rejection(self):
        """The contrast: declaring a set must not turn every rejection into a
        scope decision, or the stop would be indistinguishable from noise."""
        # A site must exist at the base revision, so it names a file the ticket
        # works on rather than one it creates.
        self.tickets_with_sites(["README.md"])
        runner = self.RejectsAt()
        runner.location = "README.md:1"
        result = run_serial(self.config, runner=runner)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("failure_category", result["tasks"][0]["details"])
        self.assertNotIn("does not declare", result["error"])

    def test_a_directory_site_contains_the_files_beneath_it(self):
        self.tickets_with_sites(["src"])
        runner = self.RejectsAt()
        runner.location = "src/anvil/cli.py:1"
        (self.repo / "src" / "anvil").mkdir(parents=True)
        (self.repo / "src" / "anvil" / "cli.py").write_text("x\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=T", "-c", "user.email=t@e.invalid", "commit", "-qm", "src")
        self.base = git(self.repo, "rev-parse", "HEAD")
        result = run_serial(self.config, runner=runner)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("failure_category", result["tasks"][0]["details"])

    def test_a_criterion_declaring_nothing_stays_unbounded(self):
        runner = self.RejectsAt()
        runner.location = "README.md:1"
        result = run_serial(self.config, runner=runner)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("failure_category", result["tasks"][0]["details"])

    # -- retained candidate refs (tickets/candidate-retention.json) ---------

    def assert_retained(self, result, kinds=("candidate", "integration")):
        """Every named revision resolves and survives an immediate prune."""
        attempt = result["attempts"][0]["id"]
        run = result["run_id"]
        prunable = git(self.repo, "prune", "--dry-run", "--expire=now")
        for kind in kinds:
            ref = f"refs/anvil/{kind}/{run}/{attempt}"
            sha = git(self.repo, "rev-parse", ref)
            self.assertEqual(len(sha), 40, f"{ref} does not resolve")
            self.assertNotIn(sha, prunable, f"{ref} names a revision git would prune")
        return attempt

    def test_an_accepted_ticket_keeps_its_candidate_and_integration_revisions(self):
        result = run_serial(self.config, runner=FakeRunner())
        self.assertEqual(result["status"], "success", result.get("error"))
        attempt = self.assert_retained(result)
        details = result["attempts"][0]["details"]
        self.assertEqual(git(self.repo, "rev-parse",
                             f"refs/anvil/candidate/{result['run_id']}/{attempt}"),
                         details["candidate_sha"])

    def test_a_rejected_candidate_survives_the_run_that_discarded_it(self):
        """The case the refs exist for: nothing else references it afterwards."""
        result = run_serial(self.config, runner=FakeRunner("review-rejects"))
        self.assertEqual(result["status"], "blocked")
        attempt = self.assert_retained(result)
        rejected = result["attempts"][0]["details"]["candidate_sha"]
        # The managed branch never moved, so only the retained ref holds it.
        self.assertEqual(git(self.repo, "rev-parse", result["branch"]), self.base)
        self.assertEqual(git(self.repo, "rev-parse",
                             f"refs/anvil/candidate/{result['run_id']}/{attempt}"), rejected)
        # Every ref that reaches it is one Anvil retained. Which ones depends on
        # whether prepare_integration's cherry-pick reproduced the candidate's
        # sha, which it does only when both commits land in the same second.
        holders = git(self.repo, "for-each-ref", "--contains", rejected,
                      "--format=%(refname)").splitlines()
        self.assertTrue(holders)
        self.assertTrue(all(ref.startswith("refs/anvil/") for ref in holders), holders)

    def test_a_candidate_that_failed_its_checks_is_kept_too(self):
        result = run_serial(self.config, runner=FakeRunner("bad-check"))
        self.assertEqual(result["status"], "failed")
        self.assert_retained(result)

    def test_retained_refs_are_evidence_and_never_an_input(self):
        """Deleting every retained ref must not change what a run decides."""
        first = run_serial(self.config, runner=FakeRunner())
        self.assertEqual(first["status"], "success", first.get("error"))
        for ref in git(self.repo, "for-each-ref", "refs/anvil/",
                       "--format=%(refname)").splitlines():
            git(self.repo, "update-ref", "-d", ref)
        self.assertEqual(git(self.repo, "for-each-ref", "refs/anvil/"), "")
        second = run_serial(self.config, runner=FakeRunner())
        self.assertEqual(second["status"], "success", second.get("error"))

    # -- reaping retained refs (tickets/candidate-retention.json) -----------

    def retained(self, state_dir=None):
        from anvil.retention import listing
        return listing(self.repo, state_dir or self.config.state_dir)

    def test_listing_names_each_retained_ref_and_changes_nothing(self):
        accepted = run_serial(self.config, runner=FakeRunner())
        before = git(self.repo, "for-each-ref", "refs/anvil/")
        result = self.retained()
        self.assertEqual(git(self.repo, "for-each-ref", "refs/anvil/"), before)
        kinds = {(item["kind"], item["task_id"], item["accepted"]) for item in result["items"]}
        self.assertIn(("candidate", "t1", True), kinds)
        self.assertIn(("integration", "t2", True), kinds)
        self.assertEqual(result["runs"], [accepted["run_id"]])

    def test_a_rejected_attempt_is_listed_as_unaccepted(self):
        run_serial(self.config, runner=FakeRunner("review-rejects"))
        statuses = {item["accepted"] for item in self.retained()["items"]}
        self.assertEqual(statuses, {False})

    def test_deleting_one_run_leaves_another_runs_refs_alone(self):
        from anvil.retention import delete
        import shutil
        first = run_serial(self.config, runner=FakeRunner())
        second = run_serial(self.config, runner=FakeRunner())
        # A ref outlives its ledger only as evidence nothing else records, so
        # the run directory goes first; that is the reaping flow. It also keeps
        # this test off a timing-dependent detail: prepare_integration produces
        # a sha identical to the candidate's only when both commits land in the
        # same second, which decides whether the branch already holds it.
        shutil.rmtree(first["run_dir"])
        outcome = delete(self.repo, self.config.state_dir, first["run_id"])
        self.assertGreater(outcome["removed_count"], 0)
        self.assertEqual(outcome["refused"], [])
        surviving = {item["run_id"] for item in self.retained()["items"]}
        self.assertEqual(surviving, {second["run_id"]})
        # Deleting refs never moves the branch or touches the ledger.
        self.assertEqual(git(self.repo, "rev-parse", second["branch"]),
                         second["tasks"][-1]["details"]["integrated_sha"])
        self.assertEqual(RunStore.read(Path(second["run_dir"]) / "state.sqlite")["status"],
                         "success")

    def test_accepted_revisions_stay_reachable_after_their_refs_go(self):
        """The managed branch holds accepted work, so removing its ref is safe."""
        from anvil.retention import delete
        import shutil
        result = run_serial(self.config, runner=FakeRunner())
        integrated = result["tasks"][-1]["details"]["integrated_sha"]
        shutil.rmtree(result["run_dir"])
        delete(self.repo, self.config.state_dir, result["run_id"])
        self.assertNotIn(integrated, git(self.repo, "prune", "--dry-run", "--expire=now"))

    def test_delete_refuses_to_strand_a_revision_the_ledger_records(self):
        """A rejected candidate reaches nothing else, and the ledger names it."""
        from anvil.retention import delete
        result = run_serial(self.config, runner=FakeRunner("review-rejects"))
        rejected = result["attempts"][0]["details"]["candidate_sha"]
        outcome = delete(self.repo, self.config.state_dir, result["run_id"])
        self.assertEqual(outcome["removed_count"], 0)
        self.assertEqual({item["revision"] for item in outcome["refused"]},
                         {rejected, result["attempts"][0]["details"]["integration_sha"]})
        self.assertEqual(git(self.repo, "rev-parse",
                             f"refs/anvil/candidate/{result['run_id']}/"
                             f"{result['attempts'][0]['id']}"), rejected)

    def test_a_run_whose_ledger_is_gone_can_be_reaped(self):
        """Without a ledger nothing records the revision, so nothing is stranded."""
        from anvil.retention import delete
        import shutil
        result = run_serial(self.config, runner=FakeRunner("review-rejects"))
        shutil.rmtree(result["run_dir"])
        outcome = delete(self.repo, self.config.state_dir, result["run_id"])
        self.assertGreater(outcome["removed_count"], 0)
        self.assertEqual(outcome["refused"], [])
        self.assertEqual(self.retained()["items"], [])

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
                # A failing check stops before review; a passing one goes on to it.
                self.assertEqual(len(runner.reviews), 0 if mode == "bad-check" else 1)
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

    def test_ticket_skills_require_an_installed_catalog(self):
        document = json.loads(self.tickets.read_text())
        document["tasks"][0]["skills"] = ["implement"]
        self.tickets.write_text(json.dumps(document))
        with self.assertRaisesRegex(ContractError, "AI Hero installation"):
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
