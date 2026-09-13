"""Durable worker assignments, owner heartbeats and coordinator messages."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from anvil.contracts import Task
from anvil.store import RunStore, StoreError


def ticket(task_id: str, dependencies: tuple[str, ...] = ()) -> Task:
    return Task(task_id, f"Implement {task_id}", f"Deliver {task_id}",
                dependencies, (f"{task_id} works",))


class ParallelStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "run.sqlite3"

    def initialize(self, store: RunStore, *tasks: Task):
        store.initialize(run_id="parallel", repo="/repo", branch="anvil/parallel",
                         base_sha="base", tasks=tasks or (ticket("a"), ticket("b")), config={})
        store.set_run("running")

    def complete(self, store: RunStore, task_id: str, attempt_id: str):
        for status, details in (
            ("candidate", {"candidate_sha": "candidate"}),
            ("reviewed", {"review": {"passed": True}}),
            ("verified", {"verification": [{"exit_code": 0}]}),
            ("integrating", {"integration_sha": "integration"}),
            ("done", {"integrated_sha": "integrated"}),
        ):
            store.transition(task_id, status, attempt_id=attempt_id, details=details)

    def test_competing_connections_claim_a_task_once(self):
        with RunStore(self.path) as store:
            self.initialize(store)
        ready = Barrier(2)

        def claim(worker_id, agent):
            with RunStore(self.path) as store:
                ready.wait(timeout=5)
                try:
                    attempt = store.start_attempt("a", "base", f"/work/{worker_id}",
                                                  worker_id=worker_id, agent=agent)
                except StoreError as exc:
                    return "rejected", str(exc)
                return "claimed", attempt

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim, "one", "codex"),
                       executor.submit(claim, "two", "claude-code")]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(result[0] for result in results), ["claimed", "rejected"])
        rejected = next(result[1] for result in results if result[0] == "rejected")
        self.assertIn("not pending", rejected)
        snapshot = RunStore.read(self.path)
        self.assertEqual(len(snapshot["attempts"]), 1)
        self.assertEqual([task["status"] for task in snapshot["tasks"]], ["running", "pending"])
        dispatches = [event for event in snapshot["events"] if event["kind"] == "dispatch"]
        self.assertEqual(len(dispatches), 1)
        self.assertEqual(dispatches[0]["attempt_id"], snapshot["attempts"][0]["id"])
        self.assertEqual(dispatches[0]["details"]["worker_id"],
                         snapshot["tasks"][0]["details"]["worker_id"])

    def test_assignments_remain_attached_to_their_own_evidence(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            owners = {}
            for task_id, agent in (("a", "codex"), ("b", "claude-code")):
                owners[task_id] = store.start_attempt(
                    task_id, "base", f"/work/{task_id}",
                    worker_id=f"worker-{task_id}", agent=agent,
                )
                store.transition(task_id, "candidate", attempt_id=owners[task_id],
                                 details={"candidate_sha": f"candidate-{task_id}"})
            snapshot = store.snapshot()
        attempts = {attempt["task_id"]: attempt for attempt in snapshot["attempts"]}
        for task, agent in zip(snapshot["tasks"], ("codex", "claude-code")):
            with self.subTest(task=task["id"]):
                expected = {"worker_id": f"worker-{task['id']}", "agent": agent,
                            "candidate_sha": f"candidate-{task['id']}"}
                self.assertEqual(task["details"], expected)
                self.assertEqual(attempts[task["id"]]["details"], expected)
                self.assertEqual(task["attempt_id"], owners[task["id"]])

    def test_claims_still_wait_for_accepted_dependencies(self):
        with RunStore(self.path) as store:
            self.initialize(store, ticket("a"), ticket("b", ("a",)))
            owner = store.start_attempt("a", "base", "/work/a", worker_id="one", agent="codex")
            before = store.snapshot()
            with self.assertRaisesRegex(StoreError, "unfinished dependencies"):
                store.start_attempt("b", "base", "/work/b", worker_id="two", agent="claude-code")
            self.assertEqual(store.snapshot(), before)
            self.complete(store, "a", owner)
            store.start_attempt("b", "integrated", "/work/b", worker_id="two", agent="claude-code")
            self.assertEqual(store.snapshot()["tasks"][1]["status"], "running")

    def test_legacy_claims_do_not_gain_assignment_metadata_or_dispatches(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            attempt = store.start_attempt("a", "base", "/work/a")
            snapshot = store.snapshot()
        self.assertEqual(snapshot["tasks"][0]["details"], {})
        self.assertEqual(snapshot["attempts"][0]["details"], {})
        self.assertEqual([event["kind"] for event in snapshot["events"]], ["run", "run", "task"])
        self.assertEqual(snapshot["events"][-1]["details"],
                         {"base_sha": "base", "workspace": "/work/a"})
        with RunStore(self.path) as store:
            store.heartbeat("a", attempt_id=attempt)
            store.record_message("a", attempt_id=attempt, kind="worker-result", body={"summary": "Ready"})
            self.complete(store, "a", attempt)
            self.assertEqual(store.snapshot()["tasks"][0]["status"], "done")

    def test_assignment_validation_cannot_partially_claim_a_task(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            before = store.snapshot()
            for field in ("worker_id", "agent"):
                for value in ("", " ", 1, [], {}):
                    with self.subTest(field=field, value=value), self.assertRaises(StoreError):
                        store.start_attempt("a", "base", "/work/a", **{field: value})
                    self.assertEqual(store.snapshot(), before)

    def test_heartbeats_preserve_evidence_without_appending_events(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            attempt = store.start_attempt("a", "base", "/work/a", worker_id="one", agent="codex")
            store.transition("a", "candidate", attempt_id=attempt, details={"candidate_sha": "candidate"})
            before = store.snapshot()
            for timestamp in ("2030-01-01T00:00:00+00:00", "2030-01-01T00:00:01+00:00"):
                with patch("anvil.store._now", return_value=timestamp):
                    store.heartbeat("a", attempt_id=attempt)
                snapshot = store.snapshot()
                self.assertEqual(snapshot["events"], before["events"])
                for item in (snapshot["tasks"][0], snapshot["attempts"][0]):
                    self.assertEqual(item["details"], before["tasks"][0]["details"] | {"heartbeat_at": timestamp})
                    self.assertEqual(item["updated_at"], timestamp)
                    self.assertEqual(item["status"], "candidate")
                self.assertIsNone(snapshot["attempts"][0]["finished_at"])
            store.transition("a", "reviewed", attempt_id=attempt, details={"review": {"passed": True}})
            self.assertEqual(store.snapshot()["tasks"][0]["details"]["heartbeat_at"], timestamp)

    def test_messages_survive_reopening_and_read_only_status(self):
        body = {"dependency_id": "a", "integrated_sha": "abc", "summary": "Shared API is ready"}
        with RunStore(self.path) as store:
            self.initialize(store)
            attempt = store.start_attempt("b", "base", "/work/b", worker_id="two", agent="claude-code")
            before = store.snapshot()
            store.record_message("b", attempt_id=attempt, kind="dependency-handoff", body=body)
            body["summary"] = "Caller changed its copy"
            after = store.snapshot()
            self.assertEqual(after["tasks"], before["tasks"])
            self.assertEqual(after["attempts"], before["attempts"])
        file_before = self.path.read_bytes()
        self.assertEqual(RunStore.read(self.path), after)
        self.assertEqual(self.path.read_bytes(), file_before)
        with RunStore(self.path) as store:
            self.assertEqual(store.snapshot(), after)
            store.transition("b", "candidate", attempt_id=attempt, details={"candidate_sha": "candidate"})
            store.record_message("b", attempt_id=attempt, kind="worker-result", body={"summary": "Implemented"})
            self.complete_from_candidate(store, "b", attempt)
            after = store.snapshot()
        self.assertEqual(RunStore.read(self.path), after)
        messages = [event for event in after["events"] if event["kind"] == "message"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["details"], {"message_kind": "dependency-handoff", "body": {
            "dependency_id": "a", "integrated_sha": "abc", "summary": "Shared API is ready",
        }})
        for message, status in zip(messages, ("running", "candidate")):
            self.assertEqual(message["task_id"], "b")
            self.assertEqual(message["attempt_id"], attempt)
            self.assertEqual(message["from_status"], status)
            self.assertEqual(message["to_status"], status)

    def complete_from_candidate(self, store, task_id, attempt):
        for status, details in (
            ("reviewed", {"review": {"passed": True}}),
            ("verified", {"verification": [{"exit_code": 0}]}),
            ("integrating", {"integration_sha": "integration"}),
            ("done", {"integrated_sha": "integrated"}),
        ):
            store.transition(task_id, status, attempt_id=attempt, details=details)

    def test_stale_foreign_pending_and_unknown_owners_cannot_send_or_heartbeat(self):
        with RunStore(self.path) as store:
            self.initialize(store, ticket("a"), ticket("b"), ticket("c"))
            first = store.start_attempt("a", "base", "/work/a", worker_id="one", agent="codex")
            second = store.start_attempt("b", "base", "/work/b", worker_id="two", agent="claude-code")
            before = store.snapshot()
            for task_id, attempt, error in (("a", "stale", "stale"), ("a", second, "stale"),
                                            ("c", first, "stale"), ("unknown", first, "unknown task")):
                for method in ("heartbeat", "record_message"):
                    kwargs = {"kind": "worker-result", "body": {}} if method == "record_message" else {}
                    with self.subTest(task=task_id, method=method), self.assertRaisesRegex(StoreError, error):
                        getattr(store, method)(task_id, attempt_id=attempt, **kwargs)
                    self.assertEqual(store.snapshot(), before)

    def test_terminal_tasks_and_runs_reject_messages_and_heartbeats(self):
        for terminal in ("failed", "blocked", "interrupted", "done"):
            with self.subTest(terminal=terminal), RunStore(self.path.parent / f"{terminal}.db") as store:
                self.initialize(store)
                attempt = store.start_attempt("a", "base", "/work/a", worker_id="one", agent="codex")
                other = store.start_attempt("b", "base", "/work/b", worker_id="two", agent="claude-code")
                if terminal == "done":
                    self.complete(store, "a", attempt)
                else:
                    store.transition("a", terminal, attempt_id=attempt)
                for task_id, token, error in (("a", attempt, "terminal"), ("b", other, "running run")):
                    if task_id == "b":
                        store.set_run("failed", "stopped")
                    before = store.snapshot()
                    with self.assertRaisesRegex(StoreError, error):
                        store.heartbeat(task_id, attempt_id=token)
                    with self.assertRaisesRegex(StoreError, error):
                        store.record_message(task_id, attempt_id=token, kind="late", body={})
                    self.assertEqual(store.snapshot(), before)

    def test_invalid_messages_leave_the_ledger_unchanged(self):
        with RunStore(self.path) as store:
            self.initialize(store)
            attempt = store.start_attempt("a", "base", "/work/a", worker_id="one", agent="codex")
            before = store.snapshot()
            for kind, body in (("", {}), (" ", {}), (1, {}), ("handoff", []),
                               ("handoff", {"bad": float("nan")}), ("handoff", {"bad": object()})):
                with self.subTest(kind=kind, body=body), self.assertRaises(StoreError):
                    store.record_message("a", attempt_id=attempt, kind=kind, body=body)
                self.assertEqual(store.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
