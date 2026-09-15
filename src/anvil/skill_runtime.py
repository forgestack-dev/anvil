"""Pin installed AI Hero instructions and supply them to ticket workers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .contracts import ContractError, Task
from .skill_management import SkillError, SkillScope, managed_snapshot
from .skill_source import SOURCE
from .store import _now
from .ticket_status import atomic, encoded


MAX_CONTEXT_BYTES = 512 * 1024
_PIN_VERSION = 2
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITIES = {"shell", "network", "subagents", "human_dialogue", "issue_tracker",
                 "conversation_history", "git_control", "review_role"}


def _registry() -> dict:
    path = Path(__file__).with_name("skill_compat.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (set(value) != {"version", "source", "entries"} or value["version"] != 1
                or value["source"] != SOURCE or not isinstance(value["entries"], dict)):
            raise ContractError("unsupported skill compatibility registry")
        for name, entry in value["entries"].items():
            if (_NAME.fullmatch(name) is None or set(entry) != {"sha256", "requires", "adaptations"}
                    or not isinstance(entry["sha256"], str)
                    or _SHA256.fullmatch(entry["sha256"]) is None
                    or not isinstance(entry["requires"], list)
                    or any(item not in _CAPABILITIES for item in entry["requires"])
                    or len(set(entry["requires"])) != len(entry["requires"])
                    or not isinstance(entry["adaptations"], list)
                    or any(not isinstance(item, str) or not item.strip() for item in entry["adaptations"])):
                raise ContractError("invalid skill compatibility registry entry")
        return value
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"cannot load skill compatibility registry: {exc}") from exc


def _capabilities(agent: str) -> set[str]:
    if agent not in {"codex", "claude-code", "muse"}:
        raise ContractError(f"unknown skill execution agent: {agent}")
    return {"shell"} if agent == "codex" else set()


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ContractError("pinned skill contains an invalid file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ContractError("pinned skill contains an unsafe file path")
    return value


def _read_file(path: Path, expected: dict) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ContractError(f"skill resource must be a regular file: {path}")
            data = stream.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ContractError(f"skill resource exceeds 4 MiB: {path}")
        actual = {"sha256": hashlib.sha256(data).hexdigest(),
                  "executable": bool(info.st_mode & 0o111)}
        if actual != expected:
            raise ContractError(f"installed skill changed or is incomplete: {path}")
        return data
    except OSError as exc:
        raise ContractError(f"cannot read installed skill resource {path}: {exc}") from exc


_RULES = (
    ("prototype", (r"\bprototype\b", r"\bspike\b", r"proof[ -]of[ -]concept"),
     "ticket requests exploratory or prototype work"),
    ("tdd", (r"\btest(?:s|ing)?\b", r"\bcoverage\b", r"\btdd\b"),
     "ticket emphasizes tests or regression coverage"),
    ("diagnosing-bugs", (r"\bbug\b", r"\bdebug\b", r"\bregression\b", r"\bcrash\b",
                         r"\bfix(?:es|ed|ing)?\b", r"\bfail(?:ure|ing|s|ed)?\b",
                         r"\berror\b", r"\bincorrect\b"),
     "ticket describes a defect or regression"),
    ("domain-modeling", (r"\bdomain\b", r"\bentity\b", r"\bentities\b", r"\bschema\b",
                         r"\binvariant\b", r"\bvalue object\b"),
     "ticket concerns domain concepts or invariants"),
    ("codebase-design", (r"\barchitecture\b", r"\barchitectural\b", r"\bdesign\b",
                         r"\brefactor\b", r"\bmodule\b", r"\bboundar(?:y|ies)\b"),
     "ticket concerns code structure or architecture"),
    ("writing-for-agents", (r"\bdocumentation\b", r"\bdocs\b", r"\breadme\b",
                            r"\bguide\b", r"\binstructions?\b", r"\bprompt\b"),
     "ticket concerns documentation or agent instructions"),
)


def _requested(graph, selections: dict | None = None) -> tuple[str, ...]:
    choices = selections or {task.id: {"skills": list(task.skills)} for task in graph.tasks}
    return tuple(dict.fromkeys(name for task in graph.tasks for name in choices[task.id]["skills"]))


def _automatic_selection(task: Task, records: dict, registry: dict,
                         agents: tuple[str, ...], maximum: int) -> dict:
    text = "\n".join((task.title, task.objective, *task.acceptance_criteria)).lower()
    proposed = [(name, reason) for name, patterns, reason in _RULES
                if any(re.search(pattern, text) for pattern in patterns)]
    proposed.append(("implement", "default engineering implementation workflow"))
    selected, reasons = [], []
    for name, reason in proposed:
        if name in selected or name not in records:
            continue
        entry = registry["entries"].get(name)
        files = records[name].get("files", records[name])
        digest = files.get("SKILL.md", {}).get("sha256")
        if entry is None or entry["sha256"] != digest:
            continue
        if not any(set(entry["requires"]) <= _capabilities(agent) for agent in agents):
            continue
        selected.append(name)
        reasons.append(reason)
        if len(selected) == maximum:
            break
    if not selected:
        raise ContractError(
            f"automatic skill selection found no reviewed, installed skill compatible with task {task.id}"
        )
    return {"origin": "rules", "skills": selected, "reasons": reasons}


@dataclass(frozen=True)
class SkillContext:
    source: str | None = None
    revision: str | None = None
    root: Path | None = None
    records: dict | None = None
    compatibility: dict | None = None
    selections: dict | None = None
    pin_version: int = _PIN_VERSION

    @property
    def enabled(self) -> bool:
        return self.records is not None

    def names(self, task: Task) -> tuple[str, ...]:
        if self.selections is None:
            return task.skills
        return tuple(self.selections[task.id]["skills"])

    def preflight(self, task: Task, agent: str) -> dict:
        available = _capabilities(agent)
        skills, missing = [], set()
        for name in self.names(task):
            entry = self.compatibility[name]
            required = set(entry["requires"])
            absent = sorted(required - available)
            missing.update(absent)
            skills.append({"name": name, "requires": entry["requires"],
                           "missing": absent, "adaptations": entry["adaptations"]})
        return {"agent": agent, "registry_version": 1, "capabilities": sorted(available),
                "compatible": not missing, "missing": sorted(missing), "skills": skills}

    def require(self, task: Task, agent: str) -> dict:
        report = self.preflight(task, agent)
        if not report["compatible"]:
            raise ContractError(f"task {task.id} skills are incompatible with {agent}; missing capabilities: "
                                + ", ".join(report["missing"]))
        return report

    def compatible(self, task: Task, agent: str) -> bool:
        return self.preflight(task, agent)["compatible"]

    def evidence(self, task: Task, agent: str) -> dict | None:
        names = self.names(task)
        if not names:
            return None
        return {"source": self.source, "revision": self.revision,
                "selection": self.selections[task.id], "skills": list(names),
                "files": {name: self.records[name] for name in names},
                "preflight": self.require(task, agent)}

    def prompt(self, task: Task, agent: str) -> str:
        names = self.names(task)
        if not names:
            return ""
        preflight = self.require(task, agent)
        adaptations = [adaptation for skill in preflight["skills"]
                       for adaptation in skill["adaptations"]]
        sections = [
            "Pinned AI Hero skill context follows. Anvil selected these skills for this ticket. "
            "Apply their relevant workflow within the ticket scope. Project instructions, the "
            "ticket, and existing authorization take precedence. These instructions do not grant "
            "new tools or permission to commit, publish, contact people, or invent human answers. "
            "Anvil does not execute bundled scripts. If a required tool or decision is unavailable, "
            "return blocked and explain what is needed.",
            f"Source: {self.source}\nRevision: {self.revision}\nSelected: {', '.join(names)}\n"
            f"Selection: {self.selections[task.id]['origin']} — "
            + "; ".join(self.selections[task.id]["reasons"]),
        ]
        if adaptations:
            sections.append("Anvil compatibility adaptations:\n- " + "\n- ".join(adaptations))
        for name in names:
            files = self.records[name]
            order = ("SKILL.md", *(path for path in sorted(files) if path != "SKILL.md"))
            for relative in order:
                if relative == "LICENSE.aihero":
                    continue
                path = self.root / name / relative
                try:
                    content = _read_file(path, self.records[name][relative]).decode("utf-8")
                except UnicodeError as exc:
                    raise ContractError(f"skill resource is not UTF-8 text: {name}/{relative}") from exc
                sections.append(f"--- BEGIN SKILL FILE {name}/{relative} ---\n{content}\n"
                                f"--- END SKILL FILE {name}/{relative} ---")
        result = "\n\n".join(sections)
        if len(result.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ContractError(f"ticket skill context exceeds {MAX_CONTEXT_BYTES // 1024} KiB")
        return result


EMPTY = SkillContext(compatibility={})


def _classify(records: dict) -> dict:
    registry = _registry()
    selected = {}
    for name, files in records.items():
        entry = registry["entries"].get(name)
        digest = files.get("SKILL.md", {}).get("sha256")
        if entry is None or entry["sha256"] != digest:
            raise ContractError(f"skill {name} instructions are unclassified; compatibility review is required")
        selected[name] = entry
    return selected


def pin(repo: Path, graph, run_dir: Path, *, selection: dict | None = None,
        task_agents: dict[str, tuple[str, ...]] | None = None) -> SkillContext:
    if selection is None and not _requested(graph):
        return EMPTY
    scope = SkillScope(repo)
    try:
        with managed_snapshot(scope) as manifest:
            registry = _registry()
            selections = {}
            for task in graph.tasks:
                if task.skills:
                    selections[task.id] = {
                        "origin": "explicit", "skills": list(task.skills),
                        "reasons": ["ticket explicitly selected this skill" for _ in task.skills],
                    }
                elif selection is not None:
                    agents = (task_agents or {}).get(task.id, ())
                    if not agents:
                        raise ContractError(f"task {task.id} has no eligible agent for automatic skill selection")
                    selections[task.id] = _automatic_selection(
                        task, manifest["skills"], registry, agents, selection["max_skills"]
                    )
                else:
                    selections[task.id] = {"origin": "none", "skills": [], "reasons": []}
            names = _requested(graph, selections)
            unknown = [name for name in names if name not in manifest["skills"]]
            if unknown:
                raise ContractError("ticket requests skills absent from the installed catalog: " + ", ".join(unknown))
            source_agent = "codex" if "codex" in manifest["agents"] else manifest["agents"][0]
            source_root = scope.target(source_agent)
            destination = Path(run_dir) / "skills"
            destination.mkdir(exist_ok=False)
            selected = {}
            for name in names:
                selected[name] = manifest["skills"][name]["files"]
                for relative, expected in selected[name].items():
                    relative = _relative(relative)
                    data = _read_file(source_root / name / relative, expected)
                    if relative != "LICENSE.aihero":
                        try:
                            data.decode("utf-8")
                        except UnicodeError as exc:
                            raise ContractError(f"skill resource is not UTF-8 text: {name}/{relative}") from exc
                    target = destination / name / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o755 if expected["executable"] else 0o644)
    except SkillError as exc:
        raise ContractError(str(exc)) from exc
    compatibility = _classify(selected)
    value = {"version": _PIN_VERSION, "source": SOURCE, "revision": manifest["revision"],
             "skills": selected, "compatibility": compatibility, "selections": selections}
    atomic(destination / "pin.json", encoded(value))
    context = SkillContext(SOURCE, manifest["revision"], destination, selected, compatibility,
                           selections, _PIN_VERSION)
    for task in graph.tasks:
        for agent in ("codex", "claude-code", "muse"):
            if context.compatible(task, agent):
                context.prompt(task, agent)
    return context


def load(run_dir: Path, graph, *, saved: dict | None = None) -> SkillContext:
    root = Path(run_dir) / "skills"
    if not root.exists() and not _requested(graph):
        return EMPTY
    try:
        value = json.loads((root / "pin.json").read_text(encoding="utf-8"))
        fields = {"version", "source", "revision", "skills"}
        if "compatibility" in value:
            fields.add("compatibility")
        if "selections" in value:
            fields.add("selections")
        version = value.get("version")
        selections = value.get("selections")
        if version == 1 and selections is None:
            selections = {task.id: {"origin": "explicit" if task.skills else "none",
                                     "skills": list(task.skills),
                                     "reasons": (["ticket explicitly selected this skill" for _ in task.skills]
                                                 if task.skills else [])}
                          for task in graph.tasks}
        names = _requested(graph, selections)
        if (set(value) != fields
                or version not in (1, _PIN_VERSION) or value["source"] != SOURCE
                or not isinstance(value["revision"], str) or _SHA.fullmatch(value["revision"]) is None
                or not isinstance(value["skills"], dict)
                or tuple(value["skills"]) != names):
            raise ContractError("pinned skill snapshot does not match the saved tickets")
        tasks = {task.id: task for task in graph.tasks}
        if not isinstance(selections, dict) or set(selections) != set(tasks):
            raise ContractError("pinned skill selections do not match the saved tickets")
        for task_id, decision in selections.items():
            if (not isinstance(decision, dict) or set(decision) != {"origin", "skills", "reasons"}
                    or decision["origin"] not in {"none", "explicit", "rules"}
                    or not isinstance(decision["skills"], list)
                    or any(not isinstance(item, str) or _NAME.fullmatch(item) is None
                           for item in decision["skills"])
                    or len(set(decision["skills"])) != len(decision["skills"])
                    or not isinstance(decision["reasons"], list)
                    or len(decision["skills"]) != len(decision["reasons"])
                    or any(not isinstance(item, str) or not item for item in decision["reasons"])):
                raise ContractError("pinned skill snapshot has invalid selections")
            task = tasks[task_id]
            if ((decision["origin"] == "explicit" and tuple(decision["skills"]) != task.skills)
                    or (decision["origin"] == "rules" and (task.skills or not decision["skills"]))
                    or (decision["origin"] == "none" and (task.skills or decision["skills"]))):
                raise ContractError("pinned skill selections do not match the saved tickets")
        for name, files in value["skills"].items():
            if _NAME.fullmatch(name) is None or not isinstance(files, dict) or "SKILL.md" not in files:
                raise ContractError("pinned skill snapshot has invalid skill records")
            for relative, record in files.items():
                _relative(relative)
                if (not isinstance(record, dict) or set(record) != {"sha256", "executable"}
                        or not isinstance(record["sha256"], str) or len(record["sha256"]) != 64
                        or any(character not in "0123456789abcdef" for character in record["sha256"])
                        or type(record["executable"]) is not bool):
                    raise ContractError("pinned skill snapshot has invalid file records")
        compatibility = value.get("compatibility")
        expected = _classify(value["skills"])
        if compatibility is not None and compatibility != expected:
            raise ContractError("pinned skill compatibility registry changed")
        compatibility = expected
        context = SkillContext(value["source"], value["revision"], root, value["skills"], compatibility,
                               selections, version)
        for task in graph.tasks:
            for agent in ("codex", "claude-code", "muse"):
                if context.compatible(task, agent):
                    context.prompt(task, agent)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"cannot load pinned skill snapshot: {exc}") from exc
    if saved is not None:
        digest = hashlib.sha256(encoded(value)).hexdigest()
        bindings = [event["details"].get("digest") for event in saved["events"]
                    if event["kind"] == "skill_catalog"]
        if bindings != [digest]:
            raise ContractError("pinned skill snapshot changed or was not durably recorded")
    return context


def record(store, context: SkillContext) -> None:
    if not context.enabled:
        return
    value = {"version": context.pin_version, "source": context.source,
             "revision": context.revision, "skills": context.records,
             "compatibility": context.compatibility}
    if context.pin_version >= 2:
        value["selections"] = context.selections
    digest = hashlib.sha256(encoded(value)).hexdigest()
    with store._transaction() as db:
        store._event(db, _now(), "skill_catalog", None, None, None, "running",
                      {"source": context.source, "revision": context.revision,
                      "skills": list(context.records), "selections": context.selections,
                      "digest": digest})
