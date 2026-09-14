"""Local, immutable policies trained on disjoint input cohorts with explicit gates."""
from __future__ import annotations
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
from .contracts import ContractError, _identifier
from .routing import fingerprint
from .ticket_status import atomic, encoded, read_regular, destination_lock


def root(repo):
    path = repo.common_dir / "anvil-routing"
    if path.is_symlink():
        raise ContractError("routing history directory cannot be a symlink")
    path.mkdir(exist_ok=True)
    return path


@contextmanager
def history(repo):
    path = root(repo) / "history.sqlite"
    if path.is_symlink():
        raise ContractError("routing history cannot be a symlink")
    with destination_lock(path):
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS samples (run_id TEXT, task_id TEXT, data TEXT NOT NULL, PRIMARY KEY(run_id, task_id))")
            db.execute("PRAGMA user_version = 1")
            yield db
            db.commit()
        finally:
            db.close()


def import_run(repo, result):
    if Path(result["repo"]).resolve() != repo.path:
        raise ContractError("run belongs to another repository")
    if result["status"] in ("running", "created"):
        raise ContractError("only finalized runs can enter history")
    if "routing" not in result:
        raise ContractError("run has no routing telemetry")
    catalog = fingerprint(result["config"]["adaptive"]["profiles"])
    samples = []
    for task in result["tasks"]:
        attempts = [a for a in result["routing"]["attempts"] if a["task_id"] == task["id"]]
        if not attempts or not attempts[0]["decision"]:
            continue
        first = attempts[0]
        costs = [a["cost_usd"] for a in attempts]
        worker = first["invocations"][0]
        sample = {"catalog": catalog, "profile": first["decision"]["profile"],
                  "assessment": first["decision"]["assessment"], "agent": worker.get("agent"),
                  "cli_version": worker.get("cli_version"),
                  "accepted": task["status"] == "done", "attempt_count": len(attempts),
                  "cost": sum(costs) if all(c is not None for c in costs) else None,
                  "duration": sum(a["duration_seconds"] for a in attempts),
                  "eligible": all(a["evaluation"] != "insufficient_evidence" for a in attempts),
                  "reported_model": worker.get("reported_model"),
                  "requested_model": worker.get("requested_model"),
                  "effort": worker.get("requested_effort")}
        samples.append((result["run_id"], task["id"], json.dumps(sample, sort_keys=True)))
    with history(repo) as db:
        for run_id, task_id, data in samples:
            old = db.execute("SELECT data FROM samples WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
            if old and old[0] != data:
                raise ContractError("imported run evidence changed; refusing to rewrite history")
            db.execute("INSERT OR IGNORE INTO samples VALUES (?, ?, ?)", (run_id, task_id, data))
    return {"imported_or_existing": len(samples)}


def interval(rows):
    n = len(rows)
    p = sum(r["accepted"] for r in rows) / n
    z = 1.96
    center = (p + z*z/(2*n))/(1+z*z/n)
    margin = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return center-margin, center+margin


def metrics(rows):
    accepted = sum(r["accepted"] for r in rows)
    return {"n": len(rows), "acceptance_rate": accepted / len(rows),
            "cost_per_accepted": sum(r["cost"] for r in rows)/accepted if accepted else None,
            "mean_duration": sum(r["duration"] for r in rows)/len(rows)}


def train(repo, config, *, min_samples, max_quality_loss, min_cost_improvement, max_latency_ratio):
    if type(min_samples) is not int or min_samples < 5:
        raise ContractError("min_samples must be at least 5 per profile in each split")
    for name, value in (("max_quality_loss", max_quality_loss), ("min_cost_improvement", min_cost_improvement)):
        if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value < 1:
            raise ContractError(f"{name} must be a finite fraction in [0,1)")
    if type(max_latency_ratio) not in (int,float) or not math.isfinite(max_latency_ratio) or max_latency_ratio <= 0:
        raise ContractError("max_latency_ratio must be finite and positive")
    if config.adaptive is None or config.repo.resolve() != repo.path:
        raise ContractError("training requires an adaptive configuration for this repository")
    profiles = config.adaptive["profiles"]
    catalog = fingerprint(profiles)
    with history(repo) as db:
        all_rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM samples ORDER BY run_id, task_id")]
    rows = [r for r in all_rows if r["catalog"] == catalog and r["eligible"] and r["cost"] is not None
            and r["requested_model"] and r["reported_model"] in (None, r["requested_model"])
            and r["cli_version"] and r["cli_version"] != "injected-test-runner"]
    # Re-running one ticket cannot manufacture independent evidence.
    unique = {}
    for row in rows:
        key = (row["agent"], row["profile"], row["cli_version"], row["assessment"]["input_digest"])
        unique.setdefault(key, row)
    rows = list(unique.values())
    routes, evidence, versions = {}, [], {}
    defaults = config.adaptive["defaults"]
    for agent in ("codex", "claude-code"):
        baselines = {name for name in defaults.values() if profiles[name]["agent"] == agent}
        if len(baselines) != 1:
            continue  # Ambiguous baselines cannot justify automatic changes.
        baseline = next(iter(baselines))
        for cohort in ("rank-0", "rank-1"):
            subset = [r for r in rows if r["agent"] == agent and r["assessment"]["cohort"] == cohort]
            # Same input always belongs to the same split, regardless of model or run.
            splits = [[r for r in subset if int(r["assessment"]["input_digest"][:8],16)%2 == split] for split in (0,1)]
            def compare(split, name):
                reference = [r for r in split if r["profile"] == baseline]
                candidate = [r for r in split if r["profile"] == name]
                if len(reference) < min_samples or len(candidate) < min_samples:
                    return None
                if len({r["cli_version"] for r in reference + candidate}) != 1:
                    return None
                a, b = metrics(reference), metrics(candidate)
                if a["cost_per_accepted"] is None or b["cost_per_accepted"] is None:
                    return None
                if interval(candidate)[0] - interval(reference)[1] < -max_quality_loss:
                    return None
                if b["cost_per_accepted"] > a["cost_per_accepted"] * (1-min_cost_improvement):
                    return None
                if b["mean_duration"] > a["mean_duration"] * max_latency_ratio:
                    return None
                return {"baseline": a, "candidate": b, "cli_version": candidate[0]["cli_version"]}

            viable = []
            for name, profile in profiles.items():
                if name == baseline or profile["agent"] != agent:
                    continue
                stats = compare(splits[0], name)
                if stats:
                    viable.append((stats["candidate"]["cost_per_accepted"], name, stats))
            if viable:
                # Select solely on training data. The held-out split can reject
                # this choice, but cannot be searched for another winning model.
                _, name, training = min(viable)
                held_out = compare(splits[1], name)
                if held_out is None or held_out["cli_version"] != training["cli_version"]:
                    continue
                if agent in versions and versions[agent] != training["cli_version"]:
                    continue
                versions[agent] = training["cli_version"]
                routes[agent+":"+cohort] = name
                evidence.append({"agent":agent, "cohort":cohort, "baseline":baseline, "candidate":name,
                                 "training":training, "held_out":held_out})
    policy = {"version":1, "catalog":catalog, "routes":routes, "evidence":evidence, "cli_versions":versions,
              "history_digest":fingerprint(all_rows), "gates":{"min_samples":min_samples,
              "max_quality_loss":max_quality_loss, "min_cost_improvement":min_cost_improvement,
              "max_latency_ratio":max_latency_ratio}, "validated":bool(routes),
              "limitations":"Observational comparisons retain selection bias. Missing model reports use explicit requested profiles, not confirmed model identity. No causal or optimality claim."}
    policy["id"] = fingerprint(policy)
    path = root(repo) / (policy["id"] + ".json")
    if path.exists():
        if read_regular(path) != encoded(policy):
            raise ContractError("immutable policy conflict")
    else:
        atomic(path, encoded(policy))
    return policy


def load_policy(repo, policy_id, catalog):
    if policy_id is None:
        active = root(repo)/"active.json"
        if not active.exists():
            return None
        policy_id = json.loads(read_regular(active))["id"]
    _identifier(policy_id, "policy ID")
    policy = json.loads(read_regular(root(repo)/(policy_id+".json")))
    body = {k:v for k,v in policy.items() if k != "id"}
    if fingerprint(body) != policy_id or not policy["validated"] or policy["catalog"] != catalog:
        raise ContractError("policy is unvalidated, changed, or incompatible with the profile catalog")
    return policy


def promote(repo, policy_id, catalog):
    policy = load_policy(repo, policy_id, catalog)
    if policy is None:
        raise ContractError("no validated policy to promote")
    with destination_lock(root(repo)/"active.json"):
        atomic(root(repo)/"active.json", encoded({"id":policy["id"]}))
    return {"active_policy":policy["id"]}


def rollback(repo):
    path=root(repo)/"active.json"
    with destination_lock(path):
        if path.exists():
            path.unlink()
    return {"active_policy":None, "mode":"baseline rules"}
