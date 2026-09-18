"""Structured agent claims, separately checked before integration."""

from .contracts import ContractError, Task, _object_fields


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_TEXT = {"type": "string", "minLength": 1}
_STRINGS = {"type": "array", "items": _TEXT}
WORKER_SCHEMA = _schema({
    "status": {"type": "string", "enum": ["completed", "blocked"]},
    "summary": _TEXT,
    "acceptance": {"type": "array", "items": _schema({
        "criterion": {"type": "integer", "minimum": 1}, "evidence": _TEXT})},
    "blockers": {**_STRINGS, "description": "Unresolved blockers. Must be [] when status is completed; otherwise explain what is needed."},
})
REVIEW_SCHEMA = _schema({
    "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
    "summary": _TEXT,
    "acceptance": {"type": "array", "items": _schema({
        "criterion": {"type": "integer", "minimum": 1},
        "satisfied": {"type": "boolean"}, "evidence": _TEXT})},
    "findings": {"type": "array", "items": _schema({
        "criterion": {"type": "integer", "minimum": 1},
        "finding": _TEXT,
        "location": {**_TEXT, "description": "A path in the reviewed revision, optionally path:line. Name the directory when the change is an addition that has no line yet."},
    }), "description": "Actionable changes only, each naming the criterion it fails and where. Must be [] when verdict is approve; do not include no-findings statements or optional style notes."},
})


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _location(value: str) -> tuple[str, int | None]:
    """Split a finding's location into a repository path and optional line.

    Shape only. The supervisor resolves the path against the reviewed revision,
    because only it knows which revision was reviewed.
    """
    if not _text(value) or "\0" in value:
        raise ContractError("a finding location must be nonempty text without NUL")
    path, _, line = value.rpartition(":")
    if not path or not line.isdigit():
        path, number = value, None
    else:
        number = int(line)
        if number < 1:
            raise ContractError(f"a finding location line must be positive: {value}")
    if (path.startswith("/") or path.startswith("../") or path == ".."
            or "/../" in path or path.endswith("/..")):
        raise ContractError(f"a finding location must be a relative path inside the repository: {value}")
    return path.rstrip("/") or path, number


def concedes(review: dict) -> bool:
    """Whether a rejection grants every criterion of the ticket it reviewed.

    The reviewer's own map is the signal: it asserted, against its own verdict,
    that nothing the ticket asked for is missing. What it objects to is then a
    requirement the ticket does not carry, which no retry of this ticket can
    satisfy. Stage 2 obliges a review to fill the map, so this is a positive
    assertion rather than the vacuous truth an empty map would give.
    """
    return (review.get("verdict") == "request_changes"
            and bool(review.get("acceptance"))
            and all(item["satisfied"] for item in review["acceptance"]))


def rejection_reason(review: dict) -> str:
    """The rejection as the ticket's author needs to read it."""
    findings = format_findings(review["findings"])
    if not concedes(review):
        return findings
    return ("every acceptance criterion is satisfied, so this names a requirement "
            f"the ticket does not carry: {findings}")


def format_findings(findings: list[dict]) -> str:
    """One line per finding, naming the criterion and the place it points at."""
    return "; ".join(f"criterion {item['criterion']} ({item['location']}): {item['finding']}"
                     for item in findings)


def validate_result(result: dict, task: Task, *, review: bool = False) -> dict:
    flag, notes = ("verdict", "findings") if review else ("status", "blockers")
    _object_fields(result, {flag, "summary", "acceptance", notes}, set(), "agent result")
    choices = ("approve", "request_changes") if review else ("completed", "blocked")
    if result[flag] not in choices or not _text(result["summary"]):
        raise ContractError("agent result has an invalid outcome or summary")
    if not isinstance(result[notes], list):
        raise ContractError(f"agent result.{notes} must be an array")
    # Blockers stay free text; findings are objects, checked against the ticket below.
    if not review and any(not _text(item) for item in result[notes]):
        raise ContractError(f"agent result.{notes} must be a text array")
    acceptance = result["acceptance"]
    if not isinstance(acceptance, list):
        raise ContractError("agent result.acceptance must be an array")
    identifiers = set()
    for item in acceptance:
        required = {"criterion", "evidence"} | ({"satisfied"} if review else set())
        _object_fields(item, required, set(), "acceptance evidence")
        number = item["criterion"]
        if (type(number) is not int or not 1 <= number <= len(task.acceptance_criteria)
                or number in identifiers or not _text(item["evidence"])):
            raise ContractError("acceptance evidence must identify unique criteria with nonempty evidence")
        if review and type(item["satisfied"]) is not bool:
            raise ContractError("review satisfaction must be boolean")
        identifiers.add(number)
    success = result[flag] == choices[0]
    if success:
        if identifiers != set(range(1, len(task.acceptance_criteria) + 1)):
            raise ContractError("agent success omitted acceptance criteria")
        if result[notes] or (review and any(not item["satisfied"] for item in acceptance)):
            raise ContractError("agent success contradicts unresolved findings or criteria")
    elif not result[notes]:
        raise ContractError("a blocked or rejected result must explain what requires attention")
    if review:
        # A rejection must assess every criterion, not only the ones it objects
        # to. Without the full map a downstream consumer cannot tell "criterion
        # 3 is unmet" from "I assessed nothing", and a later stage keyed on the
        # map would read an empty one as vacuously satisfied.
        if identifiers != set(range(1, len(task.acceptance_criteria) + 1)):
            raise ContractError("a review must assess every acceptance criterion")
        for item in result[notes]:
            # An adapter returning the old bare-string shape must get a contract
            # error the operator can read, not a TypeError from indexing a string.
            _object_fields(item, {"criterion", "finding", "location"}, set(), "review finding")
            if not _text(item["finding"]):
                raise ContractError("a review finding must explain what requires attention")
            number = item["criterion"]
            if (type(number) is not int or type(number) is bool
                    or not 1 <= number <= len(task.acceptance_criteria)):
                raise ContractError("each finding must name a criterion of this ticket")
            _location(item["location"])
        # A finding may name a criterion the same review marks satisfied. That
        # is not a contradiction to reject here: it is exactly the conceded
        # rejection a later stage hands back to the ticket's author, and
        # refusing it would turn that case into a contract error instead.
    return result
