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
from anvil.preparation import _skills, gate_schema, prepare, result_schema
from anvil.workspaces import WorkspaceError
from test_execution import git


def task(task_id="T-1", *, depends_on=(), skills=()):
    return {"id": task_id, "title": "Implement the capability",
            "objective": "Deliver the specified behavior.",
            "depends_on": list(depends_on),
            "acceptance_criteria": ["The observable behavior matches the specification."],
            "source_refs": ["### Capability"], "skills": list(skills),
            "resources": ["core"], "exclusive": False, "risk": "medium"}


def question(question_id="Q-1", *, rule=3, kind="unbounded", refs=("### Capability",)):
    return {"id": question_id, "rule": rule, "kind": kind,
            "question": "Which call sites must the absence hold over?",
            "source_refs": list(refs)}


class FakePlanner:
    def __init__(self, result=None):
        self.result = result or {"version": 1, "tasks": [task()]}
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeGate(FakePlanner):
    def __init__(self, questions=()):
        super().__init__({"version": 1, "questions": list(questions)})


class PreparationCase(unittest.TestCase):
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

    def run_prepare(self, runner, *, skills=None, gate_agent="none", gate_runner=None, **kwargs):
        with patch("anvil.preparation._skills", return_value=(skills or [], [])):
            return prepare(self.spec, self.output, repo=self.repo, agent="claude-code",
                           artifact_root=self.artifacts, runner=runner,
                           gate_agent=gate_agent, gate_runner=gate_runner, **kwargs)


class PreparationTests(PreparationCase):
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


