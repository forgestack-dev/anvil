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
    "blockers": _STRINGS,
})
REVIEW_SCHEMA = _schema({
    "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
    "summary": _TEXT,
    "acceptance": {"type": "array", "items": _schema({
        "criterion": {"type": "integer", "minimum": 1},
        "satisfied": {"type": "boolean"}, "evidence": _TEXT})},
    "findings": _STRINGS,
})


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_result(result: dict, task: Task, *, review: bool = False) -> dict:
    flag, notes = ("verdict", "findings") if review else ("status", "blockers")
    _object_fields(result, {flag, "summary", "acceptance", notes}, set(), "agent result")
    choices = ("approve", "request_changes") if review else ("completed", "blocked")
    if result[flag] not in choices or not _text(result["summary"]):
        raise ContractError("agent result has an invalid outcome or summary")
    if not isinstance(result[notes], list) or any(not _text(item) for item in result[notes]):
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
    return result
