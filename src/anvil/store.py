"""Transactional state and evidence for one bounded execution run."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .contracts import ContractError, Task
from .planning import TaskGraph


class StoreError(ValueError):
    """The ledger cannot accept the requested state change."""


_PHASES = ("running", "candidate", "reviewed", "verified", "integrating", "done")
_STOPPED = {"failed", "blocked", "interrupted"}
_RUN_TRANSITIONS = {"created": {"running", *_STOPPED}, "running": {"success", *_STOPPED}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        raise StoreError(f"ledger data must be JSON serializable: {exc}") from exc


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise StoreError(f"{name} must be a nonempty string")


class RunStore:
    """A single-run SQLite ledger; reopening a ledger never initializes it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise StoreError(f"cannot open run ledger {self.path}: {exc}") from exc
        self._connection = connection

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException as exc:
            connection.rollback()
            if isinstance(exc, sqlite3.Error):
                raise StoreError(f"run ledger operation failed: {exc}") from exc
            raise

    def initialize(
        self, *, run_id: str, repo: str, branch: str, base_sha: str,
        tasks: Sequence[Task], config: dict,
    ) -> None:
        """Create a new immutable input snapshot, refusing any existing schema."""
        for name, value in (("run_id", run_id), ("repo", repo),
                            ("branch", branch), ("base_sha", base_sha)):
            _text(value, name)
        if not isinstance(config, dict):
            raise StoreError("config must be an object")
        task_inputs = [task.to_dict() for task in tasks]
        try:
            TaskGraph.from_document({"version": 1, "tasks": task_inputs})
        except ContractError as exc:
            raise StoreError(str(exc)) from exc
        config_json = _encode(config)
        task_json = [_encode(task) for task in task_inputs]
        timestamp = _now()
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise StoreError("run ledger already exists; initialize never overwrites it")
            connection.execute("""
                CREATE TABLE runs (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    run_id TEXT NOT NULL UNIQUE, repo TEXT NOT NULL,
                    branch TEXT NOT NULL, base_sha TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, config TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE,
                    input TEXT NOT NULL, status TEXT NOT NULL,
                    attempt_id TEXT, details TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE attempts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                    base_sha TEXT NOT NULL, workspace TEXT NOT NULL,
                    status TEXT NOT NULL, details TEXT NOT NULL,
                    started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, kind TEXT NOT NULL,
                    task_id TEXT REFERENCES tasks(id),
                    attempt_id TEXT REFERENCES attempts(id),
                    from_status TEXT, to_status TEXT NOT NULL, details TEXT NOT NULL
                )
            """)
            connection.execute(
                "INSERT INTO runs VALUES (1, ?, ?, ?, ?, 'created', NULL, ?, ?, ?)",
                (run_id, repo, branch, base_sha, config_json, timestamp, timestamp),
            )
            connection.executemany(
                "INSERT INTO tasks VALUES (?, ?, ?, 'pending', NULL, '{}', ?, ?)",
                [(task["id"], position, encoded, timestamp, timestamp)
                 for position, (task, encoded) in enumerate(zip(task_inputs, task_json))],
            )
            self._event(connection, timestamp, "run", None, None, None, "created", {})

    @staticmethod
    def _run(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE singleton = 1").fetchone()
        if row is None:
            raise StoreError("run ledger is not initialized")
        return row

    @staticmethod
    def _event(
        connection: sqlite3.Connection, timestamp: str, kind: str,
        task_id: str | None, attempt_id: str | None,
        old: str | None, new: str, details: dict,
    ) -> None:
        connection.execute(
            "INSERT INTO events (created_at, kind, task_id, attempt_id, "
            "from_status, to_status, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (timestamp, kind, task_id, attempt_id, old, new, _encode(details)),
        )

    def set_run(self, status: str, error: str | None = None) -> None:
        if error is not None and not isinstance(error, str):
            raise StoreError("run error must be a string or null")
        with self._transaction() as connection:
            run = self._run(connection)
            if status not in _RUN_TRANSITIONS.get(run["status"], set()):
                raise StoreError(f"invalid run transition: {run['status']} -> {status}")
            if status == "success":
                counts = connection.execute(
                    "SELECT COUNT(*), SUM(status != 'done') FROM tasks"
                ).fetchone()
                if not counts[0] or counts[1]:
                    raise StoreError("run success requires every task to be done")
                if error is not None:
                    raise StoreError("a successful run cannot carry an error")
            timestamp = _now()
            connection.execute(
                "UPDATE runs SET status = ?, error = ?, updated_at = ? WHERE singleton = 1",
                (status, error, timestamp),
            )
            self._event(connection, timestamp, "run", None, None,
                        run["status"], status, {"error": error})

    def start_attempt(
        self, task_id: str, base_sha: str, workspace: str, *, attempt_id: str | None = None,
        worker_id: str | None = None, agent: str | None = None,
    ) -> str:
        """Claim atomically, with an optional reserved ID and worker assignment."""
        _text(base_sha, "base_sha")
        _text(workspace, "workspace")
        attempt_id = str(uuid4()) if attempt_id is None else attempt_id
        _text(attempt_id, "attempt_id")
        assignment = {}
        for name, value in (("worker_id", worker_id), ("agent", agent)):
            if value is not None:
                _text(value, name)
                assignment[name] = value
        encoded = _encode(assignment)
        with self._transaction() as connection:
            if self._run(connection)["status"] != "running":
                raise StoreError("starting an attempt requires a running run")
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise StoreError(f"unknown task: {task_id}")
            if task["status"] != "pending" or task["attempt_id"] is not None:
                raise StoreError(f"task {task_id} is not pending; retries are not supported")
            dependencies = json.loads(task["input"])["depends_on"]
            states = dict(connection.execute("SELECT id, status FROM tasks"))
            blocked = [dependency for dependency in dependencies if states.get(dependency) != "done"]
            if blocked:
                raise StoreError(f"task {task_id} has unfinished dependencies: {', '.join(blocked)}")
            timestamp = _now()
            connection.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, 'running', ?, ?, ?, NULL)",
                (attempt_id, task_id, base_sha, workspace, encoded, timestamp, timestamp),
            )
            connection.execute(
                "UPDATE tasks SET status = 'running', attempt_id = ?, details = ?, "
                "updated_at = ? WHERE id = ?",
                (attempt_id, encoded, timestamp, task_id),
            )
            self._event(connection, timestamp, "task", task_id, attempt_id,
                        "pending", "running", {"base_sha": base_sha, "workspace": workspace})
            if assignment:
                self._event(connection, timestamp, "dispatch", task_id, attempt_id,
                            "pending", "running", assignment | {
                                "base_sha": base_sha, "workspace": workspace,
                            })
            return attempt_id

    @classmethod
    def _active_task(
        cls, connection: sqlite3.Connection, task_id: str, attempt_id: str,
    ) -> sqlite3.Row:
        if cls._run(connection)["status"] != "running":
            raise StoreError("attempt updates require a running run")
        task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise StoreError(f"unknown task: {task_id}")
        if task["attempt_id"] is None or task["attempt_id"] != attempt_id:
            raise StoreError(f"stale or missing attempt for task {task_id}")
        if task["status"] not in _PHASES[:-1]:
            raise StoreError(f"task {task_id} is terminal or has not started: {task['status']}")
        return task

    def heartbeat(self, task_id: str, *, attempt_id: str) -> None:
        """Refresh an active owner's heartbeat without adding audit events."""
        with self._transaction() as connection:
            task = self._active_task(connection, task_id, attempt_id)
            timestamp = _now()
            details = json.loads(task["details"])
            details["heartbeat_at"] = timestamp
            encoded = _encode(details)
            connection.execute(
                "UPDATE tasks SET details = ?, updated_at = ? WHERE id = ?",
                (encoded, timestamp, task_id),
            )
            connection.execute(
                "UPDATE attempts SET details = ?, updated_at = ? WHERE id = ?",
                (encoded, timestamp, attempt_id),
            )

    def record_message(
        self, task_id: str, *, attempt_id: str, kind: str, body: dict,
    ) -> None:
        """Persist a coordinator message tied to its current active owner."""
        _text(kind, "message kind")
        if not isinstance(body, dict):
            raise StoreError("message body must be an object")
        with self._transaction() as connection:
            task = self._active_task(connection, task_id, attempt_id)
            self._event(connection, _now(), "message", task_id, attempt_id,
                        task["status"], task["status"], {"message_kind": kind, "body": body})

    @staticmethod
    def _require_evidence(status: str, details: dict) -> None:
        phase = _PHASES.index(status)
        if phase >= _PHASES.index("candidate"):
            _text(details.get("candidate_sha"), "candidate_sha")
        if phase >= _PHASES.index("reviewed") and not isinstance(details.get("review"), dict):
            raise StoreError("reviewed requires a review object")
        if phase >= _PHASES.index("verified"):
            verification = details.get("verification")
            if not isinstance(verification, list) or not verification:
                raise StoreError("verified requires a nonempty verification list")
        if phase >= _PHASES.index("integrating"):
            _text(details.get("integration_sha"), "integration_sha")
        if status == "done":
            _text(details.get("integrated_sha"), "integrated_sha")

    def transition(
        self, task_id: str, status: str, *, attempt_id: str, details: dict | None = None,
    ) -> None:
        if details is not None and not isinstance(details, dict):
            raise StoreError("transition details must be an object")
        with self._transaction() as connection:
            if self._run(connection)["status"] != "running":
                raise StoreError("task transitions require a running run")
            task = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise StoreError(f"unknown task: {task_id}")
            if task["attempt_id"] is None or task["attempt_id"] != attempt_id:
                raise StoreError(f"stale or missing attempt for task {task_id}")
            old = task["status"]
            if old not in _PHASES[:-1]:
                raise StoreError(f"task {task_id} is terminal or has not started: {old}")
            expected = _PHASES[_PHASES.index(old) + 1]
            if status != expected and status not in _STOPPED:
                raise StoreError(f"invalid task transition: {old} -> {status}")
            merged = json.loads(task["details"])
            merged.update(details or {})
            if status not in _STOPPED:
                self._require_evidence(status, merged)
            encoded = _encode(merged)
            timestamp = _now()
            finished_at = timestamp if status in {"done", *_STOPPED} else None
            connection.execute(
                "UPDATE tasks SET status = ?, details = ?, updated_at = ? WHERE id = ?",
                (status, encoded, timestamp, task_id),
            )
            connection.execute(
                "UPDATE attempts SET status = ?, details = ?, updated_at = ?, "
                "finished_at = ? WHERE id = ?",
                (status, encoded, timestamp, finished_at, attempt_id),
            )
            self._event(connection, timestamp, "task", task_id, attempt_id, old, status, merged)

    @staticmethod
    def _snapshot(connection: sqlite3.Connection) -> dict:
        run = dict(RunStore._run(connection))
        del run["singleton"]
        run["config"] = json.loads(run["config"])
        tasks = []
        for row in connection.execute("SELECT * FROM tasks ORDER BY position"):
            item = dict(row)
            source = json.loads(item.pop("input"))
            item.pop("position")
            item["details"] = json.loads(item["details"])
            tasks.append(source | item)
        run["tasks"] = tasks
        for table in ("attempts", "events"):
            order = "started_at, id" if table == "attempts" else "id"
            rows = []
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                item = dict(row)
                item["details"] = json.loads(item["details"])
                rows.append(item)
            run[table] = rows
        return run

    def snapshot(self) -> dict:
        with self._transaction(write=False) as connection:
            return self._snapshot(connection)

    @classmethod
    def read(cls, path: Path) -> dict:
        """Read an existing ledger without creating a database or its parents."""
        connection = None
        try:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            return cls._snapshot(connection)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise StoreError(f"cannot read run ledger {path}: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