class ReadinessGateTests(PreparationCase):
    """The gate judges the graph, may only ask, and never runs on the authoring adapter."""

    def test_gate_questions_leave_no_tickets_behind(self):
        planner, gate = FakePlanner(), FakeGate([question()])
        result = self.run_prepare(planner, gate_agent="codex", gate_runner=gate)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["raised_by"], "gate")
        self.assertEqual(result["gate_agent"], "codex")
        self.assertEqual([item["id"] for item in result["questions"]], ["Q-1"])
        self.assertFalse(self.output.exists())
        self.assertNotEqual(result["artifact_dir"], result["gate_artifact_dir"])

    def test_silent_gate_writes_tickets_and_records_the_adapter(self):
        gate = FakeGate()
        result = self.run_prepare(FakePlanner(), gate_agent="codex", gate_runner=gate)
        self.assertEqual(result["status"], "prepared")
        recorded = json.loads(self.output.read_text())["provenance"]["gate"]
        self.assertEqual(recorded, {"agent": "codex", "distinct_adapter": True, "questions": 0})
        self.assertTrue(gate.calls[0]["read_only"])
        self.assertEqual(gate.calls[0]["schema"], gate_schema())

    def test_disabled_gate_is_recorded_rather_than_assumed(self):
        result = self.run_prepare(FakePlanner())
        self.assertIsNone(json.loads(self.output.read_text())["provenance"]["gate"])
        self.assertIsNone(result["gate_artifact_dir"])
        TaskGraph.from_document(json.loads(self.output.read_text()))

    def test_gate_sees_the_graph_and_the_specification_but_not_the_planning_turn(self):
        planner, gate = FakePlanner(), FakeGate()
        self.run_prepare(planner, gate_agent="codex", gate_runner=gate)
        prompt = gate.calls[0]["prompt"]
        self.assertIn("Implement the capability", prompt)
        self.assertIn("### Capability", prompt)
        self.assertIn("You cannot approve", prompt)
        self.assertNotIn(planner.calls[0]["artifact_dir"].name, prompt)
        self.assertNotIn("Convert the committed Markdown specification", prompt)

    def test_gate_on_the_authoring_adapter_is_refused_before_any_turn(self):
        planner = FakePlanner()
        with self.assertRaises(ContractError) as raised:
            self.run_prepare(planner, gate_agent="claude-code", gate_runner=FakeGate())
        self.assertIn("must differ from the preparation agent", str(raised.exception))
        self.assertEqual(planner.calls, [])
        self.assertFalse(self.output.exists())

    def test_unavailable_gate_fails_before_the_planning_turn_is_paid_for(self):
        planner = FakePlanner()
        with patch("anvil.preparation.probe_agent", side_effect=ContractError("no codex")), \
                patch("anvil.preparation._skills", return_value=([], [])):
            with self.assertRaises(ContractError) as raised:
                prepare(self.spec, self.output, repo=self.repo, agent="claude-code",
                        artifact_root=self.artifacts, runner=planner, gate_agent="codex")
        self.assertIn("pass none to disable the gate", str(raised.exception))
        self.assertEqual(planner.calls, [])

    def test_unbound_unlocated_and_malformed_questions_are_refused(self):
        unknown_rule = question(rule=7)
        absent_ref = question(refs=["### Absent heading"])
        missing_rule = {key: value for key, value in question().items() if key != "rule"}
        for verdict in ({"version": 1, "questions": [unknown_rule]},
                        {"version": 1, "questions": [absent_ref]},
                        {"version": 1, "questions": [missing_rule]},
                        {"version": 1, "questions": [question(), question()]},
                        {"version": 1, "questions": [question(kind="stylistic")]},
                        {"version": 1, "tasks": [task()]}):
            with self.subTest(verdict=verdict):
                with self.assertRaises(ContractError):
                    self.run_prepare(FakePlanner(), gate_agent="codex",
                                     gate_runner=FakePlanner(verdict))
                self.assertFalse(self.output.exists())

    def test_planning_turn_may_ask_instead_of_inventing_a_graph(self):
        planner = FakePlanner({"version": 1, "questions": [question(rule=1, kind="undecidable")]})
        gate = FakeGate()
        result = self.run_prepare(planner, gate_agent="codex", gate_runner=gate)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["raised_by"], "preparation")
        self.assertEqual(gate.calls, [])
        self.assertFalse(self.output.exists())
        self.assertIn("questions", planner.calls[0]["schema"]["properties"])

    def test_a_result_carrying_both_branches_is_refused(self):
        both = {"version": 1, "tasks": [task()], "questions": [question()]}
        with self.assertRaises(ContractError):
            self.run_prepare(FakePlanner(both), gate_agent="codex", gate_runner=FakeGate())

    def test_cli_reports_open_questions_as_a_distinct_exit_code(self):
        report = {"status": "needs_clarification", "raised_by": "gate", "gate_agent": "codex",
                  "questions": [question()], "artifact_dir": str(self.artifacts)}
        stdout, stderr = StringIO(), StringIO()
        with patch("anvil.preparation.prepare", return_value=report), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["prepare", str(self.spec), "-o", str(self.output),
                         "--repo", str(self.repo), "--gate-agent", "codex"])
        self.assertEqual(code, 3)
        self.assertIn("rule 3 (unbounded)", stderr.getvalue())
        self.assertIn("### Capability", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_cli_passes_the_gate_selection_through(self):
        report = {"status": "prepared", "task_count": 1, "wave_count": 1,
                  "output": str(self.output), "artifact_dir": str(self.artifacts),
                  "unclassified_skills": []}
        with patch("anvil.preparation.prepare", return_value=report) as invoked, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["prepare", str(self.spec), "-o", str(self.output),
                         "--repo", str(self.repo), "--gate-agent", "none", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(invoked.call_args.kwargs["gate_agent"], "none")


class GateProfileTests(PreparationCase):
    """Explicit model and effort selection for either preparation turn."""

    def test_recorded_models_make_the_distinctness_claim_checkable(self):
        result = self.run_prepare(
            FakePlanner(), gate_agent="codex", gate_runner=FakeGate(),
            profile={"model": "claude-opus-5", "effort": "high"},
            gate_profile={"model": "gpt-5-codex", "effort": "high"})
        self.assertEqual(result["status"], "prepared")
        provenance = json.loads(self.output.read_text())["provenance"]
        self.assertEqual(provenance["model"], "claude-opus-5")
        self.assertEqual(provenance["gate"], {"agent": "codex", "model": "gpt-5-codex",
                                              "distinct_adapter": True, "questions": 0})
        TaskGraph.from_document(json.loads(self.output.read_text()))

    def test_profiles_reach_the_runners_they_select(self):
        with patch("anvil.preparation.create_runner") as created, \
                patch("anvil.preparation._probe"), \
                patch("anvil.preparation._skills", return_value=([], [])):
            created.side_effect = [FakePlanner(), FakeGate()]
            prepare(self.spec, self.output, repo=self.repo, agent="claude-code",
                    artifact_root=self.artifacts, gate_agent="codex",
                    profile={"model": "claude-opus-5", "effort": "high"},
                    gate_profile={"model": "gpt-5-codex", "effort": "medium"})
        self.assertEqual(created.call_args_list[0].kwargs["profile"],
                         {"model": "claude-opus-5", "effort": "high"})
        self.assertEqual(created.call_args_list[1].kwargs["profile"],
                         {"model": "gpt-5-codex", "effort": "medium"})

    def test_one_model_behind_two_adapters_is_not_a_second_opinion(self):
        planner = FakePlanner()
        with self.assertRaises(ContractError) as raised:
            self.run_prepare(planner, gate_agent="codex", gate_runner=FakeGate(),
                             profile={"model": "shared-model", "effort": "high"},
                             gate_profile={"model": "shared-model", "effort": "high"})
        self.assertIn("gate model must differ", str(raised.exception))
        self.assertEqual(planner.calls, [])

    def test_unsupported_and_incomplete_selections_are_refused(self):
        cases = {
            "effort": {"model": "claude-opus-5", "effort": "sideways"},
            "model": {"model": "", "effort": "high"},
            "field": {"model": "claude-opus-5", "effort": "high", "rank": 2},
        }
        for name, gate_profile in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ContractError):
                    self.run_prepare(FakePlanner(), gate_agent="codex",
                                     gate_runner=FakeGate(), gate_profile=gate_profile)
        with self.assertRaises(ContractError) as raised:
            self.run_prepare(FakePlanner(), gate_profile={"model": "m", "effort": "high"})
        self.assertIn("gate profile requires a gate agent", str(raised.exception))

    def test_muse_turns_take_no_profile(self):
        with patch("anvil.preparation._skills", return_value=([], [])):
            with self.assertRaises(ContractError) as raised:
                prepare(self.spec, self.output, repo=self.repo, agent="claude-code",
                        artifact_root=self.artifacts, runner=FakePlanner(),
                        gate_agent="muse", gate_runner=FakeGate(),
                        gate_profile={"model": "muse-1", "effort": "high"})
        self.assertIn("operator handoff", str(raised.exception))

    def test_a_profile_is_preflighted_rather_than_only_probed(self):
        planner = FakePlanner()
        with patch("anvil.preparation.preflight") as checked, \
                patch("anvil.preparation.probe_agent") as probed, \
                patch("anvil.preparation.create_runner", return_value=FakeGate()), \
                patch("anvil.preparation._skills", return_value=([], [])):
            prepare(self.spec, self.output, repo=self.repo, agent="claude-code",
                    artifact_root=self.artifacts, runner=planner, gate_agent="codex",
                    gate_runner=None, gate_profile={"model": "gpt-5-codex", "effort": "high"})
        self.assertEqual(checked.call_args.args[0], "codex")
        self.assertEqual(checked.call_args.args[2], {"model": "gpt-5-codex", "effort": "high"})
        self.assertEqual(probed.call_args_list, [])

    def test_cli_pairs_model_with_effort_and_passes_both_selections(self):
        report = {"status": "prepared", "task_count": 1, "wave_count": 1,
                  "output": str(self.output), "artifact_dir": str(self.artifacts),
                  "unclassified_skills": []}
        with patch("anvil.preparation.prepare", return_value=report) as invoked, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["prepare", str(self.spec), "-o", str(self.output),
                         "--repo", str(self.repo), "--agent", "claude-code",
                         "--model", "claude-opus-5", "--effort", "high",
                         "--gate-agent", "codex", "--gate-model", "gpt-5-codex",
                         "--gate-effort", "medium", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(invoked.call_args.kwargs["profile"],
                         {"model": "claude-opus-5", "effort": "high"})
        self.assertEqual(invoked.call_args.kwargs["gate_profile"],
                         {"model": "gpt-5-codex", "effort": "medium"})
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["prepare", str(self.spec), "-o", str(self.output),
                         "--repo", str(self.repo), "--gate-model", "gpt-5-codex", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("must be given together", json.loads(stdout.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
