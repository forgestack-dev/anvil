"""Best-effort usage accounting kept separate from acceptance claims."""
from __future__ import annotations
import json
import math
from pathlib import Path
import time
from .ticket_status import read_regular, atomic, encoded


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def usage(path, agent, profile):
    result = {"input_tokens": None, "cached_input_tokens": None, "cache_write_tokens": None,
              "output_tokens": None, "cost_usd": None, "cost_kind": "unknown",
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
                result["reported_model"] = next(iter(models))
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
        result["cost_usd"], result["cost_kind"] = None, "unknown"
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
