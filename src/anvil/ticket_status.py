"""Idempotent local ticket publication; run events are the durable outbox."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import tempfile

from .contracts import ContractError, _object_fields

STATUSES = ("todo", "in_progress", "ready_for_review", "in_review", "verifying", "done",
            "blocked", "failed", "interrupted")
FIELDS = {"status", "run_id", "attempt_id", "worker", "updated_at", "reason", "evidence",
          "candidate_sha", "integrated_sha", "event_id"}
MAP = dict(pending="todo", running="in_progress", candidate="ready_for_review",
           reviewed="verifying", verified="verifying", integrating="verifying", done="done",
           blocked="blocked", failed="failed", interrupted="interrupted")


def validate_metadata(value):
    _object_fields(value, {"status"}, FIELDS - {"status"}, "execution")
    if value["status"] not in STATUSES:
        raise ContractError("invalid execution.status")
    for key, item in value.items():
        if key == "event_id":
            if type(item) is not int or item < 0:
                raise ContractError("execution.event_id must be a nonnegative integer")
        elif not isinstance(item, str) or not item or "\0" in item:
            raise ContractError(f"execution.{key} must be nonempty text")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ContractError(f"ticket status path must not contain symlinks: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ContractError("ticket source must be a regular file")
        data = stream.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ContractError("ticket source exceeds 16 MiB")
    return data


def document(data):
    from .planning import TaskGraph, _unique_object, _reject_constant
    doc = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    TaskGraph.from_document(doc)
    return doc


def inputs(doc):
    return {"version": doc["version"], "tasks": [
        {k: v for k, v in task.items() if k != "execution"} for task in doc["tasks"]]}


def atomic(path, data):
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix=".anvil-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encoded(value):
    return (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()


@contextmanager
def destination_lock(source):
    # One lock across repositories and state roots; no generated file in the source checkout.
    path = Path(tempfile.gettempdir()) / f"anvil-ticket-{os.getuid()}-{digest(str(source).encode())}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise ContractError("ticket lock has a different owner")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class Publisher:
    def __init__(self, store, source, repo, *, fresh=False):
        self.store, self.source = store, Path(source).absolute()
        self.repo = repo
        self.binding = repo.common_dir / ("anvil-ticket-" + digest(str(self.source).encode()) + ".json")
        self.error = None
        if fresh:
            with destination_lock(self.source):
                data = read_regular(self.source)
                from .planning import TaskGraph
                canonical = [t.to_dict() for t in TaskGraph.from_document(document(data)).tasks]
                expected = [json.loads(r[0]) for r in store._connection.execute("SELECT input FROM tasks ORDER BY position")]
                if inputs({"version":1,"tasks":canonical}) != inputs({"version":1,"tasks":expected}):
                    raise ContractError("ticket inputs changed between planning and publication setup")
                row = store.snapshot()
                state = {"source": str(self.source), "run_id": row["run_id"],
                         "expected": digest(data), "pending": None, "cursor": 0}
                store._connection.execute("CREATE TABLE publication (singleton INTEGER PRIMARY KEY, data TEXT NOT NULL)")
                store._connection.execute("INSERT INTO publication VALUES (1, ?)", (json.dumps(state),))
                atomic(self.binding, encoded({"run_id": row["run_id"]}))
        else:
            self.state()  # Refuse old/unconfigured runs.

    def state(self):
        row = self.store._connection.execute("SELECT data FROM publication WHERE singleton = 1").fetchone()
        if row is None:
            raise ContractError("run has no ticket publication state")
        return json.loads(row[0])

    def save(self, state):
        # No callback recursion: events are already committed before this cursor moves.
        self.store._connection.execute("UPDATE publication SET data = ? WHERE singleton = 1",
                                       (json.dumps(state),))

    def flush(self):
        try:
            with destination_lock(self.source):
                state = self.state()
                if json.loads(read_regular(self.binding))["run_id"] != state["run_id"]:
                    raise ContractError("ticket source belongs to a newer run; refusing stale publication")
                raw = read_regular(self.source)
                current = digest(raw)
                if state["pending"] is not None and current == digest(state["pending"].encode()):
                    state["expected"] = current
                    state["cursor"] = state.pop("pending_cursor")
                    state["pending"] = None
                    self.save(state)
                if current != state["expected"]:
                    raise ContractError("ticket source changed outside publication; preserve edits and reconcile explicitly")
                snapshot = self.store.snapshot()
                cursor = snapshot["events"][-1]["id"]
                if cursor == state["cursor"]:
                    self.error = None
                    return
                doc = document(raw)
                tasks = {task["id"]: task for task in snapshot["tasks"]}
                for target in doc["tasks"]:
                    task = tasks[target["id"]]
                    status = MAP[task["status"]]
                    # Review-start is an evidence event, not a new acceptance phase.
                    events = [e for e in snapshot["events"] if e["task_id"] == task["id"]]
                    if task["status"] == "candidate" and any(e["attempt_id"] == task["attempt_id"] and e["details"].get("message_kind") == "review_started" for e in events):
                        status = "in_review"
                    value = {"status": status, "run_id": snapshot["run_id"], "event_id": cursor,
                             "updated_at": task["updated_at"], "evidence": str(self.store.path.parent)}
                    if task["attempt_id"]:
                        value["attempt_id"] = task["attempt_id"]
                    for field in ("candidate_sha", "integrated_sha"):
                        if task["details"].get(field):
                            value[field] = task["details"][field]
                    if task["details"].get("worker_id"):
                        value["worker"] = task["details"]["worker_id"]
                    reason = task["details"].get("error")
                    for key in ("worker", "review"):
                        result = task["details"].get(key, {})
                        reason = reason or "; ".join(result.get("blockers", []) or result.get("findings", []))
                    if status == "todo" and task["depends_on"]:
                        reason = "Waiting for dependencies: " + ", ".join(task["depends_on"])
                    if reason:
                        value["reason"] = reason
                    target["execution"] = value
                output = encoded(doc)
                # Prepared bytes allow replay when a crash follows replacement but precedes acknowledgement.
                state["pending"], state["pending_cursor"] = output.decode(), cursor
                self.save(state)
                if digest(read_regular(self.source)) != state["expected"]:
                    raise ContractError("ticket source changed during publication")
                atomic(self.source, output)
                state["expected"], state["cursor"] = digest(output), cursor
                state["pending"] = None
                state.pop("pending_cursor", None)
                self.save(state)
                self.error = None
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            self.error = str(exc)

    def report(self):
        return {"status": "status_sync_pending" if self.error else "synchronized",
                "error": self.error, "source": str(self.source)}


def attach(store, config, repo):
    if not config.ticket_status:
        return None
    publisher = Publisher(store, config.tickets, repo, fresh=True)
    store.after_commit = publisher.flush
    publisher.flush()
    return publisher


def sync(run_dir):
    from .store import RunStore
    from .workspaces import Repository, RepositoryLock
    saved = RunStore.read(Path(run_dir) / "state.sqlite")
    repo = Repository(Path(saved["repo"]))
    with RepositoryLock(repo), RunStore(Path(run_dir) / "state.sqlite") as store:
        publisher = Publisher(store, saved["config"]["tickets"], repo)
        publisher.flush()
        return publisher.report()


def prepare_repo(repo, config):
    if not config.ticket_status:
        return
    source = config.tickets.absolute()
    document(read_regular(source))
    if source.is_relative_to(repo.path):
        relative = str(source.relative_to(repo.path))
        repo.control_ticket = relative
        # Only generated metadata may differ; all other files and staged edits stay protected.
        committed = document(repo.git("show", f"HEAD:{relative}").encode())
        if inputs(committed) != inputs(document(read_regular(source))):
            raise ContractError("ticket source has uncommitted input changes")
