"""Meaningful planning and malformed-input regression coverage."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from anvil.contracts import ContractError
from anvil.planning import TaskGraph


def ticket(task_id: str, dependencies: list[str] | None = None) -> dict:
    return {
        "id": task_id,
        "title": f"Implement {task_id}",
        "objective": f"Deliver the behavior for {task_id}.",
        "depends_on": dependencies or [],
        "acceptance_criteria": [f"The behavior for {task_id} is verified."],
    }


def document(*tasks: dict) -> dict:
    return {"version": 1, "tasks": list(tasks)}


class PlanningTests(unittest.TestCase):
    def test_unsorted_diamond_orders_dependencies_and_preserves_ready_input_order(self):
        graph = TaskGraph.from_document(
            document(ticket("finish", ["right", "left"]), ticket("right", ["start"]),
                     ticket("start"), ticket("left", ["start"]))
        )
        self.assertEqual(
            [[task.id for task in wave] for wave in graph.waves],
            [["start"], ["right", "left"], ["finish"]],
        )
        plan = graph.to_dict()
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["task_count"], 4)
        self.assertEqual(plan["wave_count"], 3)
        self.assertEqual(plan["max_wave_size"], 2)
        self.assertEqual(plan["waves"][2]["tasks"][0]["objective"], "Deliver the behavior for finish.")
        self.assertNotIn("status", plan["waves"][0]["tasks"][0])

    def test_independent_tasks_share_a_wave(self):
        graph = TaskGraph.from_document(document(ticket("a"), ticket("b"), ticket("c")))
        self.assertEqual(len(graph.waves), 1)
        self.assertEqual(graph.to_dict()["max_wave_size"], 3)

    def test_wave_size_does_not_claim_to_measure_possible_concurrent_frontiers(self):
        graph = TaskGraph.from_document(
            document(ticket("a"), ticket("b"), ticket("a1", ["a"]),
                     ticket("a2", ["a"]), ticket("b1", ["b"]),
                     ticket("b2", ["b1"]), ticket("b3", ["b1"]))
        )
        self.assertEqual([len(wave) for wave in graph.waves], [2, 3, 2])
        plan = graph.to_dict()
        self.assertEqual(plan["max_wave_size"], 3)
        # a1, a2, b2 and b3 can overlap when b1 finishes before a1 and a2.
        self.assertNotIn("max_potential_parallelism", plan)

    def test_rejects_missing_self_duplicate_and_cyclic_dependencies(self):
        cases = [
            (document(ticket("a", ["unknown"])), "missing dependency"),
            (document(ticket("a", ["a"])), "depends on itself"),
            (document(ticket("a"), ticket("a")), "duplicate task ID"),
            (document(ticket("a"), ticket("b", ["a", "a"])), "duplicate dependencies"),
            (document(ticket("a", ["b"]), ticket("b", ["a"])), "dependency cycle"),
            (document(ticket("free"), ticket("a", ["b"]), ticket("b", ["a"]),
                      ticket("blocked", ["a"])), "dependency cycle"),
        ]
        for value, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ContractError, message):
                TaskGraph.from_document(value)

    def test_empty_input_cannot_be_reported_as_success(self):
        with self.assertRaisesRegex(ContractError, "nonempty array"):
            TaskGraph.from_document(document())
        with self.assertRaisesRegex(ContractError, "at least one task"):
            TaskGraph(())

    def test_rejects_invalid_document_shapes_and_version_types(self):
        cases = [None, [], {}, {"version": 1, "tasks": {}},
                 {"version": True, "tasks": [ticket("a")]},
                 {"version": 1.0, "tasks": [ticket("a")]},
                 {"version": 2, "tasks": [ticket("a")]},
                 {"version": 1, "tasks": [ticket("a")], "extra": 1}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ContractError):
                TaskGraph.from_document(value)

    def test_rejects_unknown_missing_blank_and_wrong_type_task_fields(self):
        changes = [
            {"extra": True}, {"title": " \n"}, {"objective": None},
            {"depends_on": "a"}, {"acceptance_criteria": []},
            {"acceptance_criteria": [True]}, {"skills": "tdd"},
            {"skills": [""]}, {"skills": ["tdd", "tdd"]},
            {"id": "../escape"}, {"id": "-flag"}, {"id": "a\n"},
            {"id": "a" * 81}, {"depends_on": ["../escape"]},
            {"title": "bad\0title"}, {"objective": "bad\0objective"},
            {"acceptance_criteria": ["bad\0criterion"]}, {"skills": ["tdd\0"]},
            {"source_refs": []}, {"source_refs": [""]},
            {"source_refs": ["Requirement A", "Requirement A"]},
        ]
        for changeset in changes:
            value = ticket("valid") | changeset
            with self.subTest(changes=changeset), self.assertRaises(ContractError):
                TaskGraph.from_document(document(value))
        value = ticket("valid")
        del value["objective"]
        with self.assertRaisesRegex(ContractError, "missing required fields"):
            TaskGraph.from_document(document(value))
        with self.assertRaisesRegex(ContractError, "must be an object"):
            TaskGraph.from_document({"version": 1, "tasks": ["ticket"]})

    def test_accepts_source_references_and_strict_provenance(self):
        value = ticket("a") | {"source_refs": ["Requirements > A"]}
        provenance = {"generator": "anvil", "generator_version": "0.1.0.dev10",
                      "source": "SPEC.md", "source_sha256": "a" * 64,
                      "repo_head": "b" * 40, "prepared_at": "2026-09-15T12:00:00+00:00",
                      "agent": "codex"}
        graph = TaskGraph.from_document(document(value) | {"provenance": provenance})
        self.assertEqual(graph.tasks[0].source_refs, ("Requirements > A",))
        for field, replacement in (("source_sha256", "bad"), ("repo_head", "bad"),
                                   ("prepared_at", "yesterday"), ("agent", "other")):
            with self.subTest(field=field), self.assertRaises(ContractError):
                TaskGraph.from_document(document(value) | {
                    "provenance": provenance | {field: replacement}})

    def test_load_rejects_ambiguous_or_invalid_json_and_missing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tickets.json"
            for raw in ['{"version": 1, "version": 1, "tasks": []}',
                        '{"version": NaN, "tasks": []}', '{']:
                path.write_text(raw, encoding="utf-8")
                with self.subTest(raw=raw), self.assertRaises(ContractError):
                    TaskGraph.load(path)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(ContractError, "cannot read"):
                TaskGraph.load(path)
            with self.assertRaisesRegex(ContractError, "cannot read"):
                TaskGraph.load(Path(directory) / "missing.json")

    def test_load_reports_numeric_and_nesting_limits_as_contract_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tickets.json"
            malformed_inputs = [
                '{"version": ' + "9" * 5000 + ', "tasks": []}',
                "[" * 10000 + "0" + "]" * 10000,
            ]
            for raw in malformed_inputs:
                path.write_text(raw, encoding="utf-8")
                with self.subTest(length=len(raw)), self.assertRaises(ContractError):
                    TaskGraph.load(path)

    def test_load_preserves_specific_contract_error_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tickets.json"
            path.write_text('{"version": 1, "version": 1, "tasks": []}', encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "^duplicate JSON field: version$"):
                TaskGraph.load(path)

    def test_example_loads_with_parallel_middle_wave_and_serializable_evidence(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "tickets.json"
        graph = TaskGraph.load(path)
        self.assertEqual([len(wave) for wave in graph.waves], [1, 2, 1])
        self.assertEqual(graph.waves[1][0].skills, ("tdd",))
        self.assertTrue(json.dumps(graph.to_dict()))


if __name__ == "__main__":
    unittest.main()
