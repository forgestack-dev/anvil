"""Scheduling metadata stays strict and execution-independent."""

from dataclasses import replace
import unittest

from anvil.contracts import ContractError, Task, parse_tasks
from anvil.planning import TaskGraph


def ticket() -> dict:
    return {"id": "task", "title": "Implement task", "objective": "Deliver the task",
            "depends_on": [], "acceptance_criteria": ["The task works"]}


def document(**fields) -> dict:
    return {"version": 1, "tasks": [ticket() | fields]}


class ParallelContractTests(unittest.TestCase):
    def test_optional_scheduling_fields_preserve_existing_ticket_serialization(self):
        task = parse_tasks(document())[0]
        self.assertEqual((task.worker, task.resources, task.exclusive), (None, (), False))
        self.assertEqual(task.to_dict(), ticket() | {"skills": []})
        explicit = parse_tasks(document(resources=[], exclusive=False))[0]
        self.assertEqual(explicit, task)
        self.assertEqual(explicit.to_dict(), task.to_dict())

    def test_worker_assignment_and_resource_locks_survive_graph_round_trip(self):
        value = document(worker="claude.tests", resources=["database", "lockfile-v2"], exclusive=True)
        graph = TaskGraph.from_document(value)
        task = graph.tasks[0]
        self.assertEqual(task.worker, "claude.tests")
        self.assertEqual(task.resources, ("database", "lockfile-v2"))
        self.assertIs(task.exclusive, True)
        # A pure plan does not need a runtime configuration to recognize an ID.
        self.assertEqual(TaskGraph.from_document({"version": 1, "tasks": [task.to_dict()]}), graph)
        self.assertEqual(graph.to_dict()["waves"][0]["tasks"][0], task.to_dict())
        value["tasks"][0]["resources"].append("modified-input")
        serialized = task.to_dict()
        serialized["resources"].append("modified-output")
        self.assertEqual(task.resources, ("database", "lockfile-v2"))

    def test_worker_and_resource_ids_use_the_task_identifier_boundary(self):
        for identifier in ("A", "worker.one-2_A", "x" * 80):
            with self.subTest(identifier=identifier):
                task = parse_tasks(document(worker=identifier, resources=[identifier]))[0]
                self.assertEqual((task.worker, task.resources), (identifier, (identifier,)))
        for identifier in (None, 1, True, [], {}, "", " ", "-starts-with-dash", ".hidden",
                           "with/slash", "with:colon", "with space", "x" * 81, "a\n", "a\0b"):
            for field, value in (("worker", identifier), ("resources", [identifier])):
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    parse_tasks(document(**{field: value}))

    def test_resources_require_unique_array_and_exclusive_requires_boolean(self):
        for resources in (None, "database", {}, ("database",), ["database", "database"]):
            with self.subTest(resources=resources), self.assertRaises(ContractError):
                parse_tasks(document(resources=resources))
        for exclusive in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(exclusive=exclusive), self.assertRaises(ContractError):
                parse_tasks(document(exclusive=exclusive))

    def test_direct_task_scheduling_fields_are_also_validated(self):
        task = Task("task", "Task", "Implement task", (), ("Task works",))
        pinned = replace(task, worker="codex", resources=("shared",), exclusive=True)
        self.assertEqual(parse_tasks({"version": 1, "tasks": [pinned.to_dict()]}), (pinned,))
        for fields in ({"worker": "../escape"}, {"resources": ["shared"]},
                       {"resources": ("shared", "shared")}, {"resources": ("",)},
                       {"exclusive": 1}):
            with self.subTest(fields=fields), self.assertRaises(ContractError):
                replace(task, **fields)


if __name__ == "__main__":
    unittest.main()
