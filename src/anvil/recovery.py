"""Explicit same-ledger recovery; no old worktree or process is adopted."""
from dataclasses import replace
import json
import os
import socket
from pathlib import Path
import uuid

from .config import RunConfig, WorkerConfig
from .contracts import ContractError
from .evidence import validate_result
from .planning import TaskGraph
from .store import RunStore, _now, _encode
from .ticket_status import Publisher, digest, read_regular
from .workspaces import WorkspaceError

PROTOCOL = 1


def track_commands(scope, run_dir):
    scope.command_dir = Path(run_dir) / "commands"
    scope.command_dir.mkdir(exist_ok=True)


def mark_supported(store):
    with store._transaction() as db:
        store._event(db, _now(), "recovery_protocol", None, None, None, "running", {"version": PROTOCOL, "host": socket.gethostname()})


def assert_quiescent(run_dir):
    directory = Path(run_dir) / "commands"
    if not directory.is_dir():
        raise ContractError("run has no persistent command registry")
    for path in directory.glob("*.json"):
        record = json.loads(path.read_text())
        pgid = record.get("pgid")
        if record.get("phase") != "running" or type(pgid) is not int or pgid <= 1:
            raise ContractError(f"unresolved command spawn; inspect {path} before recovery")
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        raise ContractError(f"command process group {pgid} may still be alive; recovery refused")


def saved_graph(store):
    rows = store._connection.execute("SELECT input FROM tasks ORDER BY position")
    return TaskGraph.from_document({"version": 1, "tasks": [json.loads(r[0]) for r in rows]})


def _accepted(repo, task, details, base, config):
    validate_result(details.get("worker"), task)
    if details["worker"]["status"] != "completed":
        raise ContractError("accepted ticket lacks completed worker evidence")
    validate_result(details.get("review"), task, review=True)
    if details["review"]["verdict"] != "approve":
        raise ContractError("accepted ticket lacks review approval")
    sha = details.get("integration_sha")
    if not sha or details.get("reviewed_sha") != sha or details.get("verified_sha") != sha:
        raise ContractError("acceptance revisions disagree")
    if details.get("expected_base") != base or details.get("integrated_sha", sha) != sha:
        raise ContractError("acceptance base or integrated revision disagrees")
    checks = details.get("verification", [])
    if (len(checks) != len(config.verification) or
        any(c.get("argv") != list(argv) or c.get("returncode") != 0 or c.get("timed_out") is not False
            for c, argv in zip(checks, config.verification))):
        raise ContractError("acceptance lacks passing configured verification")
    if repo._commit(sha) != sha:
        raise ContractError("acceptance must name an immutable commit")
    repo._single_parent(sha, base)
    return sha


def _candidate(repo, task, state, snapshot):
    details = state["details"]
    carried = details.get("recovery_candidate")
    if carried:
        details = carried
    try:
        claims = details.get("worker")
        validate_result(claims, task)
        if claims["status"] != "completed":
            return None
        # Even a rejection recorded just before a refused retry disqualifies reuse.
        if details.get("failure_category") or details.get("review", {}).get("verdict") == "request_changes":
            return None
        if any(c.get("returncode") != 0 or c.get("timed_out") for c in details.get("verification", [])):
            return None
        for event in snapshot["events"]:
            if event["attempt_id"] == state["attempt_id"]:
                body = event["details"].get("body", {})
                if body.get("verdict") == "request_changes":
                    return None
        attempt = next((a for a in snapshot["attempts"] if a["id"] == state["attempt_id"]), None)
        base = carried["base_sha"] if carried else attempt["base_sha"]
        sha = details["candidate_sha"]
        if repo._commit(sha) != sha:
            return None
        repo._single_parent(sha, base)
        if repo.git("rev-parse", f"{sha}^{{tree}}") == repo.git("rev-parse", f"{base}^{{tree}}"):
            return None
        if repo.control_ticket and repo.git("diff", "--name-only", base, sha, "--", repo.control_ticket):
            return None
        return {"candidate_sha": sha, "worker": claims, "base_sha": base}
    except (ValueError, TypeError, KeyError, StopIteration, WorkspaceError):
        return None


