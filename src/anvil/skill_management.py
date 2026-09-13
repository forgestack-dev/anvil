"""Install a pinned AI Hero catalog into native agent skill directories.

The manifest owns individual skill directories, never the entire agent root.
Updates preflight all targets, preserve local changes, and roll back ordinary
filesystem errors. No downloaded scripts or agent processes are executed.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile

from .environment import managed_environment
from .processes import ProcessError, _defer_sigint, run_process
from .skill_source import Catalog, SOURCE, fetch_catalog


class SkillError(ValueError):
    """A skill operation cannot be completed without losing ownership or data."""


_AGENTS = ("codex", "claude-code")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class SkillScope:
    root: Path
    global_scope: bool = False
    claude_config_dir: Path | None = None

    def __post_init__(self):
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise SkillError(f"skill scope must be an existing directory: {root}")
        object.__setattr__(self, "root", root)
        if self.global_scope and self.claude_config_dir is not None:
            configured = Path(self.claude_config_dir).expanduser()
            # Canonicalize the explicitly selected parents, but leave the final
            # directory unresolved so installation checks still reject symlinks.
            configured = configured.parent.resolve() / configured.name
            if configured.name == "..":
                configured = configured.resolve()
            object.__setattr__(self, "claude_config_dir", configured)

    @property
    def manifest(self) -> Path:
        parent = self.root / (".local/state/anvil/skills" if self.global_scope else ".anvil")
        return parent / "aihero.json"

    def target(self, agent: str) -> Path:
        if agent == "claude-code" and self.global_scope and self.claude_config_dir is not None:
            return self.claude_config_dir / "skills"
        return self.root / (".agents/skills" if agent == "codex" else ".claude/skills")


def scope_for(repo: Path | None = None, *, global_scope: bool = False) -> SkillScope:
    if global_scope:
        if repo is not None:
            raise SkillError("choose either a repository or a global installation")
        configured = os.environ.get("CLAUDE_CONFIG_DIR") or None
        return SkillScope(Path.home(), True, Path(configured) if configured else None)
    directory = Path(repo or Path.cwd()).expanduser().resolve()
    if not directory.is_dir():
        raise SkillError(f"repository directory does not exist: {directory}")
    with tempfile.TemporaryDirectory(prefix="anvil-skills-git-") as temporary:
        stdout, stderr = Path(temporary) / "out", Path(temporary) / "err"
        try:
            outcome = run_process(
                ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                cwd=directory, stdin=None, stdout_path=stdout, stderr_path=stderr,
                timeout=30, env=managed_environment(),
            )
        except ProcessError as exc:
            raise SkillError(f"cannot locate the repository: {exc}") from exc
        if outcome.timed_out or outcome.returncode:
            raise SkillError("repository skill installation requires a Git working tree; use --repo or --global")
        return SkillScope(Path(stdout.read_text(encoding="utf-8").strip()))


def _name(value):
    if not isinstance(value, str) or len(value) > 64 or not _NAME.fullmatch(value) or value == "synced":
        raise SkillError(f"invalid or reserved skill name: {value!r}")
    return value


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or PurePosixPath(value).is_absolute()):
        raise SkillError(f"invalid skill file path: {value!r}")
    return value


def _safe_path(scope: SkillScope, path: Path):
    """Reject redirected managed ancestors, including dangling symlinks."""
    boundary = scope.root
    if (scope.global_scope and scope.claude_config_dir is not None
            and path == scope.target("claude-code")):
        # Only the explicitly configured agent root may live outside the home.
        # Check its canonical ancestors too, in case one changed after capture.
        boundary = Path(path.anchor)
    relative = path.relative_to(boundary)
    current = boundary
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SkillError(f"refusing a symlink in an installation path: {current}")
        if current.exists() and current != path and not current.is_dir():
            raise SkillError(f"installation parent is not a directory: {current}")


def _read_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SkillError(f"duplicate manifest key: {key}")
            result[key] = value
        return result
    try:
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > 8 * 1024 * 1024:
            raise SkillError("skill manifest must be a regular file of at most 8 MiB")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SkillError(f"invalid skill manifest: {path}: {exc}") from exc


def _read_manifest(scope: SkillScope):
    _safe_path(scope, scope.manifest)
    if not scope.manifest.exists():
        return None
    value = _read_json(scope.manifest)
    try:
        fields = {"version", "source", "revision", "agents", "selection", "skills"}
        if value.get("version") == 2:
            fields.add("claude_config_dir")
        if (set(value) != fields
                or type(value["version"]) is not int or value["version"] not in (1, 2)
                or value["source"] != SOURCE or not _SHA.fullmatch(value["revision"])):
            raise SkillError("unsupported skill manifest or source")
        agents = value["agents"]
        if (not isinstance(agents, list) or not agents or any(agent not in _AGENTS for agent in agents)
                or len(set(agents)) != len(agents)):
            raise SkillError("invalid manifest agents")
        if value["version"] == 2:
            configured = value["claude_config_dir"]
            if (not scope.global_scope or "claude-code" not in agents
                    or not isinstance(configured, str) or "\0" in configured
                    or not Path(configured).is_absolute()):
                raise SkillError("invalid manifest Claude configuration directory")
        if scope.global_scope and "claude-code" in agents:
            recorded = value.get("claude_config_dir", str(scope.root / ".claude"))
            requested = str(scope.target("claude-code").parent)
            if recorded != requested:
                raise SkillError(
                    f"this installation uses Claude configuration directory {recorded}; "
                    f"CLAUDE_CONFIG_DIR currently selects {requested}. "
                    "Set CLAUDE_CONFIG_DIR to the recorded directory before inspecting or updating; "
                    "changing an existing installation's destination is not supported."
                )
        selection = value["selection"]
        if (set(selection) != {"names", "include_experimental"}
                or not isinstance(selection["names"], list)
                or type(selection["include_experimental"]) is not bool):
            raise SkillError("invalid manifest selection")
        for name in selection["names"]:
            _name(name)
        if len(set(selection["names"])) != len(selection["names"]):
            raise SkillError("duplicate manifest selection")
        skills = value["skills"]
        if not isinstance(skills, dict) or not skills:
            raise SkillError("manifest has no skills")
        for name, skill in skills.items():
            _name(name)
            if set(skill) != {"source_path", "files"}:
                raise SkillError("invalid manifest skill")
            _relative(skill["source_path"])
            if not isinstance(skill["files"], dict) or "SKILL.md" not in skill["files"]:
                raise SkillError("manifest skill has no SKILL.md")
            folded = set()
            for path, record in skill["files"].items():
                _relative(path)
                if path.casefold() in folded:
                    raise SkillError("case-colliding manifest files")
                folded.add(path.casefold())
                if (set(record) != {"sha256", "executable"} or not _HASH.fullmatch(record["sha256"])
                        or type(record["executable"]) is not bool):
                    raise SkillError("invalid manifest file hash or mode")
        return value
    except (TypeError, KeyError, AttributeError) as exc:
        raise SkillError(f"malformed skill manifest: {scope.manifest}") from exc


def _file_record(content: bytes, executable: bool):
    return {"sha256": hashlib.sha256(content).hexdigest(), "executable": executable}


def _snapshot(path: Path):
    if path.is_symlink() or not path.is_dir():
        raise SkillError(f"managed skill is missing or is not a regular directory: {path}")
    records = {}
    total = 0
    def inspection_error(error):
        raise SkillError(f"cannot inspect managed skill {path}: {error}") from error

    for parent, directories, files in os.walk(path, followlinks=False, onerror=inspection_error):
        for name in directories:
            child = Path(parent) / name
            if child.is_symlink():
                raise SkillError(f"managed skill contains a symlink: {child}")
        for name in files:
            child = Path(parent) / name
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SkillError(f"managed skill contains a non-regular file: {child}")
            total += info.st_size
            if total > 64 * 1024 * 1024 or len(records) >= 10000:
                raise SkillError(f"managed skill exceeds inspection limits: {path}")
            relative = child.relative_to(path).as_posix()
            records[relative] = _file_record(child.read_bytes(), bool(info.st_mode & 0o111))
    return records


def _conflicts(scope: SkillScope, manifest: dict | None):
    conflicts = []
    if manifest is not None:
        for agent in manifest["agents"]:
            root = scope.target(agent)
            try:
                _safe_path(scope, root)
            except SkillError as exc:
                conflicts.append(str(exc))
                continue
            for name, skill in manifest["skills"].items():
                try:
                    if _snapshot(root / name) != skill["files"]:
                        conflicts.append(f"local changes in {root / name}")
                except (SkillError, OSError) as exc:
                    conflicts.append(str(exc))
    return conflicts


def _report(scope, manifest, status, *, changes=None, conflicts=None):
    return {"status": status, "source": SOURCE, "revision": manifest["revision"] if manifest else None,
            "agents": {agent: str(scope.target(agent)) for agent in manifest["agents"]} if manifest else {},
            "skills": sorted(manifest["skills"]) if manifest else [], "manifest": str(scope.manifest),
            "changes": changes or {"added": [], "updated": [], "removed": []}, "conflicts": conflicts or []}


@contextmanager
def _locked(scope: SkillScope, *, create: bool):
    if os.name != "posix":
        raise SkillError("skill management currently requires macOS or Linux")
    import fcntl
    parent = scope.manifest.parent
    _safe_path(scope, parent)
    if not parent.exists() and not create:
        yield
        return
    if create:
        parent.mkdir(parents=True, exist_ok=True)
    # Lock the stable metadata directory itself; no lock file enters the repo.
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if create else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SkillError(f"another skill operation is using {parent}") from exc
        yield
    finally:
        os.close(descriptor)


def status(scope: SkillScope) -> dict:
    with _locked(scope, create=False):
        manifest = _read_manifest(scope)
        conflicts = _conflicts(scope, manifest)
        return _report(scope, manifest, "modified" if conflicts else ("installed" if manifest else "not-installed"),
                       conflicts=conflicts)


def _catalog_manifest(scope: SkillScope, catalog: Catalog, agents, selection):
    if not _SHA.fullmatch(catalog.revision) or not catalog.skills:
        raise SkillError("source catalog must contain skills at an exact commit")
    skills = {}
    for name, skill in catalog.skills.items():
        _name(name)
        _relative(skill.source_path)
        if "SKILL.md" not in skill.files:
            raise SkillError(f"source skill has no SKILL.md: {name}")
        files = {}
        for path, file in skill.files.items():
            _relative(path)
            if not isinstance(file.content, bytes) or type(file.executable) is not bool:
                raise SkillError("invalid source file")
            files[path] = _file_record(file.content, file.executable)
        skills[name] = {"source_path": skill.source_path, "files": files}
    manifest = {"version": 1, "source": SOURCE, "revision": catalog.revision,
                "agents": list(agents), "selection": selection, "skills": skills}
    if (scope.global_scope and "claude-code" in agents
            and scope.target("claude-code") != scope.root / ".claude/skills"):
        manifest.update(version=2, claude_config_dir=str(scope.target("claude-code").parent))
    return manifest


def _preflight(scope, old, new):
    roots = [scope.target(agent) for agent in new["agents"]]
    for index, root in enumerate(roots):
        for other in [scope.manifest.parent, *roots[:index]]:
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise SkillError(f"skill destinations and metadata must not overlap: {root} and {other}")
    conflicts = _conflicts(scope, old)
    owned = set(old["skills"]) if old else set()
    for agent in new["agents"]:
        root = scope.target(agent)
        _safe_path(scope, root)
        if root.exists() and not root.is_dir():
            raise SkillError(f"agent skills path is not a directory: {root}")
        existing = {path.name.casefold(): path for path in root.iterdir()} if root.exists() else {}
        for name in new["skills"]:
            path = existing.get(name.casefold())
            if path is not None and (name not in owned or path.name != name):
                conflicts.append(f"unmanaged skill already exists: {path}")
    if conflicts:
        raise SkillError("skill installation conflicts; no skills changed:\n" + "\n".join(conflicts))


def _changes(old, new):
    previous = old["skills"] if old else {}
    current = new["skills"]
    return {"added": sorted(current.keys() - previous.keys()),
            "updated": sorted(name for name in current.keys() & previous.keys() if current[name] != previous[name]),
            "removed": sorted(previous.keys() - current.keys())}


def _apply(scope, old, new, catalog, changes):
    """Stage on each target filesystem and retain backups through manifest commit."""
    temporary = []
    moved = []
    placed = []
    old_manifest = scope.manifest.read_bytes() if old else None
    manifest_replaced = False
    try:
        stages = {}
        for agent in new["agents"]:
            target = scope.target(agent)
            target.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix=".anvil-aihero-", dir=target.parent))
            temporary.append(directory)
            stages[agent] = directory
            for name in changes["added"] + changes["updated"]:
                staged = directory / "new" / name
                staged.mkdir(parents=True)
                for relative, file in catalog.skills[name].files.items():
                    path = staged / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(file.content)
                    path.chmod(0o755 if file.executable else 0o644)
        # Catch edits made while downloading/staging before replacing anything.
        _preflight(scope, old, new)
        with _defer_sigint():
            try:
                for agent, directory in stages.items():
                    target = scope.target(agent)
                    for name in changes["updated"] + changes["removed"]:
                        backup = directory / "old" / name
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        original = target / name
                        original.rename(backup)
                        moved.append((original, backup))
                    for name in changes["added"] + changes["updated"]:
                        destination = target / name
                        (directory / "new" / name).rename(destination)
                        placed.append(destination)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=scope.manifest.parent,
                                                 prefix=".aihero-", delete=False) as stream:
                    manifest_stage = Path(stream.name)
                    temporary.append(manifest_stage)
                    stream.write(json.dumps(new, indent=2, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                manifest_stage.replace(scope.manifest)
                manifest_replaced = True
            except BaseException:
                # Roll back all targets together, including additions/removals.
                for destination in reversed(placed):
                    shutil.rmtree(destination)
                for original, backup in reversed(moved):
                    backup.rename(original)
                if manifest_replaced:
                    if old_manifest is None:
                        scope.manifest.unlink()
                    else:
                        scope.manifest.write_bytes(old_manifest)
                raise
    finally:
        # Do not discard backups if rollback itself failed.
        retained = [backup for original, backup in moved if backup.exists()]
        if retained and not manifest_replaced:
            raise SkillError("update rollback needs manual recovery; preserved backups: "
                             + ", ".join(map(str, retained)))
        for path in temporary:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()


def _operate(scope, *, updating, agents, ref, names, include_experimental, dry_run):
    def perform():
        old = _read_manifest(scope)
        selected_agents = agents
        selection = {"names": sorted(names), "include_experimental": include_experimental}
        if updating:
            if old is None:
                raise SkillError("AI Hero is not installed in this scope; run skills install aihero first")
            selected_agents, selection = tuple(old["agents"]), old["selection"]
        if (not selected_agents or any(agent not in _AGENTS for agent in selected_agents)
                or len(set(selected_agents)) != len(selected_agents)):
            raise SkillError("agents must be a nonempty unique selection of codex and claude-code")
        selected_agents = tuple(agent for agent in _AGENTS if agent in selected_agents)
        if type(selection["include_experimental"]) is not bool or len(set(selection["names"])) != len(selection["names"]):
            raise SkillError("invalid skill selection")
        for name in selection["names"]:
            _name(name)
        existing_conflicts = _conflicts(scope, old)
        if existing_conflicts:
            raise SkillError("local skill changes must be resolved before updating:\n" + "\n".join(existing_conflicts))
        catalog = fetch_catalog(ref=ref, names=tuple(selection["names"]),
                                include_experimental=selection["include_experimental"])
        new = _catalog_manifest(scope, catalog, selected_agents, selection)
        _preflight(scope, old, new)
        if old is not None and not updating and old != new:
            raise SkillError("AI Hero is already managed here; use skills update aihero to update its recorded agents and selection")
        changes = _changes(old, new)
        if old == new:
            return _report(scope, new, "unchanged", changes=changes)
        if dry_run:
            return _report(scope, new, "dry-run", changes=changes)
        _apply(scope, old, new, catalog, changes)
        return _report(scope, new, "updated" if old else "installed", changes=changes)

    if dry_run:
        with _locked(scope, create=False):
            return perform()
    with _locked(scope, create=True):
        return perform()


def install(scope: SkillScope, *, agents=("codex", "claude-code"), ref="main", names=(),
            include_experimental=False, dry_run=False) -> dict:
    return _operate(scope, updating=False, agents=agents, ref=ref, names=names,
                    include_experimental=include_experimental, dry_run=dry_run)


def update(scope: SkillScope, *, ref="main", dry_run=False) -> dict:
    return _operate(scope, updating=True, agents=(), ref=ref, names=(),
                    include_experimental=False, dry_run=dry_run)
