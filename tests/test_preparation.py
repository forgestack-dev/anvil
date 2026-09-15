"""Markdown specification preparation without live model calls."""

from contextlib import redirect_stderr, redirect_stdout
from contextlib import contextmanager
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anvil.cli import main
from anvil.contracts import ContractError
from anvil.planning import TaskGraph
from anvil.preparation import _skills, prepare, result_schema
from anvil.workspaces import WorkspaceError
from test_execution import git


def task(task_id="T-1", *, depends_on=(), skills=()):
    return {"id": task_id, "title": "Implement the capability",
            "objective": "Deliver the specified behavior.",
            "depends_on": list(depends_on),
            "acceptance_criteria": ["The observable behavior matches the specification."],
            "source_refs": ["### Capability"], "skills": list(skills),
            "resources": ["core"], "exclusive": False, "risk": "medium"}


class FakePlanner:
    def __init__(self, result=None):
        self.result = result or {"version": 1, "tasks": [task()]}
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        self.spec = self.repo / "SPEC.md"
        self.spec.write_text("# Product\n\n## Requirements\n\n### Capability\nShip it.\n")
        git(self.repo, "add", "SPEC.md")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Add specification")
        self.output = self.repo / "tickets.json"
        self.artifacts = self.root / "artifacts"

    def run_prepare(self, runner, *, skills=None):
        with patch("anvil.preparation._skills", return_value=(skills or [], [])):
            return prepare(self.spec, self.output, repo=self.repo, agent="claude-code",
                           artifact_root=self.artifacts, runner=runner)

    def test_prepares_valid_traceable_tickets_without_implementing(self):
        runner = FakePlanner()
        result = self.run_prepare(runner, skills=[{
            "name": "writing-for-agents", "requires": [],
            "compatible_agents": ["codex", "claude-code", "muse"]}])
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["task_count"], 1)
        document = json.loads(self.output.read_text())
        graph = TaskGraph.from_document(document)
        self.assertEqual(graph.tasks[0].source_refs, ("### Capability",))
        self.assertEqual(document["tasks"][0]["execution"], {"status": "todo"})
        provenance = document["provenance"]
        self.assertEqual(provenance["source"], "SPEC.md")
        self.assertEqual(provenance["source_sha256"], hashlib.sha256(self.spec.read_bytes()).hexdigest())
        self.assertEqual(provenance["repo_head"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(provenance["agent"], "claude-code")
        self.assertTrue(runner.calls[0]["read_only"])
        self.assertIn("do not edit files", runner.calls[0]["prompt"])
        self.assertIn("writing-for-agents", runner.calls[0]["prompt"])

    def test_invalid_or_unavailable_agent_output_never_writes_tickets(self):
        cases = [
            ({"version": 1, "tasks": [task("T-1", depends_on=("missing",))]}, "missing dependency"),
            ({"version": 1, "tasks": [task(skills=("tdd",))]}, "unavailable skills"),
            ({"version": 1, "tasks": [{k: v for k, v in task().items() if k != "source_refs"}]},
             "required ticket fields"),
            ({"version": 1, "tasks": [task() | {"source_refs": ["### Invented"]}]},
             "absent from the specification"),
            ({"version": 1, "tasks": [task(f"T-{number}") for number in range(101)]},
             "between 1 and 100"),
        ]
        for document, message in cases:
            with self.subTest(message=message):
                self.output.unlink(missing_ok=True)
                with self.assertRaisesRegex(ContractError, message):
                    self.run_prepare(FakePlanner(document))
                self.assertFalse(self.output.exists())

    def test_existing_output_and_dirty_repository_fail_before_planner(self):
        runner = FakePlanner()
        self.output.write_text("keep me")
        with self.assertRaises((ContractError, WorkspaceError)):
            self.run_prepare(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.output.read_text(), "keep me")

        self.output.unlink()
        (self.repo / "dirty.txt").write_text("dirty")
        with self.assertRaisesRegex(WorkspaceError, "uncommitted or untracked"):
            self.run_prepare(runner)
        self.assertEqual(runner.calls, [])

    def test_source_and_output_must_stay_inside_clean_repository(self):
        runner = FakePlanner()
        outside = self.root / "outside.md"
        outside.write_text("# Outside")
        with self.assertRaisesRegex(ContractError, "specification must be inside"):
            prepare(outside, self.output, repo=self.repo, artifact_root=self.artifacts,
                    runner=runner)
        with self.assertRaisesRegex(ContractError, "ticket output must be inside"):
            prepare(self.spec, self.root / "tickets.json", repo=self.repo,
                    artifact_root=self.artifacts, runner=runner)
        self.assertEqual(runner.calls, [])

        (self.repo / ".gitignore").write_text("ignored.md\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Ignore fixture")
        ignored = self.repo / "ignored.md"
        ignored.write_text("# Ignored")
        with self.assertRaisesRegex(ContractError, "must be committed"):
            prepare(ignored, self.output, repo=self.repo, artifact_root=self.artifacts,
                    runner=runner)
        self.assertEqual(runner.calls, [])

    def test_source_and_output_symlinks_are_rejected_before_planning(self):
        runner = FakePlanner()
        source_link = self.repo / "linked-spec.md"
        source_link.symlink_to(self.spec)
        output_target = self.repo / "generated.json"
        output_link = self.repo / "linked-output.json"
        output_link.symlink_to(output_target)
        git(self.repo, "add", "linked-spec.md", "linked-output.json")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "Add symlink fixtures")
        for source, output in ((source_link, self.output), (self.spec, output_link)):
            with self.subTest(path=source if source != self.spec else output), \
                    self.assertRaisesRegex(ContractError, "symlink"):
                prepare(source, output, repo=self.repo, artifact_root=self.artifacts,
                        runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertFalse(output_target.exists())

    def test_empty_skill_catalog_schema_requires_an_empty_selection(self):
        schema = result_schema([])
        skills = schema["properties"]["tasks"]["items"]["properties"]["skills"]
        self.assertEqual(skills["maxItems"], 0)
        self.assertNotIn("enum", skills["items"])

    def test_skill_choices_include_only_installed_classified_runnable_workflows(self):
        manifest = {"skills": {
            "writing-for-agents": {"files": {"SKILL.md": {"sha256": "a" * 64}}},
            "tdd": {"files": {"SKILL.md": {"sha256": "b" * 64}}},
            "wizard": {"files": {"SKILL.md": {"sha256": "c" * 64}}},
            "new-skill": {"files": {"SKILL.md": {"sha256": "d" * 64}}},
        }}

        @contextmanager
        def snapshot(scope):
            yield manifest

        registry = {"version": 1, "source": "https://github.com/mattpocock/skills",
                    "entries": {
                        "writing-for-agents": {"sha256": "a" * 64, "requires": [],
                                               "adaptations": []},
                        "tdd": {"sha256": "b" * 64, "requires": ["shell"],
                                "adaptations": []},
                        "wizard": {"sha256": "c" * 64, "requires": ["human_dialogue"],
                                   "adaptations": []},
                    }}
        with patch("anvil.preparation.status", return_value={"status": "installed"}), \
                patch("anvil.preparation.managed_snapshot", side_effect=snapshot), \
                patch("anvil.skill_runtime._registry", return_value=registry):
            choices, unclassified = _skills(self.repo)
        self.assertEqual([item["name"] for item in choices], ["tdd", "writing-for-agents"])
        self.assertEqual(choices[0]["compatible_agents"], ["codex"])
        self.assertEqual(unclassified, ["new-skill"])

    def test_cli_dispatches_prepare_and_reports_json(self):
        report = {"status": "prepared", "task_count": 2, "wave_count": 1,
                  "output": str(self.output), "artifact_dir": str(self.artifacts),
                  "unclassified_skills": []}
        stdout, stderr = StringIO(), StringIO()
        with patch("anvil.preparation.prepare", return_value=report) as invoked, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["prepare", str(self.spec), "-o", str(self.output),
                         "--repo", str(self.repo), "--agent", "muse", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(invoked.call_args.kwargs["agent"], "muse")

    def test_cli_reports_preparation_failure_without_traceback(self):
        stdout, stderr = StringIO(), StringIO()
        with patch("anvil.preparation.prepare", side_effect=ContractError("bad spec")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["prepare", str(self.spec), "-o", str(self.output), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue()), {"error": "bad spec"})
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
