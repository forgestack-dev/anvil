"""Explicit execution profiles and deterministic, frozen routing decisions."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
import re
from pathlib import Path
from .contracts import ContractError, _identifier, _object_fields, _text

EFFORTS = {"codex": ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
           "claude-code": ("low", "medium", "high", "xhigh", "max", "ultracode"),
           # Muse has no effort levels: any profile selection for Muse is
           # rejected by validate_selection, and adaptive profiles naming
           # Muse are rejected by validate_config, exactly as before.
           "muse": ()}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def learning_catalog(config):
    """Bind comparable evidence to the execution and acceptance conditions."""
    value = config if isinstance(config, dict) else config.to_dict()
    adaptive = value["adaptive"]
    return fingerprint({"evidence_version": 3, "profiles": adaptive["profiles"],
                        "max_attempts": adaptive.get("max_attempts", 1),
                        "review_profile": adaptive["review_profile"],
                        "verification": value["verification"],
                        "agent_timeout": value.get("agent_timeout"),
                        "check_timeout": value.get("check_timeout")})


def validate_selection(agent, profile):
    if not isinstance(profile, dict) or profile.get("effort") not in EFFORTS[agent]:
        raise ContractError("unsupported effort for selected agent")
    model = profile.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise ContractError("model must be an explicit model identifier")
    if agent == "claude-code" and (model == "haiku" or "haiku-4-5" in model):
        raise ContractError("Haiku 4.5 does not support effort profiles")


def validate_config(value):
    _object_fields(value, {"profiles", "defaults", "review_profile"},
                   {"mode", "max_attempts", "max_invocations", "policy", "soft_budget_usd", "learning"}, "adaptive")
    if "learning" in value:
        learning = value["learning"]
        _object_fields(learning, {"min_samples", "max_quality_loss", "min_cost_improvement", "max_latency_ratio"},
                       {"auto_promote"}, "learning")
        if type(learning["min_samples"]) is not int or learning["min_samples"] < 5:
            raise ContractError("learning.min_samples must be at least 5")
        for key in ("max_quality_loss", "min_cost_improvement"):
            positive(learning[key], key, zero=True)
            if learning[key] >= 1:
                raise ContractError(f"learning.{key} must be below 1")
        positive(learning["max_latency_ratio"], "max_latency_ratio")
        if type(learning.get("auto_promote", False)) is not bool:
            raise ContractError("auto_promote must be boolean")
    profiles, defaults = value["profiles"], value["defaults"]
    if not isinstance(profiles, dict) or not profiles or len(profiles) > 32:
        raise ContractError("profiles must contain 1 to 32 named profiles")
    for name, profile in profiles.items():
        _identifier(name, "profile name")
        _object_fields(profile, {"agent", "model", "effort", "rank"},
                       {"price", "reserve_usd", "max_budget_usd"}, f"profile {name}")
        agent = profile["agent"]
        if not isinstance(agent, str) or agent not in EFFORTS or profile["effort"] not in EFFORTS[agent]:
            raise ContractError(f"unsupported agent/effort in profile {name}")
        model = _text(profile["model"], "profile.model")
        if agent == "claude-code" and (model == "haiku" or "haiku-4-5" in model):
            raise ContractError("Haiku 4.5 does not support effort; choose an effort-capable profile model")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
            raise ContractError("model must be an explicit model identifier")
        if type(profile["rank"]) is not int or not 0 <= profile["rank"] <= 2:
            raise ContractError("profile.rank must be 0, 1, or 2")
        if "max_budget_usd" in profile:
            positive(profile["max_budget_usd"], "max_budget_usd")
            if agent != "claude-code":
                raise ContractError("max_budget_usd is only supported by Claude Code")
        if "reserve_usd" in profile:
            positive(profile["reserve_usd"], "reserve_usd")
        if "price" in profile:
            price = profile["price"]
            _object_fields(price, {"version", "input", "cached_input", "cache_write", "output"}, set(), "price")
            _text(price["version"], "price.version")
            for key in ("input", "cached_input", "cache_write", "output"):
                positive(price[key], f"price.{key}", zero=True)
    if not isinstance(defaults, dict) or not defaults:
        raise ContractError("defaults must map worker IDs (or agent names) to profiles")
    for key, name in defaults.items():
        _identifier(key, "default key")
        _identifier(name, "default profile")
        if name not in profiles:
            raise ContractError("default names an unknown profile")
    _identifier(value["review_profile"], "review_profile")
    if value["review_profile"] not in profiles:
        raise ContractError("unknown review_profile")
    if value.get("mode", "off") not in ("off", "shadow", "rules", "adaptive"):
        raise ContractError("invalid routing mode")
    for key, default, maximum in (("max_attempts", 1, 2), ("max_invocations", 100, 10000)):
        n = value.get(key, default)
        if type(n) is not int or not 1 <= n <= maximum:
            raise ContractError(f"{key} must be between 1 and {maximum}")
    if "policy" in value:
        _identifier(value["policy"], "policy")
    if "soft_budget_usd" in value:
        positive(value["soft_budget_usd"], "soft_budget_usd")
        if any("reserve_usd" not in p for p in profiles.values()):
            raise ContractError("a soft budget requires reserve_usd on every profile")


def positive(value, label, zero=False):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ContractError(f"{label} must be a finite {'nonnegative' if zero else 'positive'} number")


def assess(task, repo, base):
    text = " ".join((task.title, task.objective, *task.acceptance_criteria)).lower()
    risk_words = [word for word in ("migration", "authentication", "authorization", "cryptograph", "payment", "concurren", "data loss") if word in text]
    uncertainty = [word for word in ("tbd", "unknown", "investigate", "decide", "unspecified") if word in text]
    files = repo.git("ls-tree", "-r", "--name-only", base).splitlines()[:2000]
    mentioned = [name for name in files if name.lower() in text][:50]
    tests = [name for name in files if "test" in name.lower()][:20]
    rank = 2 if risk_words or uncertainty or task.risk == "high" else 1
    if task.risk == "low" and not risk_words and not uncertainty and mentioned and tests and len(task.depends_on) <= 1:
        rank = 0
    if len(task.depends_on) > 3 or len(mentioned) > 5:
        rank = 2
    return {"feature_version": 1, "rank": rank, "cohort": f"rank-{rank}",
            "confidence": "medium" if mentioned and tests else "low",
            "base_sha": base, "input_digest": fingerprint({k:v for k,v in task.to_dict().items() if k != "execution"}),
            "risk_signals": risk_words, "uncertainty_signals": uncertainty,
            "mentioned_files": mentioned, "test_files": tests, "files_examined": len(files),
            "limitations": "Bounded path inventory and ticket text; no code-semantic or model assessment."}


class Policy:
    def __init__(self, config, graph, repo, *, frozen=None):
        self.config = deepcopy(config.adaptive)
        self.repo = repo
        self.profiles = self.config["profiles"]
        self.version = fingerprint(self.config)
        self.learned = None
        if frozen is not None:
            self.learned = frozen["learned"]
        elif self.config.get("mode") == "adaptive":
            from .learning import load_policy
            self.learned = load_policy(repo, self.config.get("policy"), learning_catalog(config))
        self.workers = config.workers
        self.review = self.config["review_profile"]
        if self.profiles[self.review]["agent"] != config.agent:
            raise ContractError("review profile must match the reviewer agent")
        for worker in config.workers:
            self.default(worker)
        for task in graph.tasks:
            if task.profile and task.profile not in self.profiles:
                raise ContractError(f"unknown ticket profile: {task.profile}")
            if not any(self.compatible(task, w) for w in config.workers):
                raise ContractError(f"no compatible worker for {task.id}")

    def default(self, worker):
        name = self.config["defaults"].get(worker.id, self.config["defaults"].get(worker.agent))
        if name not in self.profiles or self.profiles[name]["agent"] != worker.agent:
            raise ContractError(f"missing compatible default profile for worker {worker.id}")
        return name

    def compatible(self, task, worker):
        return (task.worker is None or task.worker == worker.id) and (
            task.profile is None or self.profiles[task.profile]["agent"] == worker.agent)

    def decision(self, task, worker, base):
        assessment = assess(task, self.repo, base)
        default = self.default(worker)
        compatible = {k:p for k,p in self.profiles.items() if p["agent"] == worker.agent}
        rank = assessment["rank"]
        # Low confidence never routes below the configured baseline.
        if assessment["confidence"] == "low":
            rank = max(rank, self.profiles[default]["rank"])
        eligible = sorted(compatible, key=lambda k:(compatible[k]["rank"], k))
        recommended = next((k for k in eligible if compatible[k]["rank"] >= rank), eligible[-1])
        reason = "deterministic complexity/risk rules"
        if self.learned and assessment["confidence"] != "low" and rank < 2:
            choice = self.learned["routes"].get(worker.agent + ":" + assessment["cohort"])
            if choice in compatible:
                recommended, reason = choice, "validated repository policy"
        chosen = default if self.config.get("mode", "off") in ("off", "shadow") else recommended
        if task.profile:
            chosen, reason = task.profile, "explicit ticket override"
        return {"profile": chosen, "recommended_profile": recommended, "reason": reason,
                "policy_version": self.version, "learned_policy": self.learned["id"] if self.learned else None,
                "assessment": assessment}

    def escalation(self, name):
        p = self.profiles[name]
        choices = sorted((k for k,v in self.profiles.items() if v["agent"] == p["agent"] and v["rank"] > p["rank"]),
                         key=lambda k:(self.profiles[k]["rank"], k))
        return choices[0] if choices else None


def resolve_cost_basis(environment):
    """Whether the invocation launched with this environment will be billed per token.

    Claude Code enforces `--max-budget-usd` against whatever it reports as
    `total_cost_usd` regardless of how the session authenticates. Under a
    subscription login (no `ANTHROPIC_API_KEY`) that figure is a list-price
    estimate of tokens the plan already covers, not a charge; AGENTS.md is
    explicit that such an estimate is not a hard budget cap. Only a session
    launched with an API key is billed per token, and the key must already be
    present in the exact environment the invocation runs under, since the
    ceiling flag has to be decided before that invocation starts.
    An absent or empty key resolves to "list", never "billed": a missing
    signal must never enforce a ceiling against an estimate.
    """
    if environment.get("ANTHROPIC_API_KEY"):
        return "billed", "ANTHROPIC_API_KEY is set for this invocation; Claude Code bills it per token"
    return "list", ("no ANTHROPIC_API_KEY in this invocation's environment; Claude Code reports a "
                    "subscription list-price estimate under this basis, not a billed charge")


def preflight(agent, executable, profile, *, exclude=()):
    """Probe advertised controls; no model invocation or entitlement claim."""
    import tempfile
    from .processes import run_process
    from .environment import managed_environment
    with tempfile.TemporaryDirectory(prefix="anvil-profile-") as tmp:
        stdout, stderr = Path(tmp)/"out", Path(tmp)/"err"
        args = [executable] + (["exec"] if agent == "codex" else []) + ["--help"]
        result = run_process(args, cwd=Path.cwd(), stdin=None, stdout_path=stdout,
                             stderr_path=stderr, timeout=5, env=managed_environment(exclude))
        flags = stdout.read_text(errors="replace")
        needed = ("--model", "--config") if agent == "codex" else ("--model", "--effort")
        if profile and "max_budget_usd" in profile:
            needed = (*needed, "--max-budget-usd")
        if result.returncode or result.timed_out or not all(flag in flags for flag in needed):
            raise ContractError(f"{agent} does not advertise required profile controls: {needed}")
        stdout, stderr = Path(tmp)/"version-out", Path(tmp)/"version-err"
        result = run_process([executable, "--version"], cwd=Path.cwd(), stdin=None,
                             stdout_path=stdout, stderr_path=stderr, timeout=5,
                             env=managed_environment(exclude))
        if result.returncode or result.timed_out:
            raise ContractError(f"cannot probe {agent} version")
        return stdout.read_text(errors="replace").strip()[:500]
