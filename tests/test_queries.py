"""Bounded, paginated read queries over the execution ledger."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from anvil.contracts import Task
from anvil.queries import (
    MAX_PAGE_SIZE,
    list_runs,
    run_attempts,
    run_events,
    run_summary,
    run_tasks,
)
from anvil.store import RunStore, StoreError


def ticket(task_id: str, dependencies: tuple[str, ...] = ()) -> Task:
    return Task(task_id, f"Implement {task_id}", f"Deliver {task_id}",
                dependencies, (f"{task_id} works",))


class QueriesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)

    def make_run(self, run_id: str, *tasks: Task, config: dict | None = None) -> Path:
        run_dir = self.state_dir / run_id
        run_dir.mkdir()
        with RunStore(run_dir / "state.sqlite") as store:
            store.initialize(run_id=run_id, repo="/repo", branch=f"anvil/{run_id}",
                             base_sha="base", tasks=tasks or (ticket("a"),),
                             config=config if config is not None else {})
        return run_dir

    def test_run_summary_reports_status_and_task_counts(self):
        run_dir = self.make_run("run-one", ticket("a"), ticket("b", ("a",)))
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            store.start_attempt("a", "base", "/workspace/a")
        summary = run_summary(run_dir)
        self.assertEqual(summary["run_id"], "run-one")
        self.assertEqual(summary["status"], "running")
        self.assertEqual(summary["task_counts"], {"pending": 1, "running": 1})
        self.assertEqual(summary["task_total"], 2)

    def test_list_runs_paginates_and_rejects_oversized_pages(self):
        for index in range(3):
            self.make_run(f"run-{index}")
        first = list_runs(self.state_dir, limit=2)
        self.assertEqual([item["run_id"] for item in first["items"]], ["run-0", "run-1"])
        self.assertEqual(first["next_after"], "run-1")
        second = list_runs(self.state_dir, after=first["next_after"], limit=2)
        self.assertEqual([item["run_id"] for item in second["items"]], ["run-2"])
        self.assertIsNone(second["next_after"])
        with self.assertRaisesRegex(StoreError, "limit"):
            list_runs(self.state_dir, limit=MAX_PAGE_SIZE + 1)

    def test_list_runs_on_missing_state_dir_is_an_empty_page(self):
        result = list_runs(self.state_dir / "absent")
        self.assertEqual(result, {"items": [], "next_after": None})

    def test_run_tasks_pages_in_position_order(self):
        run_dir = self.make_run("run-tasks", ticket("a"), ticket("b"), ticket("c"))
        first = run_tasks(run_dir, limit=2)
        self.assertEqual([task["id"] for task in first["items"]], ["a", "b"])
        self.assertEqual(first["next_after"], 1)
        second = run_tasks(run_dir, after=first["next_after"], limit=2)
        self.assertEqual([task["id"] for task in second["items"]], ["c"])
        self.assertIsNone(second["next_after"])
        with self.assertRaisesRegex(StoreError, "limit"):
            run_tasks(run_dir, limit=MAX_PAGE_SIZE + 1)

    def test_run_attempts_pages_by_insertion_cursor(self):
        run_dir = self.make_run("run-attempts", ticket("a"), ticket("b"))
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            store.start_attempt("a", "base", "/workspace/a")
            store.start_attempt("b", "base", "/workspace/b")
        first = run_attempts(run_dir, limit=1)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first["items"][0]["task_id"], "a")
        self.assertIsNotNone(first["next_after"])
        second = run_attempts(run_dir, after=first["next_after"], limit=1)
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["task_id"], "b")
        self.assertIsNone(second["next_after"])

    def test_run_events_page_after_a_given_event_id(self):
        run_dir = self.make_run("run-events")
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace")
            store.transition("a", "candidate", attempt_id=attempt,
                             details={"candidate_sha": "candidate"})
        all_events = run_events(run_dir, limit=MAX_PAGE_SIZE)["items"]
        self.assertGreaterEqual(len(all_events), 3)
        first_id = all_events[0]["id"]
        page = run_events(run_dir, after=first_id, limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["id"], all_events[1]["id"])
        self.assertEqual(page["next_after"], all_events[1]["id"])
        exhausted = run_events(run_dir, after=all_events[-1]["id"])
        self.assertEqual(exhausted, {"items": [], "next_after": None})

    def test_default_page_size_is_one_hundred_rows(self):
        run_dir = self.make_run("run-default")
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            attempt = store.start_attempt("a", "base", "/workspace")
            for index in range(150):
                store.record_message("a", attempt_id=attempt, kind="progress",
                                     body={"index": index})
        page = run_events(run_dir)
        self.assertEqual(len(page["items"]), 100)
        self.assertIsNotNone(page["next_after"])

    def test_unknown_run_dir_raises_store_error(self):
        with self.assertRaisesRegex(StoreError, "cannot read run ledger"):
            run_summary(self.state_dir / "missing")
        with self.assertRaisesRegex(StoreError, "cannot read run ledger"):
            run_tasks(self.state_dir / "missing")
        with self.assertRaisesRegex(StoreError, "cannot read run ledger"):
            run_attempts(self.state_dir / "missing")
        with self.assertRaisesRegex(StoreError, "cannot read run ledger"):
            run_events(self.state_dir / "missing")

    def test_reads_stay_consistent_while_a_supervisor_commits(self):
        run_dir = self.make_run("run-concurrent", *(ticket(f"t{i}") for i in range(20)))
        errors: list[Exception] = []

        def supervisor():
            try:
                with RunStore(run_dir / "state.sqlite") as store:
                    store.set_run("running")
                    for index in range(20):
                        attempt = store.start_attempt(f"t{index}", "base", f"/workspace/{index}")
                        store.record_message(f"t{index}", attempt_id=attempt, kind="progress",
                                             body={"index": index})
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        reader_errors: list[Exception] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    run_summary(run_dir)
                    run_tasks(run_dir, limit=10)
                    run_attempts(run_dir, limit=10)
                    run_events(run_dir, limit=10)
                except Exception as exc:  # pragma: no cover - surfaced via reader_errors
                    reader_errors.append(exc)

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        supervisor_thread = threading.Thread(target=supervisor)
        supervisor_thread.start()
        supervisor_thread.join()
        stop.set()
        reader_thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(reader_errors, [])
        final = run_summary(run_dir)
        self.assertEqual(final["task_counts"], {"running": 20})

    def test_one_unreadable_ledger_does_not_hide_other_runs(self):
        self.make_run("run-a")
        self.make_run("run-c")
        crashed = self.state_dir / "run-b"
        crashed.mkdir()
        sqlite3.connect(crashed / "state.sqlite").close()

        items = list_runs(self.state_dir)["items"]
        self.assertEqual([item["run_id"] for item in items], ["run-a", "run-b", "run-c"])
        self.assertIn("unreadable", items[1])
        self.assertNotIn("unreadable", items[0])
        self.assertNotIn("unreadable", items[2])

    def test_unreadable_is_distinct_from_a_recorded_run_error(self):
        run_dir = self.make_run("run-failed")
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("failed", error="a check failed")
        summary = run_summary(run_dir)
        self.assertEqual(summary["error"], "a check failed")
        self.assertNotIn("unreadable", summary)

    def test_corrupt_json_payload_raises_store_error(self):
        run_dir = self.make_run("run-corrupt")
        connection = sqlite3.connect(run_dir / "state.sqlite")
        connection.execute("UPDATE events SET details = '{not json'")
        connection.commit()
        connection.close()
        with self.assertRaises(StoreError):
            run_events(run_dir)

    def test_version_one_ledgers_are_readable(self):
        # Schema version 1 declared no user_version pragma and used identical
        # table definitions, so a genuine v1 ledger is a current ledger whose
        # user_version is 0. The queries must not gate on that value.
        run_dir = self.make_run("run-v1")
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            store.start_attempt("a", "base", "/workspace/a")
        connection = sqlite3.connect(run_dir / "state.sqlite")
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
        connection.close()
        self.assertEqual(
            sqlite3.connect(run_dir / "state.sqlite").execute("PRAGMA user_version").fetchone()[0],
            0)

        self.assertEqual(run_summary(run_dir)["run_id"], "run-v1")
        self.assertTrue(run_tasks(run_dir)["items"])
        self.assertTrue(run_attempts(run_dir)["items"])
        self.assertTrue(run_events(run_dir)["items"])
        self.assertNotIn("unreadable", list_runs(self.state_dir)["items"][0])

    def test_runs_created_by_earlier_anvil_versions_are_readable(self):
        # Earlier ticket documents omitted every optional task field, and
        # earlier runs carried no adaptive configuration; the schema itself
        # has not changed, so those minimal payloads exercise the same
        # forward-compatibility boundary a genuinely older ledger would.
        minimal_task = Task("legacy", "Legacy task", "Deliver legacy behavior",
                            (), ("legacy works",))
        run_dir = self.make_run("run-legacy", minimal_task, config={})
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            attempt = store.start_attempt("legacy", "base", "/workspace/legacy")
            store.transition("legacy", "candidate", attempt_id=attempt,
                             details={"candidate_sha": "candidate"})

        summary = run_summary(run_dir)
        self.assertEqual(summary["config"], {})
        tasks = run_tasks(run_dir)["items"]
        self.assertEqual(tasks[0]["id"], "legacy")
        self.assertNotIn("worker", tasks[0])
        attempts = run_attempts(run_dir)["items"]
        self.assertEqual(attempts[0]["task_id"], "legacy")
        events = run_events(run_dir)["items"]
        self.assertTrue(events)


if __name__ == "__main__":
    unittest.main()