def prepare(store, config, repo, run_dir):
    """Validate everything before the atomic ownership reset. Caller holds repo lock."""
    snapshot = store.snapshot()
    if snapshot["status"] not in ("running", "created", "interrupted"):
        raise ContractError("resume requires an interrupted or abandoned active run")
    if any(t["status"] in ("failed", "blocked") for t in snapshot["tasks"]):
        raise ContractError("resume cannot retry failed or blocked tickets")
    if not any(e["kind"] == "recovery_protocol" and e["details"].get("version") == PROTOCOL
               for e in snapshot["events"]):
        raise ContractError("run predates native recovery; inspect its preserved work manually")
    marker = next(e for e in snapshot["events"] if e["kind"] == "recovery_protocol")
    if marker["details"].get("host") != socket.gethostname():
        raise ContractError("native recovery requires the original host")
    if Path(run_dir).resolve() != (config.state_dir / snapshot["run_id"]).resolve() or str(config.repo) != snapshot["repo"]:
        raise ContractError("native recovery requires the original run and repository paths")
    assert_quiescent(run_dir)
    graph = saved_graph(store)
    current = TaskGraph.load(config.tickets)
    def inputs(g):
        return [{k: v for k, v in t.to_dict().items() if k != "execution"} for t in g.tasks]
    if inputs(graph) != inputs(current):
        raise ContractError("ticket inputs changed since the run started")
    if config.ticket_status:
        publisher = Publisher(store, config.tickets, repo)
        state = publisher.state()
        if json.loads(read_regular(publisher.binding))["run_id"] != snapshot["run_id"]:
            raise ContractError("ticket source belongs to a newer run")
        allowed = {state["expected"]}
        if state.get("pending") is not None:
            allowed.add(digest(state["pending"].encode()))
        if digest(read_regular(config.tickets)) not in allowed:
            raise ContractError("ticket source changed outside publication")
    tasks = {t.id: t for t in graph.tasks}
    base, accepted = snapshot["base_sha"], {}
    if snapshot["branch"] != "anvil/" + snapshot["run_id"]:
        raise ContractError("run branch is not the recorded managed branch")
    for event in snapshot["events"]:
        if event["kind"] != "task" or event["to_status"] != "done":
            continue
        tid = event["task_id"]
        if tid in accepted or not set(tasks[tid].depends_on) <= accepted.keys():
            raise ContractError("completion history violates dependency ordering")
        base = _accepted(repo, tasks[tid], event["details"], base, config)
        accepted[tid] = event["details"]
    for state in snapshot["tasks"]:
        if (state["status"] == "done") != (state["id"] in accepted):
            raise ContractError("completion history disagrees with ticket state")
    if "branch refs/heads/" + snapshot["branch"] in repo.git("worktree", "list", "--porcelain", "-z").split("\0"):
        raise ContractError("managed branch is checked out; recovery refused")
    missing_branch = False
    try:
        tip = repo._commit("refs/heads/" + snapshot["branch"])
    except WorkspaceError:
        if snapshot["attempts"] or accepted:
            raise
        # Startup may have stopped before the initial branch was created.
        tip, missing_branch = base, True
    reconcile = None
    if tip != base:
        for state in snapshot["tasks"]:
            if state["id"] in accepted:
                continue
            intents = [e for e in snapshot["events"] if e["kind"] == "task" and
                       e["to_status"] == "integrating" and e["attempt_id"] == state["attempt_id"]]
            if not intents or intents[-1]["details"].get("integration_sha") != tip:
                continue
            if not set(tasks[state["id"]].depends_on) <= accepted.keys():
                raise ContractError("integration intent has unfinished prerequisites")
            base = _accepted(repo, tasks[state["id"]], intents[-1]["details"], base, config)
            reconcile = state["id"]
            accepted[reconcile] = intents[-1]["details"] | {"integrated_sha": base}
            break
        if tip != base:
            raise ContractError("managed branch moved without a verified integration intent")
    reusable = {s["id"]: _candidate(repo, tasks[s["id"]], s, snapshot)
                for s in snapshot["tasks"] if s["id"] not in accepted}
    generation = uuid.uuid4().hex
    if missing_branch:
        repo.create_branch(snapshot["branch"], base)
    with store._transaction() as db:
        timestamp = _now()
        for state in snapshot["tasks"]:
            tid, aid = state["id"], state["attempt_id"]
            if tid == reconcile:
                details = accepted[tid]
                db.execute("UPDATE tasks SET status='done', details=?, updated_at=? WHERE id=?",
                           (_encode(details), timestamp, tid))
                db.execute("UPDATE attempts SET status='done', details=?, updated_at=?, finished_at=? WHERE id=?",
                           (_encode(details), timestamp, timestamp, aid))
                store._event(db, timestamp, "task", tid, aid, state["status"], "done", details)
            elif tid not in accepted:
                if aid:
                    db.execute("UPDATE attempts SET status='interrupted', finished_at=?, updated_at=? WHERE id=?",
                               (timestamp, timestamp, aid))
                details = {"recovery_candidate": reusable[tid]} if reusable[tid] else {}
                db.execute("UPDATE tasks SET status='pending', attempt_id=NULL, details=?, updated_at=? WHERE id=?",
                           (_encode(details), timestamp, tid))
                store._event(db, timestamp, "recovery_retired", tid, aid, state["status"], "pending", details)
        db.execute("UPDATE runs SET status='running', error=NULL, updated_at=? WHERE singleton=1", (timestamp,))
        store._event(db, timestamp, "recovery", None, None, snapshot["status"], "running",
                     {"generation": generation, "base_sha": base, "reconciled": reconcile})
    return base, generation, accepted, reusable


def resume(run_dir, *, runners=None, review_runner=None, progress=None):
    run_dir = Path(run_dir).expanduser().resolve()
    snapshot = RunStore.read(run_dir / "state.sqlite")
    config = RunConfig.from_document(snapshot["config"], base=run_dir)
    if not config.workers:
        config = replace(config, workers=(WorkerConfig("serial", config.agent, config.executable),), max_processes=1)
    from .parallel import run_parallel
    return run_parallel(config, runners=runners, review_runner=review_runner,
                        progress=progress, resume_dir=run_dir)


def frozen_policy(run_dir):
    """Bind the frozen policy artifact to its committed ledger fingerprint."""
    from .routing import fingerprint
    run_dir = Path(run_dir).expanduser().resolve()
    saved = RunStore.read(run_dir / "state.sqlite")
    value = json.loads(read_regular(run_dir / "routing-frozen.json"))
    bindings = [e["details"]["digest"] for e in saved["events"] if e["kind"] == "routing_frozen"]
    if bindings != [fingerprint(value)]:
        raise ContractError("frozen routing policy changed or was not durably recorded")
    return value
