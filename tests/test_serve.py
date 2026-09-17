"""Read-only HTTP access to run ledgers, its isolation, and its event stream."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from anvil.contracts import ContractError, Task
import anvil.serve
from anvil.serve import API_VERSION, create
from anvil.store import RunStore
from anvil.ticket_status import atomic, encoded


def ticket(task_id: str, dependencies: tuple[str, ...] = ()) -> Task:
    return Task(task_id, f"Implement {task_id}", f"Deliver {task_id}",
                dependencies, (f"{task_id} works",))


def invocation(role: str, cost: float | None, **extra) -> dict:
    return {"role": role, "cost_usd": cost, "duration_seconds": 1.5,
            "cost_kind": "provider_reported_estimate" if cost is not None else "unknown",
            **extra}


class ServeTestCase(unittest.TestCase):
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

    def record_invocation(self, run_dir: Path, attempt_id: str, role: str, payload: dict) -> None:
        directory = run_dir / "artifacts" / attempt_id / role
        directory.mkdir(parents=True, exist_ok=True)
        atomic(directory / "invocation.json", encoded(payload))

    def start(self, **kwargs):
        server = create(self.state_dir, port=0, **kwargs)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.streams.stopping.set)
        host, port = server.server_address[:2]
        return server, f"http://{host}:{port}"

    def get(self, base: str, path: str, **headers):
        request = Request(base + path, headers=headers)
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def status_of(self, base: str, path: str, **headers) -> int:
        try:
            request = Request(base + path, headers=headers)
            with urlopen(request, timeout=10) as response:
                return response.status
        except HTTPError as error:
            return error.code


class RouteTests(ServeTestCase):
    def test_health_and_run_listing_page_over_the_state_root(self):
        for index in range(3):
            self.make_run(f"run-{index}")
        _, base = self.start()
        self.assertEqual(self.get(base, "/health")["status"], "ok")
        first = self.get(base, "/api/runs?limit=2")
        self.assertEqual([item["run_id"] for item in first["items"]], ["run-0", "run-1"])
        second = self.get(base, f"/api/runs?after={first['next_after']}&limit=2")
        self.assertEqual([item["run_id"] for item in second["items"]], ["run-2"])
        self.assertIsNone(second["next_after"])

    def test_task_attempt_and_event_cursors_round_trip(self):
        run_dir = self.make_run("run-one", ticket("a"), ticket("b"))
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            store.start_attempt("a", "base", "/workspace/a")
        _, base = self.start()
        tasks = self.get(base, "/api/runs/run-one/tasks?limit=1")
        self.assertEqual([task["id"] for task in tasks["items"]], ["a"])
        remainder = self.get(base, f"/api/runs/run-one/tasks?after={tasks['next_after']}")
        self.assertEqual([task["id"] for task in remainder["items"]], ["b"])
        attempts = self.get(base, "/api/runs/run-one/attempts")
        self.assertEqual([attempt["task_id"] for attempt in attempts["items"]], ["a"])
        events = self.get(base, "/api/runs/run-one/events?limit=1")
        self.assertEqual(events["items"][0]["kind"], "run")
        after = self.get(base, f"/api/runs/run-one/events?after={events['next_after']}")
        self.assertTrue(all(event["id"] > events["next_after"] for event in after["items"]))

    def test_run_summary_reports_the_resolved_run_directory(self):
        run_dir = self.make_run("run-one")
        _, base = self.start()
        summary = self.get(base, "/api/runs/run-one")
        self.assertEqual(summary["run_dir"], str(run_dir.resolve()))
        self.assertEqual(summary["status"], "created")

    def test_rejected_cursors_and_limits_are_client_errors(self):
        self.make_run("run-one")
        _, base = self.start()
        self.assertEqual(self.status_of(base, "/api/runs/run-one/events?after=abc"), 400)
        self.assertEqual(self.status_of(base, "/api/runs/run-one/tasks?limit=100000"), 400)
        self.assertEqual(self.status_of(base, "/api/runs/run-one/tasks?limit=1&limit=2"), 400)
        self.assertEqual(self.status_of(base, "/api/runs/run-one/nowhere"), 404)
        self.assertEqual(self.status_of(base, "/nowhere"), 404)

    def test_unreadable_ledger_is_not_found_but_leaves_siblings_listable(self):
        self.make_run("run-one")
        broken = self.state_dir / "run-two"
        broken.mkdir()
        (broken / "state.sqlite").write_text("not a database")
        _, base = self.start()
        self.assertEqual(self.status_of(base, "/api/runs/run-two"), 404)
        listing = self.get(base, "/api/runs")
        self.assertEqual([item["run_id"] for item in listing["items"]], ["run-one", "run-two"])
        self.assertIn("unreadable", listing["items"][1])
        self.assertNotIn("unreadable", listing["items"][0])


class IsolationTests(ServeTestCase):
    def test_traversal_and_symlinked_runs_are_refused(self):
        self.make_run("run-one")
        outside = Path(self.directory.name).parent / "anvil-outside-run"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.is_dir() else None)
        (self.state_dir / "linked").symlink_to(outside, target_is_directory=True)
        _, base = self.start()
        self.assertEqual(self.status_of(base, "/api/runs/linked"), 404)
        self.assertEqual(self.status_of(base, "/api/runs/..%2f..%2fetc"), 404)
        self.assertEqual(self.status_of(base, "/api/runs/run%20one"), 404)
        self.assertEqual(self.status_of(base, "/api/runs/" + "x" * 129), 404)

    def test_non_loopback_host_header_is_refused(self):
        self.make_run("run-one")
        server, base = self.start()
        port = server.server_address[1]
        self.assertEqual(self.status_of(base, "/api/runs", Host=f"anvil.example:{port}"), 403)
        self.assertEqual(self.status_of(base, "/api/runs", Host=f"localhost:{port}"), 200)

    def test_binding_beyond_loopback_is_refused(self):
        with self.assertRaisesRegex(ContractError, "loopback-only"):
            create(self.state_dir, host="0.0.0.0", port=0)
        with self.assertRaisesRegex(ContractError, "state directory"):
            create(self.state_dir / "absent", port=0)
        with self.assertRaisesRegex(ContractError, "max streams"):
            create(self.state_dir, port=0, max_streams=0)

    def test_responses_carry_no_cross_origin_permission(self):
        self.make_run("run-one")
        _, base = self.start()
        with urlopen(base + "/api/runs", timeout=10) as response:
            self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")


class TelemetryRouteTests(ServeTestCase):
    def prepare(self) -> tuple[Path, str]:
        run_dir = self.make_run("run-one", ticket("a"), ticket("b"))
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            first = store.start_attempt("a", "base", "/workspace/a")
            store.record_message("a", attempt_id=first, kind="routing_decision",
                                 body={"model": "opus", "effort": "high"})
            store.record_message("a", attempt_id=first, kind="review_started", body={})
            second = store.start_attempt("b", "base", "/workspace/b")
        self.record_invocation(run_dir, first, "worker", invocation("worker", 0.25))
        self.record_invocation(run_dir, first, "review", invocation("review", 0.75))
        self.record_invocation(run_dir, second, "worker", invocation("worker", 0.5))
        return run_dir, second

    def test_telemetry_joins_cost_to_its_routing_decision(self):
        self.prepare()
        _, base = self.start()
        page = self.get(base, "/api/runs/run-one/telemetry")
        by_task = {item["task_id"]: item for item in page["items"]}
        self.assertEqual(by_task["a"]["decision"], {"model": "opus", "effort": "high"})
        self.assertAlmostEqual(by_task["a"]["cost_usd"], 1.0)
        self.assertIsNone(by_task["b"]["decision"])
        self.assertEqual(page["cost_kind"], "estimated; not a subscription invoice")

    def test_a_review_that_never_started_is_zero_not_unknown(self):
        self.prepare()
        _, base = self.start()
        page = self.get(base, "/api/runs/run-one/telemetry")
        record = next(item for item in page["items"] if item["task_id"] == "b")
        review = next(entry for entry in record["invocations"] if entry["role"] == "review")
        self.assertEqual((review["cost_usd"], review["cost_kind"]), (0, "not_started"))
        self.assertAlmostEqual(record["cost_usd"], 0.5)
        self.assertTrue(page["cost_complete"])

    def test_missing_accounting_for_a_started_role_stays_unknown(self):
        run_dir, _ = self.prepare()
        _, base = self.start()
        listed = self.get(base, "/api/runs/run-one/attempts")["items"]
        started = next(entry["id"] for entry in listed if entry["task_id"] == "a")
        (run_dir / "artifacts" / started / "review" / "invocation.json").unlink()
        page = self.get(base, "/api/runs/run-one/telemetry")
        record = next(item for item in page["items"] if item["task_id"] == "a")
        review = next(entry for entry in record["invocations"] if entry["role"] == "review")
        self.assertEqual((review["cost_usd"], review["cost_kind"]), (None, "unknown"))
        self.assertIsNone(record["cost_usd"])
        self.assertFalse(page["cost_complete"])
        self.assertAlmostEqual(page["known_cost_usd"], 0.75)

    def test_telemetry_shares_the_attempt_cursor(self):
        self.prepare()
        _, base = self.start()
        first = self.get(base, "/api/runs/run-one/telemetry?limit=1")
        self.assertEqual(len(first["items"]), 1)
        attempts = self.get(base, "/api/runs/run-one/attempts?limit=1")
        self.assertEqual(first["next_after"], attempts["next_after"])
        second = self.get(base, f"/api/runs/run-one/telemetry?after={first['next_after']}")
        self.assertEqual([item["task_id"] for item in second["items"]], ["b"])


class StreamTests(ServeTestCase):
    def read_stream(self, base: str, path: str, **headers) -> list[dict]:
        return [event for event in self.read_frames(base, path, **headers)
                if event["event"] not in ("meta", "end")]

    def read_frames(self, base: str, path: str, **headers) -> list[dict]:
        request = Request(base + path, headers=headers)
        frames, identifier, name = [], None, "message"
        with urlopen(request, timeout=20) as response:
            for raw in response:
                line = raw.decode().rstrip("\n")
                if line.startswith("id: "):
                    identifier = int(line[4:])
                elif line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    frames.append({"id": identifier, "event": name, **json.loads(line[6:])})
                    identifier, name = None, "message"
        return frames

    def finished_run(self) -> Path:
        run_dir = self.make_run("run-one", ticket("a"))
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
            store.set_run("blocked", "stopped for the test")
        return run_dir

    def test_a_terminal_run_streams_its_history_and_ends(self):
        self.finished_run()
        _, base = self.start()
        events = self.read_stream(base, "/api/runs/run-one/stream")
        self.assertEqual([event["to_status"] for event in events],
                         ["created", "running", "blocked"])
        self.assertEqual([event["id"] for event in events], [1, 2, 3])

    def test_last_event_id_resumes_without_replaying(self):
        self.finished_run()
        _, base = self.start()
        events = self.read_stream(base, "/api/runs/run-one/stream", **{"Last-Event-ID": "2"})
        self.assertEqual([event["id"] for event in events], [3])
        self.assertEqual(self.status_of(base, "/api/runs/run-one/stream",
                                        **{"Last-Event-ID": "nine"}), 400)

    def test_last_event_id_takes_precedence_over_the_query_parameter(self):
        self.finished_run()
        _, base = self.start()
        events = self.read_stream(base, "/api/runs/run-one/stream?after=0",
                                  **{"Last-Event-ID": "2"})
        self.assertEqual([event["id"] for event in events], [3])

    def test_a_stream_announces_its_contract_without_consuming_an_id(self):
        self.finished_run()
        _, base = self.start()
        frames = self.read_frames(base, "/api/runs/run-one/stream")
        self.assertEqual(frames[0]["event"], "meta")
        self.assertEqual(frames[0]["api_version"], API_VERSION)
        self.assertIsNone(frames[0]["id"])

    def test_a_terminal_run_ends_with_a_named_event_not_a_comment(self):
        self.finished_run()
        _, base = self.start()
        frames = self.read_frames(base, "/api/runs/run-one/stream")
        self.assertEqual(frames[-1]["event"], "end")
        # Unidentified, so a client's resumption point is the last ledger event.
        self.assertIsNone(frames[-1]["id"])
        self.assertEqual(frames[-2]["id"], 3)

    def test_streams_are_capped(self):
        self.make_run("run-one")
        with RunStore(self.state_dir / "run-one" / "state.sqlite") as store:
            store.set_run("running")
        server, base = self.start(max_streams=1)
        opened = urlopen(Request(base + "/api/runs/run-one/stream"), timeout=10)
        self.addCleanup(opened.close)
        self.assertEqual(self.status_of(base, "/api/runs/run-one/stream"), 503)
        server.streams.stopping.set()

    def test_streaming_an_unreadable_run_is_not_found(self):
        _, base = self.start()
        self.assertEqual(self.status_of(base, "/api/runs/absent/stream"), 404)


class ContractVersionTests(ServeTestCase):
    def test_every_json_body_carries_the_api_version(self):
        run_dir = self.make_run("run-one")
        with RunStore(run_dir / "state.sqlite") as store:
            store.set_run("running")
        _, base = self.start()
        for path in ("/health", "/api/runs", "/api/runs/run-one", "/api/runs/run-one/tasks",
                     "/api/runs/run-one/attempts", "/api/runs/run-one/events",
                     "/api/runs/run-one/telemetry"):
            self.assertEqual(self.get(base, path)["api_version"], API_VERSION, path)

    def test_errors_carry_the_api_version_too(self):
        self.make_run("run-one")
        _, base = self.start()
        request = Request(base + "/api/runs/absent")
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=10)
        self.assertEqual(json.loads(caught.exception.read())["api_version"], API_VERSION)


class PageTests(ServeTestCase):
    def test_the_page_and_its_assets_are_served_with_a_restrictive_policy(self):
        self.make_run("run-one")
        _, base = self.start()
        with urlopen(base + "/", timeout=10) as response:
            body = response.read().decode()
            self.assertIn("<title>Anvil runs</title>", body)
            policy = response.headers.get("Content-Security-Policy")
            self.assertIn("default-src 'none'", policy)
            self.assertIn("script-src 'self'", policy)
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        for name, kind in (("/app.css", "text/css"), ("/app.js", "text/javascript")):
            with urlopen(base + name, timeout=10) as response:
                self.assertIn(kind, response.headers.get("Content-Type"))

    def test_assets_are_an_allowlist_not_a_path_join(self):
        self.make_run("run-one")
        _, base = self.start()
        for path in ("/app.py", "/../serve.py", "/assets/app.js", "/%2e%2e/store.py"):
            self.assertEqual(self.status_of(base, path), 404, path)

    def test_the_page_never_builds_markup_from_ledger_content(self):
        source = (Path(anvil.serve.__file__).parent / "assets" / "app.js").read_text()
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("insertAdjacentHTML", source)
        self.assertNotIn("document.write", source)


if __name__ == "__main__":
    unittest.main()
