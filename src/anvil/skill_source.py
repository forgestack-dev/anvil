"""Fetch a pinned, data-only snapshot of the official AI Hero skill catalog.

Default selection contains engineering and productivity skills. Experimental
selection additionally includes in-progress skills; explicit names may select
misc skills too. In-progress always requires opt-in; deprecated skills are never
installed. Every selected directory is copied in full, retaining bytes and its
files' executable flags. Nothing from the archive is executed or extracted.
"""

from __future__ import annotations

from dataclasses import dataclass
import gzip
from http.client import HTTPException
from io import BytesIO
import json
import re
import tarfile
import time
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


SOURCE = "https://github.com/mattpocock/skills"
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_STABLE = {"engineering", "productivity"}
_MAX_DOWNLOAD = 16 * 1024 * 1024
_MAX_UNPACKED = 64 * 1024 * 1024
_MAX_FILE = 4 * 1024 * 1024
_MAX_ENTRIES = 4096
_TIMEOUT = 20


class SourceError(ValueError):
    """The upstream source cannot produce a complete, safe catalog snapshot."""


@dataclass(frozen=True)
class SourceFile:
    content: bytes
    executable: bool = False


@dataclass(frozen=True)
class Skill:
    source_path: str
    files: dict[str, SourceFile]


@dataclass(frozen=True)
class Catalog:
    revision: str
    skills: dict[str, Skill]


