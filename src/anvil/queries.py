"""Bounded, paginated read queries over a run's execution ledger.

Unlike ``RunStore.read()``, which loads a run's complete task, attempt, and
event history, these queries return one bounded page at a time so a dashboard
or other long-lived consumer can poll a run without repeatedly loading the
whole event history or relying on the final-only ``report.json``.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator, Sequence
import json
from pathlib import Path
import sqlite3

from .store import StoreError

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def _bounded(limit: int) -> int:
    if type(limit) is bool or not isinstance(limit, int) or limit < 1:
        raise StoreError("limit must be a positive integer")
    if limit > MAX_PAGE_SIZE:
        raise StoreError(f"limit cannot exceed {MAX_PAGE_SIZE} rows")
    return limit


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """Open one consistent, read-only snapshot transaction over a ledger."""
    connection = None
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        yield connection
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise StoreError(f"cannot read run ledger {path}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _require_run(connection: sqlite3.Connection, path: Path) -> None:
    if connection.execute("SELECT 1 FROM runs WHERE singleton = 1").fetchone() is None:
        raise StoreError(f"run ledger is not initialized: {path}")


def run_summary(run_dir: Path) -> dict:
    """The run row plus task-status counts, without attempts or events."""
    path = Path(run_dir) / "state.sqlite"
    with _read_only(path) as connection:
        row = connection.execute("SELECT * FROM runs WHERE singleton = 1").fetchone()
        if row is None:
            raise StoreError(f"run ledger is not initialized: {path}")
        summary = dict(row)
        del summary["singleton"]
        # A ledger written before anvil_version existed has no such column;
        # report the version as absent rather than guessing at one.
        summary.setdefault("anvil_version", None)
        # Where this run's saved state lives, so a reader can go straight to the
        # artifacts, worktrees and event streams the ledger only points at.
        summary["run_dir"] = str(Path(run_dir).resolve())
        summary["config"] = json.loads(summary["config"])
        counts = dict(connection.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status"))
        summary["task_counts"] = counts
        summary["task_total"] = sum(counts.values())
        return summary


def list_runs(state_dir: Path, *, after: str | None = None,
              limit: int = DEFAULT_PAGE_SIZE) -> dict:
    """A bounded page of run summaries for every ledger directly under state_dir.

    Pages are ordered by run directory name (the run ID) so pagination is
    stable across calls even as new runs are created.

    A run whose ledger cannot be read still occupies its place in the page as
    ``{"run_id": ..., "unreadable": ...}``. One crashed or unreadable run must
    not hide every other run from a consumer listing them. The key is not
    ``error``: the runs table has its own nullable ``error`` column, and a run
    that recorded a failure is not the same as a ledger that cannot be read.
    """
    limit = _bounded(limit)
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return {"items": [], "next_after": None}
    run_ids = sorted(
        entry.name for entry in state_dir.iterdir()
        if entry.is_dir() and (entry / "state.sqlite").is_file()
    )
    if after is not None:
        run_ids = [run_id for run_id in run_ids if run_id > after]
    page = run_ids[:limit]
    items = []
    for run_id in page:
        try:
            items.append(run_summary(state_dir / run_id))
        except StoreError as exc:
            items.append({"run_id": run_id, "unreadable": str(exc)})
    next_after = page[-1] if len(run_ids) > limit else None
    return {"items": items, "next_after": next_after}


def run_tasks(run_dir: Path, *, after: int | None = None,
              limit: int = DEFAULT_PAGE_SIZE) -> dict:
    """A bounded page of per-run task summaries ordered by position."""
    limit = _bounded(limit)
    if after is not None and (type(after) is bool or not isinstance(after, int) or after < 0):
        raise StoreError("after must be a nonnegative integer position")
    path = Path(run_dir) / "state.sqlite"
    with _read_only(path) as connection:
        _require_run(connection, path)
        clause = "WHERE position > ?" if after is not None else ""
        params = (after,) if after is not None else ()
        rows = connection.execute(
            f"SELECT * FROM tasks {clause} ORDER BY position LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        page = rows[:limit]
        items = []
        for row in page:
            item = dict(row)
            source = json.loads(item.pop("input"))
            item.pop("position")
            item["details"] = json.loads(item["details"])
            items.append(source | item)
        next_after = page[-1]["position"] if len(rows) > limit else None
        return {"items": items, "next_after": next_after}


def run_attempts(run_dir: Path, *, after: int | None = None,
                  limit: int = DEFAULT_PAGE_SIZE) -> dict:
    """A bounded page of ticket attempts ordered by insertion (rowid)."""
    limit = _bounded(limit)
    if after is not None and (type(after) is bool or not isinstance(after, int) or after < 0):
        raise StoreError("after must be a nonnegative integer cursor")
    path = Path(run_dir) / "state.sqlite"
    with _read_only(path) as connection:
        _require_run(connection, path)
        clause = "WHERE rowid > ?" if after is not None else ""
        params = (after,) if after is not None else ()
        rows = connection.execute(
            f"SELECT rowid AS _cursor, * FROM attempts {clause} ORDER BY rowid LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
        page = rows[:limit]
        items = []
        for row in page:
            item = dict(row)
            del item["_cursor"]
            item["details"] = json.loads(item["details"])
            items.append(item)
        next_after = page[-1]["_cursor"] if len(rows) > limit else None
        return {"items": items, "next_after": next_after}


def run_attempts_all(run_dir: Path) -> list[dict]:
    """Every attempt across every page, for a whole-run total rather than one page."""
    items: list[dict] = []
    after = None
    while True:
        page = run_attempts(run_dir, after=after, limit=MAX_PAGE_SIZE)
        items.extend(page["items"])
        after = page["next_after"]
        if after is None:
            return items


def run_events(run_dir: Path, *, after: int = 0,
               limit: int = DEFAULT_PAGE_SIZE) -> dict:
    """A bounded page of ledger events with ``id`` strictly greater than after."""
    limit = _bounded(limit)
    if type(after) is bool or not isinstance(after, int) or after < 0:
        raise StoreError("after must be a nonnegative integer event ID")
    path = Path(run_dir) / "state.sqlite"
    with _read_only(path) as connection:
        _require_run(connection, path)
        rows = connection.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (after, limit + 1),
        ).fetchall()
        page = rows[:limit]
        items = []
        for row in page:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            items.append(item)
        next_after = page[-1]["id"] if len(rows) > limit else None
        return {"items": items, "next_after": next_after}


def attempt_exists(run_dir: Path, attempt_id: str) -> bool:
    """Whether the ledger records an attempt with this ID.

    Used only to decide whether an artifact path derived from the run
    directory and this ID may be resolved; it grants no read of the row.
    """
    if not isinstance(attempt_id, str) or not attempt_id:
        return False
    path = Path(run_dir) / "state.sqlite"
    try:
        with _read_only(path) as connection:
            _require_run(connection, path)
            row = connection.execute(
                "SELECT 1 FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            return row is not None
    except StoreError:
        return False


def attempt_messages(run_dir: Path, message_kind: str,
                     attempt_ids: Sequence[str]) -> dict[str, dict]:
    """The latest coordinator message body of one kind per attempt, for a bounded set.

    One indexed pass over the page's attempts, rather than one scan per attempt:
    ``message_kind`` lives inside the event ``details`` document, so a caller
    resolving it per attempt would rescan the whole event history each time.
    """
    identifiers = list(attempt_ids)
    if not all(isinstance(identifier, str) for identifier in identifiers):
        raise StoreError("attempt IDs must be strings")
    if len(identifiers) > MAX_PAGE_SIZE:
        raise StoreError(f"cannot index more than {MAX_PAGE_SIZE} attempts")
    if not isinstance(message_kind, str) or not message_kind:
        raise StoreError("message kind must be a non-empty string")
    if not identifiers:
        return {}
    path = Path(run_dir) / "state.sqlite"
    with _read_only(path) as connection:
        _require_run(connection, path)
        placeholders = ",".join("?" * len(identifiers))
        rows = connection.execute(
            "SELECT attempt_id, details FROM events "
            f"WHERE kind = 'message' AND attempt_id IN ({placeholders}) "
            "AND json_extract(details, '$.message_kind') = ? ORDER BY id",
            (*identifiers, message_kind),
        ).fetchall()
        # Ordered by event ID, so a later message replaces an earlier one.
        return {row["attempt_id"]: json.loads(row["details"]).get("body")
                for row in rows}
