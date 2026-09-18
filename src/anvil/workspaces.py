"""Isolated Git workspaces and guarded, local-only integration.

Only worktrees created by a Repository instance can be committed, reset, or
removed through its managed operations. Git hooks and signing are disabled for
these mechanical operations; project verification is run explicitly by Anvil.
Managed commits have an Anvil identity, so a user's Git identity is not required.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
import fcntl
import re
from pathlib import Path
import tempfile
from typing import IO

from .environment import managed_environment
from .processes import ProcessError, run_process


_GIT_TIMEOUT = 120
_MAX_GIT_OUTPUT = 32 * 1024 * 1024


def _output_tail(path: Path) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 4000))
        return stream.read().decode("utf-8", errors="replace").strip()


class WorkspaceError(RuntimeError):
    """A Git workspace cannot be safely used or integrated."""


_REF_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class Repository:
    """A committed repository and the detached worktrees this run owns."""

    def __init__(self, path: Path, *, exclude=()):
        self.path = Path(path).resolve()
        self.exclude = tuple(exclude)
        self._managed_worktrees: set[Path] = set()
        self.control_ticket = None
        top = Path(self.git("rev-parse", "--show-toplevel")).resolve()
        if top != self.path:
            raise WorkspaceError(f"repository path must be its top-level directory: {top}")
        common = self.git("rev-parse", "--git-common-dir")
        self.common_dir = (self.path / common).resolve()
        self.head()  # Reject unborn branches before creating any run artifacts.

    def git(self, *args: str, cwd: Path | None = None) -> str:
        """Run literal, noninteractive Git and stop any filter/helper descendants.

        Hooks are disabled, but Git still launches credential helpers, askpass
        programs and clean/smudge filters of its own, so it runs under the same
        exclusion set as an agent turn.
        """
        environment = managed_environment(self.exclude)
        environment.update({
            "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no",
            "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C",
        })
        command = [
            "git", "--no-pager", "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
            "-c", "user.name=Anvil", "-c", "user.email=anvil@localhost",
            *args,
        ]
        try:
            with tempfile.TemporaryDirectory(prefix="anvil-git-") as temporary:
                stdout, stderr = Path(temporary) / "stdout", Path(temporary) / "stderr"
                result = run_process(
                    command, cwd=cwd or self.path, env=environment, stdin=None,
                    stdout_path=stdout, stderr_path=stderr, timeout=_GIT_TIMEOUT,
                )
                if result.timed_out:
                    raise WorkspaceError(f"cannot run Git: timed out after {_GIT_TIMEOUT} seconds")
                if result.returncode:
                    detail = _output_tail(stderr) or _output_tail(stdout)
                    raise WorkspaceError(f"Git {args[0] if args else ''} failed: {detail}")
                with stdout.open("rb") as stream:
                    output = stream.read(_MAX_GIT_OUTPUT + 1)
                if len(output) > _MAX_GIT_OUTPUT:
                    raise WorkspaceError("Git output exceeded the 32 MiB capture limit")
                return output.decode("utf-8", errors="replace").strip()
        except (OSError, ValueError, ProcessError) as exc:
            raise WorkspaceError(f"cannot run Git: {exc}") from exc

    def _commit(self, revision: str, *, cwd: Path | None = None) -> str:
        return self.git("rev-parse", "--verify", "--end-of-options",
                        f"{revision}^{{commit}}", cwd=cwd)

    def head(self) -> str:
        return self._commit("HEAD")

    def _assert_clean(self, path: Path) -> None:
        if self.git("status", "--porcelain=v1", "--untracked-files=all",
                    "--ignore-submodules=none", cwd=path):
            raise WorkspaceError(f"workspace has uncommitted or untracked changes: {path}")

    def assert_clean(self) -> None:
        if self.control_ticket is None:
            self._assert_clean(self.path)
            return
        from .ticket_status import document, inputs, read_regular
        committed = document(self.git("show", f"HEAD:{self.control_ticket}").encode())
        if inputs(committed) != inputs(document(read_regular(self.path / self.control_ticket))):
            raise WorkspaceError("ticket input changed")
        if self.git("diff", "--cached", "--name-only") or self.git(
                "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none",
                "--", ".", f":(literal,exclude){self.control_ticket}"):
            raise WorkspaceError("workspace has uncommitted or untracked changes")

    def _managed(self, path: Path) -> Path:
        path = Path(path).resolve()
        if path not in self._managed_worktrees or path == self.path:
            raise WorkspaceError(f"workspace is not owned by this run: {path}")
        if Path(self.git("rev-parse", "--show-toplevel", cwd=path)).resolve() != path:
            raise WorkspaceError(f"workspace is no longer a Git worktree: {path}")
        common = (path / self.git("rev-parse", "--git-common-dir", cwd=path)).resolve()
        if common != self.common_dir:
            raise WorkspaceError(f"workspace no longer belongs to this repository: {path}")
        if self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=path) != "HEAD":
            raise WorkspaceError(f"managed workspace must have a detached HEAD: {path}")
        return path

    def assert_revision(self, path: Path, sha: str) -> None:
        """Reject edits or HEAD movement after review or verification."""
        path = self._managed(path)
        if self._commit("HEAD", cwd=path) != self._commit(sha):
            raise WorkspaceError(f"workspace HEAD changed from the expected revision: {path}")
        self._assert_clean(path)

    def create_worktree(self, path: Path, base: str) -> None:
        path = Path(path).resolve()
        if path.exists() or path == self.path:
            raise WorkspaceError(f"workspace path already exists: {path}")
        base = self._commit(base)
        self.git("worktree", "add", "--detach", "--", str(path), base)
        self._managed_worktrees.add(path)
        self.assert_revision(path, base)

    def _branch_ref(self, branch: str) -> str:
        if not isinstance(branch, str) or not branch or branch.startswith("refs/"):
            raise WorkspaceError("branch must be a short branch name, without refs/")
        ref = f"refs/heads/{branch}"
        self.git("check-ref-format", ref)
        return ref

    def create_branch(self, branch: str, base: str) -> None:
        ref, base = self._branch_ref(branch), self._commit(base)
        self.git("update-ref", ref, base, "0" * len(base))

    def _single_parent(self, revision: str, expected: str | None = None) -> None:
        parents = self.git("rev-list", "--parents", "-n", "1", revision).split()[1:]
        if len(parents) != 1 or (expected is not None and parents[0] != expected):
            raise WorkspaceError("candidate must be one commit with the expected single parent")

    def commit_candidate(self, worktree: Path, base: str, message: str) -> str:
        path, base = self._managed(worktree), self._commit(base)
        if self._commit("HEAD", cwd=path) != base:
            raise WorkspaceError("worker moved HEAD; Anvil must own candidate commits")
        if not isinstance(message, str) or not message.strip():
            raise WorkspaceError("candidate commit message must be nonempty")
        if self.control_ticket and self.git("status", "--porcelain=v1", "--",
                                            self.control_ticket, cwd=path):
            raise WorkspaceError("worker changed the protected ticket control file")
        self.git("add", "--all", "--", ".", cwd=path)
        tree = self.git("write-tree", cwd=path)
        if tree == self.git("rev-parse", f"{base}^{{tree}}"):
            raise WorkspaceError("worker produced an empty candidate")
        self.git("commit", "--no-gpg-sign", "-m", message, cwd=path)
        candidate = self._commit("HEAD", cwd=path)
        self._single_parent(candidate, base)
        if self.git("rev-parse", f"{candidate}^{{tree}}") != tree:
            raise WorkspaceError("candidate tree changed during commit")
        self.assert_revision(path, candidate)
        return candidate

    def prepare_integration(self, path: Path, expected_base: str, candidate: str) -> str:
        """Create a detached integration candidate without advancing any branch."""
        path = self._managed(path)
        self._assert_clean(path)
        expected_base, candidate = self._commit(expected_base), self._commit(candidate)
        self._single_parent(candidate)
        self.git("reset", "--hard", expected_base, cwd=path)
        self.git("cherry-pick", "--no-gpg-sign", candidate, cwd=path)
        integrated = self._commit("HEAD", cwd=path)
        self._single_parent(integrated, expected_base)
        self.assert_revision(path, integrated)
        return integrated

    RETAINED = ("candidate", "integration")

    def retain(self, kind: str, run_id: str, attempt_id: str, sha: str) -> str:
        """Name a revision under refs/anvil/ so it survives an ordinary prune.

        The ledger records candidate_sha and integration_sha, but a rejected
        attempt's commits are reachable from no ref once its worktree is gone,
        so `git gc` destroys the evidence the ledger points at. The managed
        branch already holds accepted work; these refs are what make a rejection
        auditable, which is the replay docs/OBSERVED_LIMITS.md describes.

        Nothing reads them. They are evidence, not input: no acceptance,
        rejection, retry or recovery decision may depend on one existing.
        """
        if kind not in self.RETAINED:
            raise WorkspaceError(f"retained revision kind must be one of {self.RETAINED}")
        for name, value in (("run ID", run_id), ("attempt ID", attempt_id)):
            if not isinstance(value, str) or not _REF_COMPONENT.fullmatch(value):
                raise WorkspaceError(f"retained {name} must match {_REF_COMPONENT.pattern}")
        ref = f"refs/anvil/{kind}/{run_id}/{attempt_id}"
        self.git("update-ref", ref, self._commit(sha))
        return ref

    def advance_branch(self, branch: str, old: str, new: str) -> None:
        """Compare-and-swap an unchecked-out branch after successful verification."""
        ref = self._branch_ref(branch)
        old, new = self._commit(old), self._commit(new)
        self._single_parent(new, old)
        records = self.git("worktree", "list", "--porcelain", "-z").split("\0")
        if f"branch {ref}" in records:
            raise WorkspaceError(f"integration branch is checked out in a workspace: {branch}")
        self.git("update-ref", ref, new, old)

    def remove_worktree(self, path: Path) -> None:
        path = self._managed(path)
        self._assert_clean(path)
        self.git("worktree", "remove", "--", str(path))
        self._managed_worktrees.remove(path)


class RepositoryLock(AbstractContextManager):
    """A nonblocking lock shared by runs regardless of their state location."""

    def __init__(self, repo: Repository):
        self.path = repo.common_dir / "anvil.lock"
        self._file: IO[str] | None = None

    def __enter__(self) -> RepositoryLock:
        if self._file is not None:
            raise WorkspaceError("repository lock is already held by this instance")
        try:
            self._file = self.path.open("a+", encoding="utf-8")
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise WorkspaceError(f"repository is locked by another Anvil run: {self.path}") from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
