"""State, ownership and durable evidence gates for serial runs."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from anvil.contracts import Task
from anvil.store import RunStore, StoreError


def ticket(task_id: str, dependencies: tuple[str, ...] = ()) -> Task:
    return Task(task_id, f"Implement {task_id}", f"Deliver {task_id}",
                dependencies, (f"{task_id} works",))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "run.sqlite3"

    def initialize(self, store: RunStore, *tasks: Task):
        store.initialize(run_id="run-one", repo="/repo", branch="anvil/run-one",
                         base_sha="base", tasks=tasks or (ticket("a"),),
                         config={"verification_commands": [["python", "-m", "unittest"]]})

    def complete(self, store: RunStore, task_id: str, attempt_id: str):
        for status, details in (
            ("candidate", {"candidate_sha": "candidate"}),
            ("reviewed", {"review": {"passed": True}}),
            ("verified", {"verification": [{"exit_code": 0}]}),
            ("integrating", {"integration_sha": "integration"}),
            ("done", {"integrated_sha": "integrated"}),
        ):
            store.transition(task_id, status, attempt_id=attempt_id, details=details)

    def test_attempt_requires_running_run_and_finished_dependencies(self):
        with RunStore(self.path) as store:
            self.initialize(store, ticket("a"), ticket("b", ("a",)))
            with self.assertRaisesRegex(StoreError, "running run"):
                store.start_attempt("a", "base", "/workspace/a")
            store.set_run("running")
            with self.assertRaisesRegex(StoreError, "unfinished dependencies"):
                store.start_attempt("b", "base", "/workspace/b")
            attempt = store.start_attempt("a", "base", "/workspace/a")
            with self.assertRaisesRegex(StoreError, "not pending"):
                store.start_attempt("a", "base", "/workspace/a")
            self.complete(store, "a", attempt)
            second = store.start_attempt("b", "integrated", "/workspace/b")
            self.assertNotEqual(attempt, second)
            snapshot = store.snapshot()
            self.assertEqual([task["status"] for task in snapshot["tasks"]], ["done", "running"])
            self.assertEqual(snapshot["attempts"][1]["base_sha"], "integrated")

    def test_recorded_rejection_survives_refused_retry_reservation(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace/a")
            store.transition("a", "candidate", attempt_id=attempt,
                             details={"candidate_sha": "candidate",
                                      "review": {"verdict": "request_changes", "findings": ["bug"]}})
            with self.assertRaisesRegex(StoreError, "failure_category"):
                store.record_rejection("a", attempt_id=attempt, reason="r", failure_category="bogus")
            # The coordinator records the rejection before reserving the next
            # attempt. If the invocation or cost budget rejects that
            # reservation, the run fails here with the cause already stored.
            store.record_rejection("a", attempt_id=attempt, reason="reviewer requested changes",
                                   failure_category="review_rejection")
            recorded = next(a for a in store.snapshot()["attempts"] if a["id"] == attempt)
            self.assertEqual(recorded["status"], "failed")
            self.assertEqual(recorded["details"]["failure_category"], "review_rejection")
            self.assertEqual(recorded["details"]["retry_reason"], "reviewer requested changes")
            # The stop transition must not clobber the recorded cause.
            store.transition("a", "failed", attempt_id=attempt,
                             details={"error": "soft cost budget cannot reserve worker and reviewer"})
            stopped = next(a for a in store.snapshot()["attempts"] if a["id"] == attempt)
            self.assertEqual(stopped["details"]["failure_category"], "review_rejection")
            self.assertEqual(stopped["details"]["retry_reason"], "reviewer requested changes")
            self.assertEqual(stopped["details"]["error"],
                             "soft cost budget cannot reserve worker and reviewer")

    def test_stale_attempt_cannot_change_a_task_or_append_an_event(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            store.set_run("running")
            store.start_attempt("a", "base", "/workspace")
            before = store.snapshot()
            with self.assertRaisesRegex(StoreError, "stale or missing attempt"):
                store.transition("a", "candidate", attempt_id="wrong-owner",
                                 details={"candidate_sha": "candidate"})
            self.assertEqual(store.snapshot(), before)

    def test_reserved_attempt_id_collision_rolls_back_without_claiming_task(self):
        with RunStore(self.path) as store:
            self.initialize(store, ticket("a"), ticket("b"))
            store.set_run("running")
            first = store.start_attempt("a", "base", "/workspace/a", attempt_id="reserved-first")
            self.assertEqual(first, "reserved-first")
            before = store.snapshot()
            with self.assertRaises(StoreError):
                store.start_attempt("b", "base", "/workspace/b", attempt_id=first)
            self.assertEqual(store.snapshot(), before)
            second = store.start_attempt("b", "base", "/workspace/b")
            self.assertNotEqual(first, second)

    def test_false_completion_and_skipped_phases_are_rejected(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace")
            for phase in ("done", "verified", "integrating", "reviewed", "pending", "running"):
                with self.subTest(phase=phase), self.assertRaisesRegex(StoreError, "invalid task transition"):
                    store.transition("a", phase, attempt_id=attempt,
                                     details={"message": "everything is complete", "integrated_sha": "sha"})
            with self.assertRaisesRegex(StoreError, "every task"):
                store.set_run("success")
            self.assertEqual(store.snapshot()["tasks"][0]["status"], "running")

    def test_each_phase_requires_its_evidence_and_preserves_previous_evidence(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace")
            stages = (
                ("candidate", {"candidate_sha": "candidate"}, {"candidate_sha": " "}),
                ("reviewed", {"review": {"passed": True}}, {"review": []}),
                ("verified", {"verification": [{"exit_code": 0}]}, {"verification": []}),
                ("integrating", {"integration_sha": "integration"}, {"integration_sha": None}),
                ("done", {"integrated_sha": "integrated"}, {"integrated_sha": ""}),
            )
            expected = {}
            for phase, good, bad in stages:
                with self.subTest(phase=phase):
                    before = store.snapshot()
                    with self.assertRaises(StoreError):
                        store.transition("a", phase, attempt_id=attempt, details=bad)
                    self.assertEqual(store.snapshot(), before)
                    store.transition("a", phase, attempt_id=attempt, details=good)
                    expected.update(good)
                    snapshot = store.snapshot()
                    self.assertEqual(snapshot["tasks"][0]["details"], expected)
                    self.assertEqual(snapshot["attempts"][0]["details"], expected)
                    self.assertEqual(snapshot["events"][-1]["details"], expected)
            store.set_run("success")
            self.assertEqual(store.snapshot()["status"], "success")

    def test_terminal_tasks_and_terminal_runs_cannot_be_edited(self):
        for terminal in ("failed", "blocked", "interrupted", "done"):
            with self.subTest(terminal=terminal), RunStore(self.path.parent / f"{terminal}.db") as store:
                self.initialize(store)
                store.set_run("running")
                attempt = store.start_attempt("a", "base", "/workspace")
                if terminal == "done":
                    self.complete(store, "a", attempt)
                else:
                    store.transition("a", terminal, attempt_id=attempt, details={"error": "stopped"})
                before = store.snapshot()
                with self.assertRaisesRegex(StoreError, "terminal"):
                    store.transition("a", "candidate", attempt_id=attempt, details={"candidate_sha": "new"})
                with self.assertRaisesRegex(StoreError, "not pending"):
                    store.start_attempt("a", "base", "/workspace")
                self.assertEqual(store.snapshot(), before)
                store.set_run("failed", "integration could not complete")
                with self.assertRaisesRegex(StoreError, "invalid run transition"):
                    store.set_run("success")
                with self.assertRaisesRegex(StoreError, "running run"):
                    store.transition("a", "failed", attempt_id=attempt)
                self.assertEqual(store.snapshot()["status"], "failed")

    def test_stopped_initialization_preserves_pending_tasks_and_is_terminal(self):
        for status in ("failed", "blocked", "interrupted"):
            path = self.path.parent / f"startup-{status}.db"
            with self.subTest(status=status):
                with RunStore(path) as store:
                    self.initialize(store)
                    with self.assertRaisesRegex(StoreError, "invalid run transition"):
                        store.set_run("success")
                    store.set_run(status, "stopped before execution")
                    snapshot = store.snapshot()
                    self.assertEqual(snapshot["status"], status)
                    self.assertEqual(snapshot["tasks"][0]["status"], "pending")
                    self.assertEqual(snapshot["attempts"], [])
                    self.assertEqual(snapshot["events"][-1]["from_status"], "created")
                    with self.assertRaisesRegex(StoreError, "invalid run transition"):
                        store.set_run("running")
                    with self.assertRaisesRegex(StoreError, "running run"):
                        store.start_attempt("a", "base", "/workspace")
                    self.assertEqual(store.snapshot(), snapshot)
                self.assertEqual(RunStore.read(path), snapshot)

    def test_reopen_retains_evidence_and_immutable_input_snapshot(self):
        config = {"verification_commands": [["check"]]}
        with RunStore(self.path) as store:
            store.initialize(run_id="persisted", repo="/repo", branch="anvil/persisted",
                             base_sha="base", tasks=(ticket("a"),), config=config)
            config["verification_commands"][0].append("mutated")
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace")
            store.transition("a", "candidate", attempt_id=attempt,
                             details={"candidate_sha": "candidate", "artifacts": ["/output.json"]})
            before = store.snapshot()
        self.assertEqual(RunStore.read(self.path), before)
        with RunStore(self.path) as store:
            with self.assertRaisesRegex(StoreError, "already exists"):
                self.initialize(store)
            self.assertEqual(store.snapshot(), before)
            store.transition("a", "reviewed", attempt_id=attempt, details={"review": {"passed": True}})
            snapshot = store.snapshot()
            self.assertEqual(snapshot["config"]["verification_commands"], [["check"]])
            self.assertEqual(snapshot["tasks"][0]["details"]["artifacts"], ["/output.json"])
            self.assertEqual(snapshot["tasks"][0]["acceptance_criteria"], ["a works"])
            self.assertTrue(json.dumps(snapshot, allow_nan=False))

    def test_missing_read_does_not_create_database_or_parent(self):
        missing = self.path.parent / "absent" / "run.sqlite3"
        with self.assertRaisesRegex(StoreError, "cannot read run ledger"):
            RunStore.read(missing)
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())

    def test_status_and_evidence_rollback_if_audit_event_write_fails(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace")
            store.transition("a", "candidate", attempt_id=attempt, details={"candidate_sha": "candidate"})
            before = store.snapshot()
            with sqlite3.connect(self.path) as connection:
                connection.execute("""
                    CREATE TRIGGER reject_review BEFORE INSERT ON events
                    WHEN NEW.to_status = 'reviewed'
                    BEGIN SELECT RAISE(ABORT, 'injected event failure'); END
                """)
            with self.assertRaisesRegex(StoreError, "injected event failure"):
                store.transition("a", "reviewed", attempt_id=attempt, details={"review": {"passed": True}})
            self.assertEqual(store.snapshot(), before)

    def test_empty_graph_and_non_json_evidence_cannot_enter_the_ledger(self):
        with RunStore(self.path) as store:
            with self.assertRaises(StoreError):
                store.initialize(run_id="empty", repo="/repo", branch="anvil/empty",
                                 base_sha="base", tasks=(), config={})
            self.initialize(store)
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace")
            before = store.snapshot()
            for value in (float("nan"), object()):
                with self.subTest(value=value), self.assertRaises(StoreError):
                    store.transition("a", "candidate", attempt_id=attempt,
                                     details={"candidate_sha": "candidate", "invalid": value})
            self.assertEqual(store.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
