"""Pinned ticket skill context across supported worker adapters and recovery."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from anvil.config import WorkerConfig
from anvil.contracts import ContractError, Task
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


class SkillRegistryTests(unittest.TestCase):
    def test_bundled_registry_classifies_the_reviewed_stable_catalog(self):
        from anvil.skill_runtime import _registry
        registry = _registry()
        self.assertEqual(registry["version"], 1)
        self.assertEqual(len(registry["entries"]), 25)
        self.assertEqual(registry["entries"]["tdd"]["requires"], ["shell"])
        self.assertEqual(registry["entries"]["writing-for-agents"]["requires"], [])
        self.assertTrue(registry["entries"]["implement"]["adaptations"])


class TicketSkillExecutionTests(unittest.TestCase):
    def setUp(self):
        test_execution.SerialExecutionTests.setUp(self)
        registry = patch("anvil.skill_runtime._registry", side_effect=self.compatibility_registry)
        registry.start()
        self.addCleanup(registry.stop)

    def compatibility_registry(self):
        entries = {}
        requirements = {"tdd": ["shell"], "research": ["network", "subagents"],
                        "implement": [], "writing-for-agents": []}
        adaptations = {"implement": ["Anvil owns commits and verification."]}
        for name, required in requirements.items():
            skill = self.repo / ".agents/skills" / name / "SKILL.md"
            data = skill.read_bytes() if skill.exists() else b""
            entries[name] = {"sha256": hashlib.sha256(data).hexdigest(),
                             "requires": required, "adaptations": adaptations.get(name, [])}
        return {"version": 1, "source": "https://github.com/mattpocock/skills",
                "entries": entries}

    def install_skills(self, *, binary=False, oversized=False):
        (self.repo / ".gitignore").write_text(".agents/\n.claude/\n.anvil/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Ignore managed skills")
        self.base = git(self.repo, "rev-parse", "HEAD")
        skills = {}
        for name in ("tdd", "research", "implement", "writing-for-agents"):
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

    def test_capability_preflight_routes_shell_skill_to_codex(self):
        self.install_skills()
        original = json.loads(self.tickets.read_text())
        for agent in ("claude-code", "muse"):
            with self.subTest(agent=agent):
                self.tickets.write_text(json.dumps(original))
                self.select()
                runner = CaptureRunner()
                config = replace(self.config, agent=agent,
                                 agent_binary="claude" if agent == "claude-code" else "muse")
                with self.assertRaisesRegex(ContractError, "missing capabilities: shell"):
                    run_serial(config, runner=runner)
                self.assertEqual(runner.worker_prompts, [])

        document = original
        document["tasks"] = document["tasks"][:1]
        document["tasks"][0]["skills"] = ["tdd"]
        self.tickets.write_text(json.dumps(document))
        a, b, review = CaptureRunner(), CaptureRunner(), CaptureRunner()
        config = replace(self.config, workers=(WorkerConfig("a", "claude-code"),
                                               WorkerConfig("b", "codex")), max_processes=2)
        result = run_parallel(config, runners={"a": a, "b": b}, review_runner=review)
        self.assertEqual(result["status"], "success", result["error"])
        self.assertEqual(a.worker_prompts, [])
        self.assertIn("tdd exact guidance", b.worker_prompts[0])
        evidence = [event["details"]["body"] for event in result["events"]
                    if event["details"].get("message_kind") == "skill_context"]
        self.assertEqual(evidence[0]["preflight"]["agent"], "codex")
        self.assertTrue(evidence[0]["preflight"]["compatible"])

    def test_explicit_incompatible_worker_fails_before_agent_or_baseline(self):
        self.install_skills()
        document = json.loads(self.tickets.read_text())
        document["tasks"] = document["tasks"][:1]
        document["tasks"][0]["skills"] = ["tdd"]
        document["tasks"][0]["worker"] = "a"
        self.tickets.write_text(json.dumps(document))
        a, b, review = CaptureRunner(), CaptureRunner(), CaptureRunner()
        marker = self.root / "baseline-ran"
        config = replace(self.config, workers=(WorkerConfig("a", "claude-code"),
                                               WorkerConfig("b", "codex")), max_processes=2,
                         verification=((sys.executable, "-c",
                                        f"from pathlib import Path; Path({str(marker)!r}).touch()"),))
        with self.assertRaisesRegex(ContractError, "missing capabilities: shell"):
            run_parallel(config, runners={"a": a, "b": b}, review_runner=review)
        self.assertEqual(a.worker_prompts + b.worker_prompts + review.worker_prompts, [])
        self.assertFalse(marker.exists())

    def test_file_only_skill_and_anvil_adaptation_are_delivered(self):
        self.install_skills()
        self.select(("writing-for-agents",))
        for agent in ("claude-code", "muse"):
            with self.subTest(agent=agent):
                runner = CaptureRunner()
                config = replace(self.config, agent=agent,
                                 agent_binary="claude" if agent == "claude-code" else "muse")
                result = run_serial(config, runner=runner)
                self.assertEqual(result["status"], "success", result["error"])
                self.assertIn("writing-for-agents exact guidance", runner.worker_prompts[0])

        self.select(("implement",))
        runner = CaptureRunner()
        result = run_serial(self.config, runner=runner)
        self.assertEqual(result["status"], "success", result["error"])
        self.assertIn("Anvil compatibility adaptations", runner.worker_prompts[0])
        self.assertIn("Anvil owns commits and verification.", runner.worker_prompts[0])

    def test_capability_vocabulary_is_reported_as_one_actionable_set(self):
        from anvil.skill_runtime import SkillContext
        required = ["network", "subagents", "human_dialogue", "issue_tracker",
                    "conversation_history", "git_control", "review_role"]
        context = SkillContext(compatibility={
            "fixture": {"requires": required, "adaptations": []}
        })
        task = Task("T-1", "Fixture", "Exercise requirements", (), ("Report gaps",),
                    skills=("fixture",))
        report = context.preflight(task, "codex")
        self.assertFalse(report["compatible"])
        self.assertEqual(report["missing"], sorted(required))
        with self.assertRaisesRegex(ContractError, "conversation_history.*review_role"):
            context.require(task, "codex")

    def test_unclassified_skill_hash_fails_before_agent_work(self):
        self.install_skills()
        self.select()
        runner = CaptureRunner()
        with patch("anvil.skill_runtime._registry", return_value={
                "version": 1, "source": "https://github.com/mattpocock/skills", "entries": {}}):
            with self.assertRaisesRegex(ContractError, "unclassified"):
                run_serial(self.config, runner=runner)
        self.assertEqual(runner.worker_prompts, [])

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
