"""Execution and status command boundaries; no live model workers."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from anvil.cli import main
from anvil.config import RunConfig, WorkerConfig
from anvil.contracts import ContractError, Task
from anvil.store import RunStore, StoreError
from anvil.workspaces import WorkspaceError


class RunCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config_path = self.root / "run.json"
        self.config = RunConfig(self.root / "repo", self.root / "tickets.json",
                                (("check", "--all"),), self.root / "state")

    def invoke(self, arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def report(self, status):
        return {"run_id": "saved-run", "status": status, "branch": "anvil/saved-run",
                "run_dir": str(self.root / "saved-run"),
                "tasks": [{"id": "a", "status": "done" if status == "success" else status}],
                "error": None if status == "success" else "required check did not complete"}

    def test_run_exit_codes_and_progress_do_not_contaminate_json_output(self):
        for status, expected in (("success", 0), ("failed", 1), ("blocked", 3), ("interrupted", 130)):
            report = self.report(status)

            def execute(config, *, progress):
                self.assertIs(config, self.config)
                progress("a: verifying the candidate")
                return report

            with self.subTest(status=status), patch("anvil.config.RunConfig.load", return_value=self.config) as load:
                with patch("anvil.execution.run_serial", side_effect=execute) as run:
                    code, stdout, stderr = self.invoke(["run", str(self.config_path), "--json"])
                load.assert_called_once_with(self.config_path)
                run.assert_called_once()
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(stdout), report)
                self.assertEqual(stderr, "a: verifying the candidate\n")

    def test_run_text_output_reports_saved_failure_and_returns_nonzero(self):
        with patch("anvil.config.RunConfig.load", return_value=self.config):
            with patch("anvil.execution.run_serial", return_value=self.report("failed")):
                code, stdout, stderr = self.invoke(["run", str(self.config_path)])
        self.assertEqual(code, 1)
        self.assertIn("Run saved-run: failed", stdout)
        self.assertIn("a: failed", stdout)
        self.assertIn("Integration branch: anvil/saved-run", stdout)
        self.assertIn(str(self.root / "saved-run"), stdout)
        self.assertIn("required check did not complete", stdout)
        self.assertEqual(stderr, "")

    def test_worker_pool_configuration_routes_to_coordinator(self):
        from dataclasses import replace
        config = replace(self.config, workers=(WorkerConfig("codex", "codex"),
                                               WorkerConfig("claude", "claude-code")))
        report = self.report("success")
        with patch("anvil.config.RunConfig.load", return_value=config), \
                patch("anvil.parallel.run_parallel", return_value=report) as parallel, \
                patch("anvil.execution.run_serial") as serial:
            code, stdout, stderr = self.invoke(["run", str(self.config_path), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), report)
        self.assertEqual(stderr, "")
        self.assertIs(parallel.call_args.args[0], config)
        serial.assert_not_called()

    def test_invalid_run_configuration_returns_error_before_any_worker_or_state(self):
        documents = [
            {"version": 1, "repo": "repo", "tickets": "tickets.json", "verification": "check --all"},
            {"version": 1, "repo": "repo", "tickets": "tickets.json", "verification": [["check"]],
             "agent_timeout": 10**400},
        ]
        for document in documents:
            with self.subTest(document=document):
                self.config_path.write_text(json.dumps(document), encoding="utf-8")
                with patch("anvil.execution.run_serial") as run:
                    code, stdout, stderr = self.invoke(["run", str(self.config_path), "--json"])
                run.assert_not_called()
                self.assertEqual(code, 2)
                self.assertIn("error", json.loads(stdout))
                self.assertEqual(stderr, "")
                self.assertFalse(self.config.state_dir.exists())

    def test_run_preflight_failures_are_descriptive_errors_without_tracebacks(self):
        for failure in (ContractError("invalid input"), WorkspaceError("repository is locked"),
                        StoreError("ledger unavailable"), OSError("cannot write state")):
            for as_json in (False, True):
                with self.subTest(failure=type(failure).__name__, as_json=as_json):
                    with patch("anvil.config.RunConfig.load", return_value=self.config):
                        with patch("anvil.execution.run_serial", side_effect=failure):
                            code, stdout, stderr = self.invoke(
                                ["run", str(self.config_path)] + (["--json"] if as_json else []))
                    self.assertEqual(code, 2)
                    if as_json:
                        self.assertEqual(json.loads(stdout), {"error": str(failure)})
                        self.assertEqual(stderr, "")
                    else:
                        self.assertEqual(stdout, "")
                        self.assertEqual(stderr, f"anvil: {failure}\n")

    def test_unsupported_execution_platform_fails_before_loading_or_running(self):
        with patch("anvil.cli.os", SimpleNamespace(name="nt")):
            with patch("anvil.config.RunConfig.load") as load, patch("anvil.execution.run_serial") as run:
                code, stdout, stderr = self.invoke(["run", str(self.config_path), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("macOS or Linux", json.loads(stdout)["error"])
        self.assertEqual(stderr, "")
        load.assert_not_called()
        run.assert_not_called()

    def test_status_missing_ledger_returns_error_and_creates_nothing(self):
        run_dir = self.root / "missing" / "run"
        for as_json in (False, True):
            with self.subTest(as_json=as_json), patch("anvil.execution.run_serial") as run:
                code, stdout, stderr = self.invoke(
                    ["status", str(run_dir)] + (["--json"] if as_json else []))
            run.assert_not_called()
            self.assertEqual(code, 2)
            if as_json:
                self.assertIn("cannot read run ledger", json.loads(stdout)["error"])
                self.assertEqual(stderr, "")
            else:
                self.assertEqual(stdout, "")
                self.assertIn("anvil: cannot read run ledger", stderr)
            self.assertFalse(run_dir.parent.exists())

    def test_status_reads_failed_run_without_resuming_or_changing_saved_state(self):
        run_dir = self.root / "failed-run"
        path = run_dir / "state.sqlite"
        with RunStore(path) as store:
            store.initialize(run_id="failed-run", repo="/repo", branch="anvil/failed-run",
                             base_sha="base", tasks=(Task("a", "A", "Build A", (), ("A works",)),), config={})
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/worker")
            store.transition("a", "failed", attempt_id=attempt, details={"error": "required check failed"})
            store.set_run("failed", "required check failed")
        before = path.read_bytes()
        with patch("anvil.execution.run_serial") as run, patch("anvil.config.RunConfig.load") as load:
            code, stdout, stderr = self.invoke(["status", str(run_dir), "--json"])
        self.assertEqual(code, 0)
        snapshot = json.loads(stdout)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["tasks"][0]["status"], "failed")
        self.assertEqual(snapshot["error"], "required check failed")
        self.assertEqual(snapshot["run_dir"], str(run_dir))
        self.assertEqual(stderr, "")
        self.assertEqual(path.read_bytes(), before)
        # The ledger is a write-ahead log database. Reading one needs its
        # shared-memory index, and SQLite creates that file when it is absent,
        # so a read can add state.sqlite-shm and state.sqlite-wal beside the
        # ledger. Whether they are already there when status runs differs by
        # platform. What must hold is that status resumes nothing, writes no
        # run artifacts, and leaves the ledger's own bytes untouched.
        self.assertEqual(
            sorted(item.name for item in run_dir.iterdir()
                   if not item.name.startswith("state.sqlite-")),
            ["state.sqlite"])
        run.assert_not_called()
        load.assert_not_called()

    def test_status_corrupt_ledger_reports_error_without_overwriting_it(self):
        run_dir = self.root / "corrupt"
        run_dir.mkdir()
        path = run_dir / "state.sqlite"
        path.write_bytes(b"not a SQLite database")
        code, stdout, stderr = self.invoke(["status", str(run_dir), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read run ledger", json.loads(stdout)["error"])
        self.assertEqual(stderr, "")
        self.assertEqual(path.read_bytes(), b"not a SQLite database")


if __name__ == "__main__":
    unittest.main()
