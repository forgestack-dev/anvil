"""Coordinator-owned routing, reservations, and immutable per-attempt runners."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import sqlite3
from .adapters import create_runner
from .contracts import ContractError
from .routing import Policy, preflight
from .telemetry import MeasuredRunner, attempt_record, load_record, rollup
from .ticket_status import read_regular


class Session:
    def __init__(self, config, graph, repo, *, injected=False, frozen=None):
        self.config, self.policy = config, Policy(config, graph, repo, frozen=frozen)
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
                self.versions[key] = ("injected-test-runner" if injected else
                                      preflight(agent, resolved, budget_profile,
                                                exclude=config.credential_exclusion))
            except ProcessError as exc:
                raise ContractError(f"profile preflight failed: {exc}") from exc

        if frozen is not None:
            expected = sorted(frozen["versions"])
            actual = sorted([agent, executable, version] for (agent, executable), version in self.versions.items())
            if expected != actual or frozen["policy_version"] != self.policy.version:
                raise ContractError("resume requires the original routing configuration and CLI versions")
        if self.policy.learned:
            expected = self.policy.learned.get("cli_versions", {})
            if any(expected.get(agent) != version for (agent, _), version in self.versions.items()
                   if agent in expected):
                self.policy.learned = None

    def freeze(self, run_dir, store):
        from .ticket_status import atomic, encoded
        from .routing import fingerprint
        from .store import _now
        value = {"policy_version": self.policy.version, "learned": self.policy.learned,
                 "versions": [[a, e, v] for (a, e), v in self.versions.items()]}
        atomic(Path(run_dir) / "routing-frozen.json", encoded(value))
        with store._transaction() as db:
            store._event(db, _now(), "routing_frozen", None, None, None, "running", {"digest": fingerprint(value)})

    def restore(self, run_dir, saved):
        from types import SimpleNamespace
        decisions = {e["attempt_id"]: e["details"]["body"] for e in saved["events"]
                     if e["details"].get("message_kind") == "routing_decision"}
        self.previous_profiles = {}
        for attempt in saved["attempts"]:
            aid = attempt["id"]
            decision = decisions.get(aid)
            if decision is None:
                # Claim may have committed before its decision event. Dispatch follows that event.
                continue
            self.count += 2
            self.reservations[aid] = decision["reservation"]["estimated_usd"]
            self.settle(SimpleNamespace(attempt_id=aid, artifacts=Path(run_dir) / "artifacts" / aid))
        # Event order, not wall-clock order, identifies the last selected profile.
        for event in saved["events"]:
            if event["details"].get("message_kind") == "routing_decision":
                self.previous_profiles[event["task_id"]] = event["details"]["body"]["profile"]
            if event["kind"] == "retry":
                tid = event["task_id"]
                self.attempt_counts[tid] = self.attempt_counts.get(tid, 0) + 1

    def record_reuse(self, item):
        from .ticket_status import atomic, encoded
        directory = item.artifacts / "worker"
        directory.mkdir(parents=True, exist_ok=False)
        atomic(directory / "invocation.json", encoded({
            "role": "worker", "cost_usd": 0, "cost_kind": "not_started",
            "duration_seconds": 0, "recovered_candidate": item.candidate}))

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
        # agent_turns is a run-level setting, not part of a routing profile: an
        # adaptive run must honor it exactly as a non-adaptive one does.
        runner = injected if injected is not None else create_runner(
            agent, self.executables[key], profile=profile, turns=self.config.agent_turns,
            exclude=self.config.credential_exclusion)
        return MeasuredRunner(runner, agent, profile, self.versions[key], decision | {"invocation_profile": name})

    def next_profile(self, item):
        if self.attempt_counts.get(item.task.id, 1) >= self.policy.config.get("max_attempts", 1):
            return None
        return self.policy.escalation(self.decisions[item.attempt_id]["profile"])

    def settle(self, item, *, reviewed=True):
        """Commit an attempt's actual cost, releasing its reservation.

        Under checks-first a rejected candidate may never reach review, so
        charging that attempt the reserved reviewer cost would consume a run's
        soft budget with invocations that never happened. The caller says
        whether the review was dispatched; it never guesses from the artifacts,
        where an absent record also means damaged accounting.
        """
        reservation = self.reservations.pop(item.attempt_id, 0)
        total, complete = 0.0, True
        for role in ("worker", "review") if reviewed else ("worker",):
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
    reviewed = {e["attempt_id"] for e in result["events"]
                if e["details"].get("message_kind") == "review_started"}
    records = [attempt_record(result["run_dir"], attempt,
                              decision=decisions.get(attempt["id"]),
                              review_started=attempt["id"] in reviewed)
               for attempt in result["attempts"]]
    result["routing"] = {"attempts": records, **rollup(records)}


def learn(repo, config, result):
    from .learning import import_run, train, promote, rollback
    from .routing import learning_catalog
    try:
        result["learning"] = import_run(repo, result)
        if result["learning"].get("status") == "excluded_recovery_evidence":
            return
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
