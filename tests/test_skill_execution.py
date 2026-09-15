"""Pinned ticket skill context across supported worker adapters and recovery."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from anvil.config import WorkerConfig
from anvil.contracts import ContractError
from anvil.execution import run_serial
from anvil.parallel import run_parallel
from anvil.recovery import resume
from anvil.skill_management import SkillScope, install
from anvil.skill_source import Catalog, Skill, SourceFile
from anvil.store import RunStore
import test_execution
from test_execution import FakeRunner, git


class CaptureRunner(FakeRunner):
    def __init__(self, mode="success"):
        super().__init__(mode)
        self.worker_prompts = []
        self.review_prompts = []

    def run(self, **kwargs):
        target = self.review_prompts if kwargs.get("read_only") else self.worker_prompts
        target.append(kwargs["prompt"])
        return super().run(**kwargs)


class TicketSkillExecutionTests(unittest.TestCase):
    setUp = test_execution.SerialExecutionTests.setUp

    def install_skills(self, *, binary=False, oversized=False):
        (self.repo / ".gitignore").write_text(".agents/\n.claude/\n.anvil/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Ignore managed skills")
        self.base = git(self.repo, "rev-parse", "HEAD")
        skills = {}
        for name in ("tdd", "research"):
            files = {
                "SKILL.md": SourceFile((f"---\nname: {name}\ndescription: Fixture\n---\n"
                                         f"Read references/{name}.md before working.\n").encode()),
                f"references/{name}.md": SourceFile(f"{name} exact guidance\n".encode()),
                "LICENSE.aihero": SourceFile(b"MIT fixture\n"),
            }
            if binary and name == "tdd":
                files["assets/blob.bin"] = SourceFile(b"\x00\xff")
            if oversized and name == "tdd":
                files["references/large.md"] = SourceFile(b"x" * (512 * 1024 + 1))
            skills[name] = Skill(f"skills/engineering/{name}", files)
        catalog = Catalog("a" * 40, skills)
        with patch("anvil.skill_management.fetch_catalog", return_value=catalog):
            install(SkillScope(self.repo), agents=("codex",))

    def select(self, first=("tdd",), *, one_task=True):
        document = json.loads(self.tickets.read_text())
        if one_task:
            document["tasks"] = document["tasks"][:1]
        document["tasks"][0]["skills"] = list(first)
        self.tickets.write_text(json.dumps(document))

    def test_serial_pins_exact_context_and_records_attempt_evidence(self):
        self.install_skills()
        self.select()
        runner = CaptureRunner()
        result = run_serial(self.config, runner=runner)
        self.assertEqual(result["status"], "success", result["error"])
        prompt = runner.worker_prompts[0]
        self.assertIn("Revision: " + "a" * 40, prompt)
        self.assertIn("BEGIN SKILL FILE tdd/SKILL.md", prompt)
        self.assertIn("tdd exact guidance", prompt)
        self.assertNotIn("research exact guidance", prompt)
        self.assertNotIn("BEGIN SKILL FILE", runner.review_prompts[0])
        run_dir = Path(result["run_dir"])
        self.assertEqual((run_dir / "skills/tdd/SKILL.md").read_bytes(),
                         (self.repo / ".agents/skills/tdd/SKILL.md").read_bytes())
        events = RunStore.read(run_dir / "state.sqlite")["events"]
        catalog = [event for event in events if event["kind"] == "skill_catalog"]
        attempts = [event for event in events
                    if event["details"].get("message_kind") == "skill_context"]
        self.assertEqual(catalog[0]["details"]["skills"], ["tdd"])
        self.assertEqual(attempts[0]["details"]["body"]["skills"], ["tdd"])

    def test_agent_neutral_context_supports_claude_muse_and_mixed_workers(self):
        self.install_skills()
        original = json.loads(self.tickets.read_text())
        for agent in ("claude-code", "muse"):
            with self.subTest(agent=agent):
                self.tickets.write_text(json.dumps(original))
                self.select()
                runner = CaptureRunner()
                config = replace(self.config, agent=agent,
                                 agent_binary="claude" if agent == "claude-code" else "muse")
                result = run_serial(config, runner=runner)
                self.assertEqual(result["status"], "success", result["error"])
                self.assertIn("tdd exact guidance", runner.worker_prompts[0])

        document = original
        document["tasks"][0]["skills"] = ["tdd"]
        document["tasks"][0]["worker"] = "a"
        document["tasks"][1]["skills"] = ["research"]
        document["tasks"][1]["worker"] = "b"
        self.tickets.write_text(json.dumps(document))
        a, b, review = CaptureRunner(), CaptureRunner(), CaptureRunner()
        config = replace(self.config, workers=(WorkerConfig("a", "claude-code"),
                                               WorkerConfig("b", "muse")), max_processes=2)
        result = run_parallel(config, runners={"a": a, "b": b}, review_runner=review)
        self.assertEqual(result["status"], "success", result["error"])
        prompts = a.worker_prompts + b.worker_prompts
        self.assertEqual(len(prompts), 2)
        self.assertTrue(any("tdd exact guidance" in prompt for prompt in prompts))
        self.assertTrue(any("research exact guidance" in prompt for prompt in prompts))

    def test_missing_unknown_and_modified_skills_fail_before_agent_work(self):
        self.select()
        runner = CaptureRunner()
        with self.assertRaisesRegex(ContractError, "installation"):
            run_serial(self.config, runner=runner)
        self.assertEqual(runner.worker_prompts, [])

        self.install_skills()
        self.select(("unknown",))
        with self.assertRaisesRegex(ContractError, "absent"):
            run_serial(self.config, runner=runner)
        self.select()
        (self.repo / ".agents/skills/tdd/SKILL.md").write_text("changed")
        with self.assertRaisesRegex(ContractError, "local changes"):
            run_serial(self.config, runner=runner)

    def test_binary_skill_resource_fails_before_agent_work(self):
        self.install_skills(binary=True)
        self.select()
        runner = CaptureRunner()
        with self.assertRaisesRegex(ContractError, "UTF-8"):
            run_serial(self.config, runner=runner)
        self.assertEqual(runner.worker_prompts, [])

    def test_oversized_skill_context_fails_before_agent_work(self):
        self.install_skills(oversized=True)
        self.select()
        runner = CaptureRunner()
        with self.assertRaisesRegex(ContractError, "512 KiB"):
            run_serial(self.config, runner=runner)
        self.assertEqual(runner.worker_prompts, [])

    def test_skill_can_report_a_missing_human_decision_as_blocked(self):
        self.install_skills()
        self.select()
        runner = CaptureRunner("blocked")
        result = run_serial(self.config, runner=runner)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["tasks"][0]["status"], "blocked")
        self.assertIn("Clarify the output format", result["error"])

    def test_resume_uses_pinned_snapshot_after_ignored_installation_changes(self):
        self.install_skills()
        self.select()
        first = run_serial(self.config, runner=CaptureRunner("interrupt"))
        self.assertEqual(first["status"], "interrupted")
        (self.repo / ".agents/skills/tdd/references/tdd.md").write_text("later installation change\n")
        runner = CaptureRunner()
        result = resume(Path(first["run_dir"]), runners={"serial": runner}, review_runner=runner)
        self.assertEqual(result["status"], "success", result["error"])
        self.assertIn("tdd exact guidance", runner.worker_prompts[0])
        self.assertNotIn("later installation change", runner.worker_prompts[0])

    def test_resume_rejects_changed_pinned_skill_bytes(self):
        self.install_skills()
        self.select()
        first = run_serial(self.config, runner=CaptureRunner("interrupt"))
        pinned = Path(first["run_dir"]) / "skills/tdd/references/tdd.md"
        pinned.write_text("tampered\n")
        runner = CaptureRunner()
        with self.assertRaisesRegex(ContractError, "changed or is incomplete"):
            resume(Path(first["run_dir"]), runners={"serial": runner}, review_runner=runner)
        self.assertEqual(runner.worker_prompts, [])


if __name__ == "__main__":
    unittest.main()
