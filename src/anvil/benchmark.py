"""Explicit bounded comparisons on low-risk fixture tickets, never automatic."""
from copy import deepcopy
from dataclasses import replace
from .contracts import ContractError
from .planning import TaskGraph
from .execution import run


def _escalation_path(profiles, name, attempts):
    """Bounded escalation path for a profile: the selected profile followed by
    same-agent higher-rank profiles, one per possible retry. Mirrors
    Policy.escalation: retries climb the rank chain until it or the attempt
    limit runs out."""
    path, current = [name], name
    while len(path) < attempts:
        base = profiles[current]
        choices = sorted((k for k, v in profiles.items()
                          if v.get("agent") == base.get("agent")
                          and v.get("rank", 0) > base.get("rank", 0)),
                         key=lambda k: (profiles[k].get("rank", 0), k))
        if not choices:
            break
        current = choices[0]
        path.append(current)
    return path


def _path_reserve(profiles, review_reserve, name, attempts):
    """Total reservation along a profile's bounded escalation path: each
    reachable attempt reserves its own profile plus the reviewer. Profiles
    with no escalation headroom contribute fewer attempts."""
    path = _escalation_path(profiles, name, attempts)
    return sum(profiles[p].get("reserve_usd", 0) for p in path) + review_reserve * len(path)


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
    # Benchmark evidence must land in the same catalog as the production
    # configuration under comparison, so it inherits the retry limit instead
    # of forcing a single attempt.
    attempts = options.get("max_attempts", 1)
    calls = len(graph.tasks)*2*attempts*len(names)
    if calls > options.get("max_invocations", 100):
        raise ContractError("benchmark exceeds aggregate invocation budget")
    review = options["profiles"][options["review_profile"]]
    # Retries can escalate to a more expensive profile than the one under
    # comparison, so reserve the worst case per profile for every attempt;
    # otherwise the budget check can pass here and fail midway through.
    # The aggregate budget sums each profile's reachable escalation path, as
    # the consumption accounting does: a profile with no escalation headroom
    # cannot run the full attempt count.
    reserve = sum(_path_reserve(options["profiles"], review.get("reserve_usd",0), n, attempts)*len(graph.tasks) for n in names)
    limit = options.get("soft_budget_usd")
    if limit is not None and reserve > limit:
        raise ContractError("benchmark cannot reserve aggregate soft budget")
    results, consumed = [], 0.0
    for name in names:
        selected = deepcopy(options)
        selected.update(mode="off", max_attempts=attempts, defaults={config.agent:name})
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
        # Without complete telemetry, charge the actual bounded escalation
        # path: the first attempt reserves the selected profile and each
        # retry reserves the escalated profile, plus the reviewer every time.
        reserved = _path_reserve(options["profiles"], review.get("reserve_usd",0), name, attempts)*len(graph.tasks)
        consumed += measured.get("known_cost_usd",0) if measured.get("cost_complete") else max(reserved,measured.get("known_cost_usd",0))
        if result["status"] in ("interrupted", "failed"):
            break
    return {"comparisons":results,"known_or_reserved_cost_usd":consumed,
            "limitations":"Currency budget is estimated unless profiles enforce provider limits; all changes stay on separate local Anvil branches."}
