"""Best-effort usage accounting kept separate from acceptance claims."""
from __future__ import annotations
import json
import math
from pathlib import Path
import time
from .evidence import concedes
from .ticket_status import read_regular, atomic, encoded


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def usage(path, agent, profile):
    result = {"input_tokens": None, "cached_input_tokens": None, "cache_write_tokens": None,
              "output_tokens": None, "cost_usd": None, "cost_kind": "unknown", "cost_basis": "unknown",
              "reported_model": None, "reported_effort": None, "usage_error": None, "provider_error": None}
    try:
        data = read_regular(path)
        events = [json.loads(line) for line in data.splitlines()]
        if any(not isinstance(e, dict) for e in events):
            raise ValueError("non-object telemetry event")
        if agent == "claude-code":
            terminal = [e for e in events if e.get("type") == "result"]
            if len(terminal) != 1:
                raise ValueError("usage requires exactly one result")
            event = terminal[0]
            if event.get("is_error") is True:
                result["provider_error"] = str(event.get("result") or event.get("errors") or "provider error")[:2000]
            u = event.get("usage", {})
            for field, key in (("input_tokens", "input_tokens"), ("cached_input_tokens", "cache_read_input_tokens"),
                               ("cache_write_tokens", "cache_creation_input_tokens"), ("output_tokens", "output_tokens")):
                result[field] = number(u.get(key))
            result["cost_usd"] = number(event.get("total_cost_usd"))
            if result["cost_usd"] is not None:
                result["cost_kind"] = "provider_reported_estimate"
            models = event.get("modelUsage", {})
            if len(models) == 1:
                model_name, model_usage = next(iter(models.items()))
                result["reported_model"] = model_name
                # An unrecognized costBasis, or a stream with none/multiple
                # models, cannot be attributed to a single reported basis and
                # stays "unknown" rather than guessing "billed".
                if isinstance(model_usage, dict) and model_usage.get("costBasis") in ("list", "billed"):
                    result["cost_basis"] = model_usage["costBasis"]
            for e in events:
                if e.get("type") == "system" and e.get("subtype") == "init":
                    result["reported_model"] = result["reported_model"] or e.get("model")
        else:
            # Each completed turn is incremental; do not sum message/item-level usage too.
            turns = [e for e in events if e.get("type") == "turn.completed"]
            if not turns:
                raise ValueError("no completed-turn usage")
            if any(not isinstance(e.get("usage"), dict) for e in turns):
                raise ValueError("missing turn usage")
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                values = [number(e["usage"].get(field)) for e in turns]
                result[field] = sum(values) if all(v is not None for v in values) else None
            result["cache_write_tokens"] = 0
            # Codex cached tokens are included in input_tokens; normalize to uncached input.
            if result["input_tokens"] is not None and result["cached_input_tokens"] is not None:
                result["input_tokens"] -= result["cached_input_tokens"]
                if result["input_tokens"] < 0:
                    raise ValueError("cached input exceeds total input")
        price = profile.get("price") if profile else None
        if result["cost_usd"] is None and price:
            fields = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
            if all(result[f] is not None for f in fields):
                result["cost_usd"] = sum(result[f]*price[k] for f,k in zip(fields,("input","cached_input","cache_write","output")))/1_000_000
                result["cost_kind"] = "configured_price_estimate"
                result["price_version"] = price["version"]
        if result["cost_usd"] is not None and number(result["cost_usd"]) is None:
            raise ValueError("nonfinite cost estimate")
        if not isinstance(result["reported_model"], str):
            result["reported_model"] = None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as exc:
        result["usage_error"] = str(exc)
        result["cost_usd"], result["cost_kind"], result["cost_basis"] = None, "unknown", "unknown"
    return result


class MeasuredRunner:
    def __init__(self, runner, agent, profile, version, decision):
        self.runner, self.agent, self.profile = runner, agent, profile
        self.version, self.decision = version, decision

    def run(self, **kwargs):
        started = time.time()
        outcome, error = "failed", None
        try:
            value = self.runner.run(**kwargs)
            outcome = "returned"
            return value
        except BaseException as exc:
            error = str(exc) or type(exc).__name__
            raise
        finally:
            directory = kwargs["artifact_dir"]
            record = {"agent": self.agent, "requested_model": self.profile["model"],
                      "requested_effort": self.profile["effort"], "cli_version": self.version,
                      "decision": self.decision, "role": "review" if kwargs.get("read_only") else "worker",
                      "started_at": started, "duration_seconds": time.time()-started,
                      "outcome": outcome, "error": error}
            record.update(usage(directory / "events.jsonl", self.agent, self.profile))
            # Telemetry must not mask the original error or change acceptance.
            try:
                if directory.is_dir() and not directory.is_symlink():
                    atomic(directory / "invocation.json", encoded(record))
            except (OSError, ValueError):
                pass


