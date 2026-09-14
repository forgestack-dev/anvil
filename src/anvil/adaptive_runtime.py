"""Coordinator-owned routing, reservations, and immutable per-attempt runners."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import sqlite3
from .adapters import create_runner
from .contracts import ContractError
from .routing import Policy, preflight
from .telemetry import MeasuredRunner, load_record
from .ticket_status import read_regular


class Session:
    def __init__(self, config, graph, repo, *, injected=False):
        self.config, self.policy = config, Policy(config, graph, repo)
        self.count = 0
        self.committed_cost = 0.0
        self.reservations = {}
        self.decisions = {}
        self.attempt_counts = {}
        self.versions, self.executables = {}, {}
        entries = [(w.agent, w.executable) for w in config.workers] + [(config.agent, config.executable)]
        for agent, executable in entries:
            key = (agent, executable)
            if key in self.versions:
                continue
            resolved = executable if injected else shutil.which(executable)
            if resolved is None:
                raise ContractError(f"agent executable not found: {executable}")
            self.executables[key] = resolved
            from .processes import ProcessError
            try:
                budget_profile = next((p for p in self.policy.profiles.values()
                                       if p["agent"] == agent and "max_budget_usd" in p), None)
                self.versions[key] = "injected-test-runner" if injected else preflight(agent, resolved, budget_profile)
            except ProcessError as exc:
                raise ContractError(f"profile preflight failed: {exc}") from exc

        if self.policy.learned:
            expected = self.policy.learned.get("cli_versions", {})
            if any(expected.get(agent) != version for (agent, _), version in self.versions.items()
                   if agent in expected):
                self.policy.learned = None

    def reserve(self, attempt_id, profile):
        # Reserve work plus independent review together; retain unknown usage estimates.
        work = self.policy.profiles[profile]
        review = self.policy.profiles[self.policy.review]
        reserve = work.get("reserve_usd", 0) + review.get("reserve_usd", 0)
        if self.count + 2 > self.policy.config.get("max_invocations", 100):
            raise ContractError("invocation budget exhausted (worker plus reviewer required)")
        limit = self.policy.config.get("soft_budget_usd")
        if limit is not None and self.committed_cost + sum(self.reservations.values()) + reserve > limit:
            raise ContractError("soft cost budget cannot reserve worker and reviewer")
        self.count += 2
        self.reservations[attempt_id] = reserve

    def decide(self, task, worker, base, attempt_id, *, escalate=None):
        decision = self.policy.decision(task, worker, base)
        if escalate:
            decision["profile"], decision["reason"] = escalate, "bounded escalation after review/check failure"
        self.reserve(attempt_id, decision["profile"])
        self.decisions[attempt_id] = decision
        self.attempt_counts[task.id] = self.attempt_counts.get(task.id, 0) + 1
        decision["reservation"] = {"invocations":2, "estimated_usd":self.reservations[attempt_id],
                                   "cumulative_invocations":self.count}
        return decision

    def runner(self, item, *, review=False, injected=None):
        decision = self.decisions[item.attempt_id]
        name = self.policy.review if review else decision["profile"]
        profile = self.policy.profiles[name]
        agent = self.config.agent if review else item.worker.agent
        executable = self.config.executable if review else item.worker.executable
        key = (agent, executable)
        runner = injected if injected is not None else create_runner(agent, self.executables[key], profile=profile)
        return MeasuredRunner(runner, agent, profile, self.versions[key], decision | {"invocation_profile": name})

    def next_profile(self, item):
        if self.attempt_counts.get(item.task.id, 1) >= self.policy.config.get("max_attempts", 1):
            return None
        return self.policy.escalation(self.decisions[item.attempt_id]["profile"])

    def settle(self, item):
        reservation = self.reservations.pop(item.attempt_id, 0)
        total, complete = 0.0, True
        for role in ("worker", "review"):
            try:
                record = load_record(item.artifacts / role / "invocation.json", role)
                if record["cost_usd"] is None:
                    complete = False
                else:
                    total += record["cost_usd"]
            except (OSError, ValueError, KeyError):
                complete = False
        self.committed_cost += total if complete else max(total, reservation)


def report(result):
    if not result["config"].get("adaptive"):
        return
    decisions = {e["attempt_id"]: e["details"]["body"] for e in result["events"]
                 if e["details"].get("message_kind") == "routing_decision"}
    records = []
    for attempt in result["attempts"]:
        invocations = []
        for role in ("worker", "review"):
            path = Path(result["run_dir"]) / "artifacts" / attempt["id"] / role / "invocation.json"
            try:
                invocations.append(load_record(path, role))
            except (OSError, ValueError):
                started = role == "worker" or any(
                    e["attempt_id"] == attempt["id"] and e["details"].get("message_kind") == "review_started"
                    for e in result["events"])
                invocations.append({"role": role, "cost_usd": None if started else 0,
                                    "cost_kind": "unknown" if started else "not_started"})
        costs = [r["cost_usd"] for r in invocations]
        details = attempt["details"]
        attributable_rejection = (
            "retry_reason" in details
            or details.get("review", {}).get("verdict") == "request_changes"
            or any(check.get("returncode") not in (None, 0) or check.get("timed_out")
                   for check in details.get("verification", []))
        )
        evaluation = "observed_sufficient" if attempt["status"] == "done" else (
            "rejected" if attempt["status"] in ("failed", "blocked") and attributable_rejection
            else "insufficient_evidence")
        # The coordinator records the structured retry cause when it retires the
        # attempt; prefer it over re-deriving from whatever evidence survived.
        stored_category = details.get("failure_category")
        if stored_category not in ("review_rejection", "verification_failure", "retryable_rejection"):
            stored_category = None
        records.append({"attempt_id": attempt["id"], "task_id": attempt["task_id"],
                        "status": attempt["status"], "decision": decisions.get(attempt["id"]),
                        "failure_category": ("none" if attempt["status"] == "done" else
                            "provider_error" if any(i.get("provider_error") for i in invocations) else
                            stored_category if stored_category else
                            "review_rejection" if attempt["details"].get("review", {}).get("verdict") == "request_changes" else
                            "verification_failure" if attempt["details"].get("verification") else
                            "retryable_rejection" if "retry_reason" in attempt["details"] else "unknown"),
                        "evaluation": evaluation, "invocations": invocations,
                        "cost_usd": sum(costs) if all(c is not None for c in costs) else None,
                        "duration_seconds": sum(r.get("duration_seconds", 0) for r in invocations)})
    known = [i["cost_usd"] for r in records for i in r["invocations"] if i["cost_usd"] is not None]
    result["routing"] = {"attempts": records, "known_cost_usd": sum(known),
                         "cost_complete": all(r["cost_usd"] is not None for r in records),
                         "cost_kind": "estimated; not a subscription invoice",
                         "evaluation_note": "Success establishes observed sufficiency, not optimal model choice."}


def learn(repo, config, result):
    from .learning import import_run, train, promote, rollback
    from .routing import learning_catalog
    try:
        result["learning"] = import_run(repo, result)
        options = config.adaptive.get("learning")
        if options:
            gates = {k:v for k,v in options.items() if k != "auto_promote"}
            policy = train(repo, config, **gates)
            result["learning"]["candidate_policy"] = policy["id"]
            result["learning"]["validated"] = policy["validated"]
            if options.get("auto_promote"):
                if policy["validated"]:
                    result["learning"].update(promote(repo, policy["id"], learning_catalog(config)))
                else:
                    result["learning"].update(rollback(repo))
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        result["learning"] = {"error": str(exc), "status": "history_update_pending"}