def _download(url: str, limit: int) -> bytes:
    """Bound bytes and elapsed body reads; accept only the requested GitHub host."""
    request = Request(url, headers={"User-Agent": "forgestack-anvil",
                                    "Accept": "application/vnd.github+json"})
    deadline = time.monotonic() + _TIMEOUT
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            resolved = urlparse(response.geturl())
            if resolved.scheme != "https" or resolved.netloc != urlparse(url).netloc:
                raise SourceError("upstream download redirected outside its expected HTTPS host")
            chunks, size = [], 0
            # read1 returns after a socket read, allowing an elapsed-time check
            # between arrivals instead of waiting to fill a large read buffer.
            while True:
                if time.monotonic() >= deadline:
                    raise SourceError("upstream download exceeded its time limit")
                chunk = response.read1(min(64 * 1024, limit + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise SourceError("upstream download exceeded its size limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except (OSError, ValueError, HTTPException) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError(f"cannot download upstream skills: {exc}") from exc


def _name(value: object) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 64
            or _NAME.fullmatch(value) is None or value == "synced"):
        raise SourceError("skill names must be 1–64 lowercase letters, digits and single hyphens; synced is reserved")
    return value


def _skill_name(content: bytes, path: str) -> str:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise SourceError(f"skill metadata must be UTF-8: {path}") from exc
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise SourceError(f"skill requires YAML frontmatter: {path}")
    header = lines[1:lines.index("---", 1)]
    names = [line[len("name:"):].strip() for line in header if line.startswith("name:")]
    if len(names) != 1:
        raise SourceError(f"skill requires exactly one name field: {path}")
    name = names[0]
    # The name grammar needs no general YAML implementation. Support the plain
    # and quoted scalar forms while refusing aliases, multiline values and tags.
    if name.startswith('"'):
        try:
            name = json.loads(name)
        except ValueError as exc:
            raise SourceError(f"invalid quoted skill name: {path}") from exc
    elif name.startswith("'") and name.endswith("'"):
        name = name[1:-1].replace("''", "'")
    return _name(name)


def _archive_catalog(data: bytes, revision: str, include_experimental: bool,
                     names: tuple[str, ...]) -> Catalog:
    if len(data) > _MAX_DOWNLOAD:
        raise SourceError("upstream archive exceeds its download size limit")
    try:
        with gzip.GzipFile(fileobj=BytesIO(data)) as compressed:
            unpacked = compressed.read(_MAX_UNPACKED + 1)
        if len(unpacked) > _MAX_UNPACKED:
            raise SourceError("upstream archive exceeds its decompressed size limit")
        archive = tarfile.open(fileobj=BytesIO(unpacked), mode="r:")
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise SourceError(f"invalid upstream archive: {exc}") from exc

    with archive:
        entries: dict[str, tarfile.TarInfo] = {}
        spellings: set[str] = set()
        expected_root = f"skills-{revision}"
        try:
            for number, member in enumerate(archive, start=1):
                if number > _MAX_ENTRIES:
                    raise SourceError("upstream archive contains too many entries")
                path = member.name.removesuffix("/")
                parts = path.split("/")
                if (len(path) > 512 or "\\" in path or "\0" in path
                        or any(part in {"", ".", ".."} for part in parts)
                        or parts[0] != expected_root):
                    raise SourceError(f"invalid upstream archive path: {member.name!r}")
                if path.casefold() in spellings:
                    raise SourceError(f"duplicate or case-colliding archive path: {path}")
                spellings.add(path.casefold())
                if len(parts) == 1:
                    if not member.isdir():
                        raise SourceError("upstream archive root must be a directory")
                    continue
                if member.size < 0 or member.size > _MAX_FILE:
                    raise SourceError(f"upstream file exceeds its size limit: {path}")
                entries["/".join(parts[1:])] = member

            def read_file(path: str) -> SourceFile:
                member = entries.get(path)
                if member is None or not member.isfile():
                    raise SourceError(f"upstream input must be a regular file, not a link: {path}")
                if member.mode & 0o7000:
                    raise SourceError(f"unsupported upstream file mode: {path}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SourceError(f"cannot read upstream file: {path}")
                with stream:
                    content = stream.read(_MAX_FILE + 1)
                if len(content) != member.size:
                    raise SourceError(f"truncated upstream file: {path}")
                return SourceFile(content, bool(member.mode & 0o111))

            license_file = read_file("LICENSE")
            if not license_file.content.strip():
                raise SourceError("upstream license must not be empty")
            discovered: dict[str, tuple[str, str]] = {}
            for path in sorted(entries):
                parts = path.split("/")
                if len(parts) < 4 or parts[0] != "skills" or parts[-1] != "SKILL.md":
                    continue
                name = _skill_name(read_file(path).content, path)
                if name != parts[-2]:
                    raise SourceError(f"skill name does not match its directory: {path}")
                if name in discovered:
                    raise SourceError(f"duplicate upstream skill name: {name}")
                discovered[name] = ("/".join(parts[:-1]), parts[1])
            selected = names or tuple(name for name, (_, category) in discovered.items()
                                      if category in _STABLE or (include_experimental and category == "in-progress"))
            if not selected:
                raise SourceError("upstream catalog contains no selected skills")
            skills = {}
            for name in sorted(selected):
                if name not in discovered:
                    raise SourceError(f"upstream skill was not found: {name}")
                directory, category = discovered[name]
                if category == "deprecated":
                    raise SourceError(f"deprecated skill cannot be installed: {name}")
                if category == "in-progress" and not include_experimental:
                    raise SourceError(f"experimental skill requires explicit opt-in: {name}")
                if directory in entries and not entries[directory].isdir():
                    raise SourceError(f"upstream skill root must be a directory: {directory}")
                files = {}
                for path, member in entries.items():
                    if not path.startswith(directory + "/"):
                        continue
                    if member.isdir():
                        continue
                    relative = path[len(directory) + 1:]
                    files[relative] = read_file(path)
                if any(path.casefold() == "license.aihero" and path != "LICENSE.aihero" for path in files):
                    raise SourceError(f"upstream license destination has a case collision: {name}")
                existing_license = files.get("LICENSE.aihero")
                if existing_license is not None and existing_license.content != license_file.content:
                    raise SourceError(f"upstream license destination already contains different content: {name}")
                files.setdefault("LICENSE.aihero", SourceFile(license_file.content))
                skills[name] = Skill(directory, files)
            return Catalog(revision, skills)
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise SourceError(f"cannot read upstream archive: {exc}") from exc


def fetch_catalog(ref: str = "main", include_experimental: bool = False,
                  names: tuple[str, ...] = ()) -> Catalog:
    """Resolve one ref once, then fetch and discover its immutable source archive."""
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 200 or "\0" in ref:
        raise SourceError("upstream ref must be nonempty text of at most 200 characters without NUL")
    if type(include_experimental) is not bool:
        raise SourceError("include_experimental must be a boolean")
    if not isinstance(names, tuple):
        raise SourceError("skill names must be a tuple")
    for name in names:
        _name(name)
    if len(set(names)) != len(names):
        raise SourceError("requested skills contain duplicate names")
    raw = _download("https://api.github.com/repos/mattpocock/skills/commits/" + quote(ref, safe=""),
                    1024 * 1024)
    try:
        metadata = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SourceError("upstream commit response is not valid JSON") from exc
    revision = metadata.get("sha") if isinstance(metadata, dict) else None
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise SourceError("upstream commit response has no full commit SHA")
    archive = _download(f"https://codeload.github.com/mattpocock/skills/tar.gz/{revision}", _MAX_DOWNLOAD)
    return _archive_catalog(archive, revision, include_experimental, names)