COST_KIND_NOTE = "estimated; not a subscription invoice"
EVALUATION_NOTE = "Success establishes observed sufficiency, not optimal model choice."


def attempt_invocations(run_dir, attempt_id, *, review_started=False):
    """Both role records for one attempt; absent accounting stays unknown, never free."""
    invocations = []
    for role in ("worker", "review"):
        path = Path(run_dir) / "artifacts" / attempt_id / role / "invocation.json"
        try:
            invocations.append(load_record(path, role))
        except (OSError, ValueError):
            started = role == "worker" or review_started
            invocations.append({"role": role, "cost_usd": None if started else 0,
                                "cost_kind": "unknown" if started else "not_started"})
    return invocations


def attempt_record(run_dir, attempt, *, decision=None, review_started=False):
    """One attempt's accounting and failure classification, from saved evidence only."""
    invocations = attempt_invocations(run_dir, attempt["id"], review_started=review_started)
    costs = [r["cost_usd"] for r in invocations]
    details = attempt["details"]
    review = details.get("review") or {}
    # A conceded rejection is not evidence about the worker or its profile: the
    # candidate satisfied every criterion the ticket stated. Scoring it as a
    # rejection would train routing on the ticket's incompleteness, so the
    # sample is dropped as insufficient evidence instead. The same is true of
    # turn_exhaustion/budget_exhaustion: the invocation hit a ceiling before
    # producing any candidate, so there is no outcome attributable to the
    # worker or reviewer.
    exhausted = details.get("failure_category") in ("turn_exhaustion", "budget_exhaustion")
    attributable_rejection = not exhausted and (
        "retry_reason" in details
        or (review.get("verdict") == "request_changes" and not concedes(review)
            and details.get("failure_category") != "undeclared_site")
        or any(check.get("returncode") not in (None, 0) or check.get("timed_out")
               for check in details.get("verification", [])))
    evaluation = "observed_sufficient" if attempt["status"] == "done" else (
        "rejected" if attempt["status"] in ("failed", "blocked") and attributable_rejection
        else "insufficient_evidence")
    # The coordinator records the structured retry cause when it retires the
    # attempt; prefer it over re-deriving from whatever evidence survived.
    stored_category = details.get("failure_category")
    if stored_category not in ("review_rejection", "verification_failure",
                               "retryable_rejection", "unlisted_requirement",
                               "undeclared_site", "turn_exhaustion", "budget_exhaustion",
                               "unresolvable_location"):
        stored_category = None
    return {"attempt_id": attempt["id"], "task_id": attempt["task_id"],
            "status": attempt["status"], "decision": decision,
            "failure_category": ("none" if attempt["status"] == "done" else
                "provider_error" if any(i.get("provider_error") for i in invocations) else
                stored_category if stored_category else
                "unlisted_requirement" if concedes(review) else
                "review_rejection" if review.get("verdict") == "request_changes" else
                "verification_failure" if details.get("verification") else
                "retryable_rejection" if "retry_reason" in details else "unknown"),
            "evaluation": evaluation, "invocations": invocations,
            "cost_usd": sum(costs) if all(c is not None for c in costs) else None,
            "duration_seconds": sum(r.get("duration_seconds", 0) for r in invocations)}


def rollup(records):
    """Totals that omit unknown cost rather than counting it as zero."""
    known = [i["cost_usd"] for r in records for i in r["invocations"] if i["cost_usd"] is not None]
    return {"known_cost_usd": sum(known),
            "cost_complete": all(r["cost_usd"] is not None for r in records),
            "cost_kind": COST_KIND_NOTE, "evaluation_note": EVALUATION_NOTE}


def run_telemetry(run_dir, *, after=None, limit=None):
    """A bounded page of per-attempt accounting joined to its routing decision."""
    from .queries import DEFAULT_PAGE_SIZE, attempt_messages, run_attempts

    page = run_attempts(run_dir, after=after,
                        limit=DEFAULT_PAGE_SIZE if limit is None else limit)
    identifiers = [attempt["id"] for attempt in page["items"]]
    decisions = attempt_messages(run_dir, "routing_decision", identifiers)
    reviewed = attempt_messages(run_dir, "review_started", identifiers)
    records = [attempt_record(run_dir, attempt, decision=decisions.get(attempt["id"]),
                              review_started=attempt["id"] in reviewed)
               for attempt in page["items"]]
    return {"items": records, "next_after": page["next_after"], **rollup(records)}


def load_record(path, role):
    """Treat absent or damaged accounting as unknown, never as free execution."""
    value = json.loads(read_regular(path))
    if not isinstance(value, dict) or value.get("role") != role or "cost_usd" not in value:
        raise ValueError("invalid invocation record")
    cost = value["cost_usd"]
    if cost is not None and number(cost) is None:
        raise ValueError("invalid invocation cost")
    if number(value.get("duration_seconds")) is None:
        raise ValueError("invalid invocation duration")
    return value
