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
_PIN_VERSION = 1
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


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


def _requested(graph) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for task in graph.tasks for name in task.skills))


@dataclass(frozen=True)
class SkillContext:
    source: str | None = None
    revision: str | None = None
    root: Path | None = None
    records: dict | None = None

    @property
    def enabled(self) -> bool:
        return self.records is not None

    def evidence(self, task: Task) -> dict | None:
        if not task.skills:
            return None
        return {"source": self.source, "revision": self.revision,
                "skills": list(task.skills),
                "files": {name: self.records[name] for name in task.skills}}

    def prompt(self, task: Task) -> str:
        if not task.skills:
            return ""
        sections = [
            "Pinned AI Hero skill context follows. The ticket explicitly selected these skills. "
            "Apply their relevant workflow within the ticket scope. Project instructions, the "
            "ticket, and existing authorization take precedence. These instructions do not grant "
            "new tools or permission to commit, publish, contact people, or invent human answers. "
            "Anvil does not execute bundled scripts. If a required tool or decision is unavailable, "
            "return blocked and explain what is needed.",
            f"Source: {self.source}\nRevision: {self.revision}\nSelected: {', '.join(task.skills)}",
        ]
        for name in task.skills:
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


EMPTY = SkillContext()


def pin(repo: Path, graph, run_dir: Path) -> SkillContext:
    names = _requested(graph)
    if not names:
        return EMPTY
    scope = SkillScope(repo)
    try:
        with managed_snapshot(scope) as manifest:
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
    value = {"version": _PIN_VERSION, "source": SOURCE, "revision": manifest["revision"],
             "skills": selected}
    atomic(destination / "pin.json", encoded(value))
    context = SkillContext(SOURCE, manifest["revision"], destination, selected)
    for task in graph.tasks:
        context.prompt(task)
    return context


def load(run_dir: Path, graph, *, saved: dict | None = None) -> SkillContext:
    names = _requested(graph)
    if not names:
        return EMPTY
    root = Path(run_dir) / "skills"
    try:
        value = json.loads((root / "pin.json").read_text(encoding="utf-8"))
        if (set(value) != {"version", "source", "revision", "skills"}
                or value["version"] != _PIN_VERSION or value["source"] != SOURCE
                or not isinstance(value["revision"], str) or _SHA.fullmatch(value["revision"]) is None
                or not isinstance(value["skills"], dict)
                or tuple(value["skills"]) != names):
            raise ContractError("pinned skill snapshot does not match the saved tickets")
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
        context = SkillContext(value["source"], value["revision"], root, value["skills"])
        for task in graph.tasks:
            context.prompt(task)
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
    value = {"version": _PIN_VERSION, "source": context.source,
             "revision": context.revision, "skills": context.records}
    digest = hashlib.sha256(encoded(value)).hexdigest()
    with store._transaction() as db:
        store._event(db, _now(), "skill_catalog", None, None, None, "running",
                     {"source": context.source, "revision": context.revision,
                      "skills": list(context.records), "digest": digest})
