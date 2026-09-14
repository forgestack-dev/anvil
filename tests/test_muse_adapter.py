"""Operator-handoff adapter checks. These tests never invoke a model worker.

The "operator" is simulated with a background thread that fulfills the staged
handoff by writing result.json, exactly as a Muse session driving `anvil run`
would. No CLI is launched and no credentials are used.
"""

from contextlib import redirect_stderr
from io import StringIO
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from anvil.adapters import AGENT_NAMES, EXECUTION_AGENTS, create_runner, probe_agent
from anvil.adapters.muse import MuseDoctor, MuseRunner, _read_result, build_handoff, doctor
from anvil.config import RunConfig, WorkerConfig
from anvil.contracts import ContractError
from anvil.processes import ProcessError


SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}}


def fulfill(handoff_dir: Path, payload: dict, delay: float = 0.2) -> threading.Thread:
    """Simulate the operator: wait for the staged request, then atomically write result.json."""
    def target():
        request = handoff_dir / "request.json"
        deadline = time.monotonic() + 30
        while not request.is_file():
            if time.monotonic() > deadline:
                raise AssertionError("handoff was never staged")
            time.sleep(0.05)
        time.sleep(delay)
        temporary = handoff_dir / "result.json.tmp"
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, handoff_dir / "result.json")

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="anvil muse ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()

    def stage(self, **overrides):
        args = dict(repo=self.repo, prompt="Implement the ticket",
                    schema=SCHEMA, artifact_dir=self.root / "artifacts")
        args.update(overrides)
        return build_handoff(**args)

    def test_stages_prompt_schema_and_request_without_waiting(self):
        handoff = self.stage()
        self.assertFalse((self.root / "artifacts" / "result.json").exists())
        self.assertEqual(handoff.prompt_path.read_text(encoding="utf-8"), "Implement the ticket\n")
        self.assertEqual(json.loads(handoff.schema_path.read_text(encoding="utf-8")), SCHEMA)
        request = json.loads(handoff.request_path.read_text(encoding="utf-8"))
        self.assertEqual(request["agent"], "muse")
        self.assertEqual(request["role"], "implement")
        self.assertEqual(request["repo"], str(self.repo))
        self.assertFalse(request["read_only"])
        self.assertEqual(request["timeout_seconds"], 900)
        self.assertIn("protocol", request)

    def test_review_role_is_recorded(self):
        handoff = self.stage(read_only=True, timeout=60)
        request = json.loads(handoff.request_path.read_text(encoding="utf-8"))
        self.assertEqual(request["role"], "review")
        self.assertTrue(request["read_only"])
        self.assertTrue(handoff.read_only)

    def test_prompt_newline_is_normalized(self):
        handoff = self.stage(prompt="Do it\n")
        self.assertEqual(handoff.prompt_path.read_text(encoding="utf-8"), "Do it\n")

    def test_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            self.stage(prompt="   ")
        with self.assertRaises(ValueError):
            self.stage(prompt="bad\0prompt")
        with self.assertRaises(ValueError):
            self.stage(schema=["not", "an", "object"])
        with self.assertRaises(ValueError):
            self.stage(read_only="yes")
        for timeout in (0, -5, float("inf"), float("nan"), True, "60"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    self.stage(timeout=timeout, artifact_dir=self.root / f"a-{timeout}")
        with self.assertRaises(ValueError):
            self.stage(repo=self.root / "missing", artifact_dir=self.root / "b1")
        (self.repo / ".git").rmdir()
        with self.assertRaisesRegex(ValueError, ".git"):
            self.stage(artifact_dir=self.root / "b2")

    def test_existing_artifact_dir_is_rejected(self):
        self.stage()
        with self.assertRaises(OSError):
            self.stage()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="anvil muse ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.runner = MuseRunner()

    def run_turn(self, **overrides):
        args = dict(repo=self.repo, prompt="Implement the ticket", schema=SCHEMA,
                    artifact_dir=self.root / "artifacts", timeout=10)
        args.update(overrides)
        return self.runner.run(**args)

    def test_operator_result_is_returned(self):
        thread = fulfill(self.root / "artifacts", {"status": "completed", "summary": "done"})
        with redirect_stderr(StringIO()):
            result = self.run_turn()
        thread.join(timeout=10)
        self.assertEqual(result, {"status": "completed", "summary": "done"})

    def test_handoff_is_announced_on_stderr(self):
        thread = fulfill(self.root / "artifacts", {"status": "completed"})
        captured = StringIO()
        with redirect_stderr(captured):
            self.run_turn(timeout=10)
        thread.join(timeout=10)
        announcement = captured.getvalue()
        self.assertIn("Anvil Muse handoff", announcement)
        self.assertIn(str(self.repo), announcement)
        self.assertIn("result.json", announcement)

    def test_timeout_fails_the_turn(self):
        with redirect_stderr(StringIO()):
            with self.assertRaisesRegex(ProcessError, "timed out"):
                self.run_turn(timeout=1)

    def test_invalid_results_are_rejected(self):
        handoff = build_handoff(self.repo, "prompt", SCHEMA, self.root / "artifacts", timeout=1)
        cases = {
            "not json": b"{oops",
            "non-object": b"[1, 2]",
            "duplicate keys": b'{"a": 1, "a": 2}',
            "non-finite": b'{"a": NaN}',
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                handoff.result_path.write_bytes(data)
                with self.assertRaises(ProcessError):
                    _read_result(handoff)
                handoff.result_path.unlink()
        handoff.result_path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ProcessError, "4 MiB"):
            _read_result(handoff)

    def test_symlinked_result_is_rejected(self):
        handoff = build_handoff(self.repo, "prompt", SCHEMA, self.root / "artifacts", timeout=1)
        target = self.root / "real.json"
        target.write_text('{"status": "completed"}', encoding="utf-8")
        handoff.result_path.symlink_to(target)
        with self.assertRaises(ProcessError):
            _read_result(handoff)

    def test_profiles_are_rejected(self):
        with self.assertRaises(ContractError):
            MuseRunner(profile={"model": "anything", "effort": "high"})

    def test_binary_label_must_be_nonempty_text(self):
        with self.assertRaises(ValueError):
            MuseRunner("")
        with self.assertRaises(ValueError):
            MuseRunner("bad\0label")
        self.assertEqual(MuseRunner("custom-label").muse_binary, "custom-label")


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_operator_handoff_without_probing(self):
        with patch("shutil.which", side_effect=AssertionError("no CLI lookup")) as which, \
             patch("subprocess.run", side_effect=AssertionError("no probes")):
            result = doctor("muse", probe=True)
        which.assert_not_called()
        self.assertIsInstance(result, MuseDoctor)
        self.assertIsNone(result.executable)
        self.assertTrue(result.compatible)
        self.assertIsNone(result.error)

    def test_probe_agent_supports_muse(self):
        result = probe_agent("muse")
        self.assertTrue(result.compatible)
        self.assertIsNone(result.executable)


class RegistryTests(unittest.TestCase):
    def test_create_runner_supports_muse(self):
        runner = create_runner("muse", "muse")
        self.assertIsInstance(runner, MuseRunner)

    def test_unknown_agents_still_rejected(self):
        with self.assertRaises(ContractError):
            create_runner("bogus", "bogus")
        with self.assertRaises(ContractError):
            probe_agent("bogus")

    def test_execution_agents_superset_skill_agents(self):
        self.assertEqual(AGENT_NAMES, ("codex", "claude-code"))
        self.assertEqual(EXECUTION_AGENTS, ("codex", "claude-code", "muse"))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="anvil muse config ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def parse(self, document):
        return RunConfig.from_document(document, base=self.root)

    def document(self, **overrides):
        config = {"version": 1, "repo": "target repo", "tickets": "tickets.json",
                  "verification": [["check", "--all"]], "state_dir": "run state"}
        config.update(overrides)
        return config

    def test_muse_agent_loads_with_label_executable(self):
        config = self.parse(self.document(agent="muse"))
        self.assertEqual(config.agent, "muse")
        self.assertEqual(config.agent_binary, "muse")
        self.assertEqual(config.executable, "muse")

    def test_muse_worker_slot_loads(self):
        config = self.parse(self.document(
            agent="codex", workers=[{"id": "muse-slot", "agent": "muse"}]))
        worker = config.workers[0]
        self.assertIsInstance(worker, WorkerConfig)
        self.assertEqual(worker.agent, "muse")
        self.assertEqual(worker.executable, "muse")

    def test_muse_config_round_trips(self):
        config = self.parse(self.document(agent="muse"))
        self.assertEqual(RunConfig.from_document(config.to_dict(), base=self.root), config)

    def test_existing_agents_unchanged(self):
        self.assertEqual(self.parse(self.document()).agent, "codex")
        self.assertEqual(self.parse(self.document(agent="claude-code")).agent_binary, "claude")
        with self.assertRaises(ContractError):
            self.parse(self.document(agent="bogus"))

    def test_legacy_codex_binary_rejected_for_muse(self):
        with self.assertRaisesRegex(ContractError, "codex_binary"):
            self.parse(self.document(agent="muse", codex_binary="./custom"))

    def test_worker_agent_validation(self):
        with self.assertRaises(ContractError):
            WorkerConfig("slot", "bogus")
        self.assertEqual(WorkerConfig("slot", "muse").agent_binary, "muse")


class CliTests(unittest.TestCase):
    def test_doctor_accepts_muse(self):
        from anvil.cli import main
        from io import StringIO
        captured = StringIO()
        with patch.object(sys, "stdout", captured), patch.object(sys, "stderr", StringIO()):
            code = main(["doctor", "--agent", "muse"])
        self.assertEqual(code, 0)
        self.assertIn("Muse", captured.getvalue())
        self.assertIn("no local CLI", captured.getvalue())

    def test_doctor_rejects_unknown_agent(self):
        from anvil.cli import main
        with patch.object(sys, "stderr", StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["doctor", "--agent", "bogus"])
        self.assertEqual(raised.exception.code, 2)

    def test_skills_install_still_limited_to_cli_agents(self):
        from anvil.cli import parser
        with patch.object(sys, "stderr", StringIO()):
            with self.assertRaises(SystemExit):
                parser().parse_args(["skills", "install", "aihero", "--agent", "muse"])
        arguments = parser().parse_args(["skills", "install", "aihero", "--agent", "both"])
        self.assertEqual(arguments.agent, "both")


if __name__ == "__main__":
    unittest.main()
