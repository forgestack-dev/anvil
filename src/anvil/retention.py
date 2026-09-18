"""Inspect and remove the refs a run retains for its candidate revisions.

`Repository.retain` names every candidate and integration revision a run
creates, so a rejected attempt stays auditable after the run that discarded it.
Those refs accumulate one pair per attempt and would otherwise grow without
bound in the operator's repository, which AGENTS.md forbids.

Removal is explicit and never automatic. Nothing in a run reads these refs, so
deleting them changes no decision; what it can destroy is evidence a ledger
still points at, and `delete` refuses exactly that case.

That ties a ref's lifetime to its ledger's: while a run directory survives so do
its refs, and removing the directory releases them. The pair is reclaimed
together under whatever retention the operator already applies to the state
root, rather than becoming a second thing to remember.
"""

from __future__ import annotations

from pathlib import Path
import re

from .contracts import ContractError
from .queries import run_attempts, run_summary
from .store import StoreError
from .workspaces import Repository, WorkspaceError

NAMESPACE = "refs/anvil/"
RETAINED = re.compile(r"refs/anvil/(candidate|integration)/([^/]+)/([^/]+)\Z")
#: Ledger fields that name a revision; a dangling one of these is the failure
#: docs/OBSERVED_LIMITS.md records.
RECORDED = ("candidate_sha", "integration_sha", "integrated_sha", "reviewed_sha")


def _refs(repo: Repository) -> list[tuple[str, str, str, str, str]]:
    listed = repo.git("for-each-ref", NAMESPACE, "--format=%(refname) %(objectname)")
    found = []
    for line in listed.splitlines():
        name, _, revision = line.partition(" ")
        match = RETAINED.match(name)
        if match:
            found.append((name, revision, *match.groups()))
    return found


def _ledger(state_dir: Path, run_id: str) -> dict:
    """What a run's saved ledger says about each of its attempts, if readable."""
    directory = Path(state_dir) / run_id
    if not (directory / "state.sqlite").is_file():
        return {}
    try:
        summary = run_summary(directory)
        attempts, cursor = {}, None
        while True:
            page = run_attempts(directory, after=cursor)
            for attempt in page["items"]:
                attempts[attempt["id"]] = attempt
            cursor = page["next_after"]
            if cursor is None:
                break
        return {"branch": summary["branch"], "attempts": attempts}
    except StoreError:
        return {}


def listing(repo: Path, state_dir: Path) -> dict:
    """Every retained ref, enriched from its run's ledger where one survives."""
    repository = Repository(repo)
    ledgers: dict[str, dict] = {}
    items = []
    for name, revision, kind, run_id, attempt_id in sorted(_refs(repository)):
        ledger = ledgers.setdefault(run_id, _ledger(state_dir, run_id))
        attempt = (ledger.get("attempts") or {}).get(attempt_id, {})
        items.append({
            "ref": name, "kind": kind, "run_id": run_id, "attempt_id": attempt_id,
            "revision": revision, "task_id": attempt.get("task_id"),
            "attempt_status": attempt.get("status"),
            "accepted": attempt.get("status") == "done",
            "run_dir": str(Path(state_dir) / run_id) if ledger else None,
        })
    return {"items": items, "runs": sorted({item["run_id"] for item in items})}


def delete(repo: Path, state_dir: Path, run_id: str) -> dict:
    """Remove one run's retained refs, refusing to strand a recorded revision.

    A ref whose revision is reachable from something else -- the managed branch,
    another run's ref -- is safe: the revision survives without it. One that is
    not, while the run's ledger still names that revision, is the only thing
    here worth protecting, because deleting it leaves the ledger pointing at a
    hash nothing can resolve.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ContractError("a run ID is required to delete retained refs")
    repository = Repository(repo)
    mine = [entry for entry in _refs(repository) if entry[3] == run_id]
    if not mine:
        raise ContractError(f"no retained refs for run {run_id}")
    ledger = _ledger(state_dir, run_id)
    recorded = {value for attempt in (ledger.get("attempts") or {}).values()
                for field in RECORDED if (value := attempt["details"].get(field))}
    going = {name for name, *_ in mine}

    removed, refused = [], []
    for name, revision, kind, _, attempt_id in sorted(mine):
        holders = {line.strip() for line in repository.git(
            "for-each-ref", "--contains", revision, "--format=%(refname)").splitlines()}
        if revision in recorded and not (holders - going):
            refused.append({"ref": name, "revision": revision, "attempt_id": attempt_id,
                            "reason": "the run's ledger still records this revision and "
                                      "nothing else reaches it; remove the run directory "
                                      "first if this evidence is no longer wanted"})
            continue
        try:
            repository.git("update-ref", "-d", name, revision)
        except WorkspaceError as exc:
            refused.append({"ref": name, "revision": revision, "attempt_id": attempt_id,
                            "reason": str(exc)})
            continue
        removed.append({"ref": name, "revision": revision, "kind": kind})
    return {"run_id": run_id, "removed": removed, "refused": refused,
            "removed_count": len(removed), "refused_count": len(refused)}
