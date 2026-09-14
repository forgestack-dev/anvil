"""Explicit bounded comparisons on low-risk fixture tickets, never automatic."""
from copy import deepcopy
from dataclasses import replace
from .contracts import ContractError
from .planning import TaskGraph
from .execution import run


def compare(config, names):
    if config.adaptive is None or config.workers:
        raise ContractError("benchmark requires a serial adaptive configuration")
    graph = TaskGraph.load(config.tickets)
    if any(t.risk != "low" or t.profile or t.worker for t in graph.tasks):
        raise ContractError("benchmark tickets must explicitly be low-risk, with no profile or worker override")
    if not 2 <= len(names) <= 4 or len(set(names)) != len(names):
        raise ContractError("select 2 to 4 distinct profiles")
    options = config.adaptive
    if any(n not in options["profiles"] or options["profiles"][n]["agent"] != config.agent for n in names):
        raise ContractError("benchmark profiles must match the serial worker agent")
    calls = len(graph.tasks)*2*len(names)
    if calls > options.get("max_invocations", 100):
        raise ContractError("benchmark exceeds aggregate invocation budget")
    review = options["profiles"][options["review_profile"]]
    reserve = sum((options["profiles"][n].get("reserve_usd",0)+review.get("reserve_usd",0))*len(graph.tasks) for n in names)
    limit = options.get("soft_budget_usd")
    if limit is not None and reserve > limit:
        raise ContractError("benchmark cannot reserve aggregate soft budget")
    results, consumed = [], 0.0
    for name in names:
        selected = deepcopy(options)
        selected.update(mode="off", max_attempts=1, defaults={config.agent:name})
        # Benchmark imports evidence but cannot change the policy under comparison.
        selected.pop("learning", None)
        if limit is not None:
            remaining = limit-consumed
            if remaining <= 0:
                break
            selected["soft_budget_usd"] = remaining
        result = run(replace(config, adaptive=selected, ticket_status=False))
        results.append({"profile":name, "run_dir":result["run_dir"], "status":result["status"], "routing":result.get("routing")})
        measured = result.get("routing",{})
        reserved = (options["profiles"][name].get("reserve_usd",0)+review.get("reserve_usd",0))*len(graph.tasks)
        consumed += measured.get("known_cost_usd",0) if measured.get("cost_complete") else max(reserved,measured.get("known_cost_usd",0))
        if result["status"] in ("interrupted", "failed"):
            break
    return {"comparisons":results,"known_or_reserved_cost_usd":consumed,
            "limitations":"Currency budget is estimated unless profiles enforce provider limits; all changes stay on separate local Anvil branches."}
