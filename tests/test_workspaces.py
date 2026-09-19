"""Real Git coverage for isolation, candidate ownership, and branch publication."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from anvil.workspaces import Repository, RepositoryLock, WorkspaceError
from anvil.processes import ProcessError, ProcessOutcome


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "application with spaces"
        self.path.mkdir()
        self.raw_git("init", "--initial-branch=main")
        self.raw_git("config", "user.name", "Fixture")
        self.raw_git("config", "user.email", "fixture@example.invalid")
        (self.path / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.raw_git("add", ".")
        self.raw_git("-c", "commit.gpgSign=false", "commit", "-m", "Initial fixture")
        self.repo = Repository(self.path)
        self.base = self.repo.head()

    def raw_git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd or self.path, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def worktree(self, name="worker") -> Path:
        path = self.root / name
        self.repo.create_worktree(path, self.base)
        return path

    def candidate(self, name="worker") -> tuple[Path, str]:
        path = self.worktree(name)
        (path / "tracked.txt").write_text("implemented\n", encoding="utf-8")
        (path / "new.txt").write_text("acceptance evidence\n", encoding="utf-8")
        return path, self.repo.commit_candidate(path, self.base, "Implement ticket")

    def test_requires_repository_top_level_and_committed_head(self):
        subdirectory = self.path / "subdirectory"
        subdirectory.mkdir()
        with self.assertRaisesRegex(WorkspaceError, "top-level"):
            Repository(subdirectory)
        empty = self.root / "empty"
        empty.mkdir()
        self.raw_git("init", cwd=empty)
        with self.assertRaises(WorkspaceError):
            Repository(empty)
        self.assertEqual(self.repo.common_dir, self.path / ".git")
        self.assertEqual(len(self.base), 40)

    def test_dirty_original_checkout_and_its_branch_are_preserved(self):
        (self.path / "tracked.txt").write_text("user edits\n", encoding="utf-8")
        (self.path / "personal.txt").write_text("user notes\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "uncommitted or untracked"):
            self.repo.assert_clean()
        worker, candidate = self.candidate()
        integration = self.worktree("integration")
        self.repo.create_branch("anvil/run-one", self.base)
        integrated = self.repo.prepare_integration(integration, self.base, candidate)
        self.assertEqual(self.repo.git("rev-parse", "anvil/run-one"), self.base)
        self.repo.advance_branch("anvil/run-one", self.base, integrated)
        self.assertEqual(self.repo.git("rev-parse", "anvil/run-one"), integrated)
        self.assertEqual(self.repo.head(), self.base)
        self.assertEqual(self.repo.git("branch", "--show-current"), "main")
        self.assertEqual((self.path / "tracked.txt").read_text(), "user edits\n")
        self.assertEqual((self.path / "personal.txt").read_text(), "user notes\n")
        self.assertEqual((integration / "new.txt").read_text(), "acceptance evidence\n")
        self.assertEqual(self.repo.git("show", "-s", "--format=%P", integrated), self.base)
        self.repo.assert_revision(worker, candidate)

    def test_clean_check_includes_untracked_files(self):
        self.repo.assert_clean()
        (self.path / "untracked.txt").write_text("data")
        with self.assertRaises(WorkspaceError):
            self.repo.assert_clean()

    def test_empty_candidate_and_worker_commits_are_rejected(self):
        worker = self.worktree()
        with self.assertRaisesRegex(WorkspaceError, "empty candidate"):
            self.repo.commit_candidate(worker, self.base, "Empty")
        self.repo.git("commit", "--allow-empty", "-m", "Worker-owned commit", cwd=worker)
        with self.assertRaisesRegex(WorkspaceError, "worker moved HEAD"):
            self.repo.commit_candidate(worker, self.base, "Candidate")

    def test_exhausted_worktree_commits_a_single_commit_on_its_base(self):
        worker = self.worktree()
        (worker / "partial.txt").write_text("interrupted mid-turn\n", encoding="utf-8")
        revision = self.repo.commit_exhausted(worker, self.base, "Anvil: exhausted attempt")
        self.assertEqual(self.repo.git("show", "-s", "--format=%P", revision), self.base)
        self.assertEqual(self.repo.git("show", f"{revision}:partial.txt"), "interrupted mid-turn")
        # Unlike a candidate, an exhausted revision names no ticket claim and
        # is never checked out as a base for review or verification.
        self.assertNotEqual(revision, self.base)

    def test_exhausted_worktree_with_nothing_changed_records_no_revision(self):
        worker = self.worktree()
        self.assertIsNone(self.repo.commit_exhausted(worker, self.base, "Anvil: exhausted attempt"))
        self.assertEqual(self.repo.git("rev-parse", "HEAD", cwd=worker), self.base)

    def test_exhausted_commit_rejects_a_moved_head(self):
        worker = self.worktree()
        self.repo.git("commit", "--allow-empty", "-m", "Worker-owned commit", cwd=worker)
        with self.assertRaisesRegex(WorkspaceError, "worker moved HEAD"):
            self.repo.commit_exhausted(worker, self.base, "Anvil: exhausted attempt")

    def test_exhausted_revision_survives_a_prune_after_its_worktree_is_gone(self):
        """The case retention exists for: nothing but the ref reaches it once
        the worktree that held it is cleaned up."""
        worker = self.worktree()
        (worker / "partial.txt").write_text("interrupted mid-turn\n", encoding="utf-8")
        revision = self.repo.commit_exhausted(worker, self.base, "Anvil: exhausted attempt")
        self.repo.retain("exhausted", "run-1", "attempt-1", revision)
        self.repo.git("reset", "--hard", self.base, cwd=worker)
        self.repo.remove_worktree(worker)
        self.raw_git("gc", "--prune=now")
        self.assertEqual(self.repo.git("cat-file", "-t", revision), "commit")

    def test_an_amend_worktree_holds_a_candidate_tree_at_the_base(self):
        """The mechanism docs/AMEND_RETRIES.md section 3 prototyped.

        A replacement attempt cannot check the rejected candidate out, because
        commit_candidate requires HEAD to be the base and the result to be one
        commit on it. Restoring the tree keeps both properties.
        """
        (self.path / "gone.txt").write_text("doomed\n", encoding="utf-8")
        self.raw_git("add", ".")
        self.raw_git("-c", "commit.gpgSign=false", "commit", "-m", "Add a file to delete")
        base = self.repo.head()
        worker = self.root / "first"
        self.repo.create_worktree(worker, base)
        (worker / "tracked.txt").write_text("implemented\n", encoding="utf-8")
        (worker / "new.txt").write_text("acceptance evidence\n", encoding="utf-8")
        (worker / "gone.txt").unlink()
        candidate = self.repo.commit_candidate(worker, base, "Rejected candidate")

        amend = self.root / "amend"
        self.repo.create_amended_worktree(amend, base, candidate)
        self.assertEqual(self.repo.git("rev-parse", "HEAD", cwd=amend), base)
        self.assertEqual((amend / "tracked.txt").read_text(), "implemented\n")
        self.assertEqual((amend / "new.txt").read_text(), "acceptance evidence\n")
        self.assertFalse((amend / "gone.txt").exists())

        (amend / "amended.txt").write_text("the requested change\n", encoding="utf-8")
        amended = self.repo.commit_candidate(amend, base, "Amended candidate")
        self.assertEqual(self.repo.git("show", "-s", "--format=%P", amended), base)
        changes = dict(line.split("\t")[::-1] for line
                       in self.repo.git("diff", "--name-status", base, amended).splitlines())
        self.assertEqual(changes, {"gone.txt": "D", "tracked.txt": "M",
                                   "new.txt": "A", "amended.txt": "A"})

    def test_managed_commit_disables_signing_and_hooks(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 42\n")
        hook.chmod(0o755)
        self.raw_git("config", "core.hooksPath", str(hooks))
        self.raw_git("config", "commit.gpgSign", "true")
        self.raw_git("config", "gpg.program", "/does/not/exist")
        _, candidate = self.candidate()
        self.assertEqual(self.repo.git("show", "-s", "--format=%cn", candidate), "Anvil")

    def test_post_review_mutations_and_moved_head_are_rejected(self):
        worker, candidate = self.candidate()
        self.repo.assert_revision(worker, candidate)
        (worker / "new.txt").write_text("changed after review\n")
        with self.assertRaisesRegex(WorkspaceError, "uncommitted"):
            self.repo.assert_revision(worker, candidate)
        self.repo.git("reset", "--hard", self.base, cwd=worker)
        with self.assertRaisesRegex(WorkspaceError, "HEAD changed"):
            self.repo.assert_revision(worker, candidate)

    def test_branch_creation_never_overwrites_an_existing_branch(self):
        self.repo.create_branch("anvil/existing", self.base)
        _, candidate = self.candidate()
        with self.assertRaises(WorkspaceError):
            self.repo.create_branch("anvil/existing", candidate)
        self.assertEqual(self.repo.git("rev-parse", "anvil/existing"), self.base)
        with self.assertRaises(WorkspaceError):
            self.repo.create_branch("refs/heads/anvil/double-prefix", self.base)

    def test_compare_and_swap_rejects_unexpected_branch_movement(self):
        _, first = self.candidate("first")
        second = self.worktree("second")
        (second / "different.txt").write_text("a different candidate\n")
        second_revision = self.repo.commit_candidate(second, self.base, "Different ticket")
        self.repo.create_branch("anvil/concurrent", self.base)
        self.repo.git("update-ref", "refs/heads/anvil/concurrent", second_revision, self.base)
        with self.assertRaises(WorkspaceError):
            self.repo.advance_branch("anvil/concurrent", self.base, first)
        self.assertEqual(self.repo.git("rev-parse", "anvil/concurrent"), second_revision)

    def test_cannot_advance_a_branch_checked_out_in_any_worktree(self):
        _, candidate = self.candidate()
        with self.assertRaisesRegex(WorkspaceError, "checked out"):
            self.repo.advance_branch("main", self.base, candidate)
        self.repo.create_branch("anvil/in-use", self.base)
        other = self.root / "user workspace"
        self.raw_git("worktree", "add", str(other), "anvil/in-use")
        with self.assertRaisesRegex(WorkspaceError, "checked out"):
            self.repo.advance_branch("anvil/in-use", self.base, candidate)
        self.assertEqual(self.repo.head(), self.base)

    def test_integration_refuses_to_discard_unexpected_edits(self):
        _, candidate = self.candidate()
        integration = self.worktree("integration")
        (integration / "tracked.txt").write_text("unexpected changes\n")
        with self.assertRaisesRegex(WorkspaceError, "uncommitted"):
            self.repo.prepare_integration(integration, self.base, candidate)
        self.assertEqual((integration / "tracked.txt").read_text(), "unexpected changes\n")

    def test_integration_conflict_preserves_candidate_and_leaves_branch_unchanged(self):
        worker, candidate = self.candidate()
        competing = self.worktree("competing")
        (competing / "tracked.txt").write_text("incompatible implementation\n")
        competing_revision = self.repo.commit_candidate(competing, self.base, "Competing change")
        integration = self.worktree("integration")
        self.repo.create_branch("anvil/conflict", competing_revision)
        with self.assertRaises(WorkspaceError):
            self.repo.prepare_integration(integration, competing_revision, candidate)
        self.assertEqual(self.repo.git("rev-parse", "anvil/conflict"), competing_revision)
        self.repo.assert_revision(worker, candidate)
        self.assertIn("<<<<<<<", (integration / "tracked.txt").read_text())
        with self.assertRaisesRegex(WorkspaceError, "uncommitted"):
            self.repo.remove_worktree(integration)

    def test_verification_untracked_output_is_rejected(self):
        worker, candidate = self.candidate()
        (worker / "unexpected-result.txt").write_text("verification side effect\n")
        with self.assertRaisesRegex(WorkspaceError, "untracked"):
            self.repo.assert_revision(worker, candidate)

    def test_managed_operations_cannot_modify_user_checkouts_or_existing_paths(self):
        with self.assertRaisesRegex(WorkspaceError, "not owned"):
            self.repo.commit_candidate(self.path, self.base, "Must not commit")
        with self.assertRaisesRegex(WorkspaceError, "not owned"):
            self.repo.prepare_integration(self.path, self.base, self.base)
        with self.assertRaisesRegex(WorkspaceError, "not owned"):
            self.repo.remove_worktree(self.path)
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(WorkspaceError, "already exists"):
            self.repo.create_worktree(existing, self.base)

    def test_worker_checkout_of_a_branch_is_rejected(self):
        worker = self.worktree()
        self.raw_git("switch", "-c", "worker-created-branch", cwd=worker)
        with self.assertRaisesRegex(WorkspaceError, "detached HEAD"):
            self.repo.commit_candidate(worker, self.base, "Do not change worker branch")
        with self.assertRaisesRegex(WorkspaceError, "detached HEAD"):
            self.repo.remove_worktree(worker)
        self.assertTrue(worker.exists())

    def test_removing_a_managed_clean_worktree_preserves_main_checkout(self):
        worker, _ = self.candidate()
        self.repo.remove_worktree(worker)
        self.assertFalse(worker.exists())
        self.assertTrue(self.path.exists())
        self.assertEqual(self.repo.head(), self.base)

    def test_lock_coordinates_distinct_instances_and_linked_repositories(self):
        linked_path = self.worktree("linked")
        another_repo = Repository(self.path)
        linked_repo = Repository(linked_path)
        self.assertEqual(linked_repo.common_dir, self.repo.common_dir)
        with RepositoryLock(self.repo):
            for contender in (another_repo, linked_repo):
                with self.subTest(path=contender.path), self.assertRaisesRegex(WorkspaceError, "locked"):
                    with RepositoryLock(contender):
                        self.fail("overlapping lock acquired")
        with RepositoryLock(another_repo):
            pass

    def test_git_environment_cannot_redirect_operations_to_another_repository(self):
        with patch.dict(os.environ, {"GIT_DIR": "/does/not/exist", "GIT_WORK_TREE": "/tmp"}):
            self.assertEqual(Repository(self.path).head(), self.base)

    def test_git_runs_under_the_configured_credential_exclusion(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "KEEP": "yes"}):
            repo = Repository(self.path, exclude=("OPENAI_API_KEY",))
            with patch("anvil.workspaces.run_process") as launched:
                launched.return_value = ProcessOutcome(0, False)
                with self.assertRaises(WorkspaceError):
                    repo.git("status")
        environment = launched.call_args.kwargs["env"]
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(environment["KEEP"], "yes")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_git_inherits_everything_when_no_exclusion_is_configured(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
            repo = Repository(self.path)
            self.assertEqual(repo.exclude, ())
            with patch("anvil.workspaces.run_process") as launched:
                launched.return_value = ProcessOutcome(0, False)
                with self.assertRaises(WorkspaceError):
                    repo.git("status")
        self.assertIn("OPENAI_API_KEY", launched.call_args.kwargs["env"])

    def test_git_errors_and_timeouts_are_workspace_errors(self):
        with patch("anvil.workspaces.run_process", side_effect=ProcessError("creation failed")):
            with self.assertRaisesRegex(WorkspaceError, "cannot run Git"):
                self.repo.head()
        with patch("anvil.workspaces.run_process", return_value=ProcessOutcome(-9, True)):
            with self.assertRaisesRegex(WorkspaceError, "timed out after 120 seconds"):
                self.repo.head()
        with self.assertRaises(WorkspaceError):
            self.repo.git("rev-parse", "--verify", "a-ref-that-does-not-exist")
        with self.assertRaisesRegex(WorkspaceError, "cannot run Git"):
            self.repo.git("show", "bad\0revision")

    def git_filter_fixture(self):
        ready, release = self.root / "filter-ready", self.root / "filter-release"
        mutation = self.root / "late-filter-write"
        script = self.root / "filter child.py"
        script.write_text(
            "import pathlib,signal,sys,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "data=sys.stdin.buffer.read()\n"
            f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
            "deadline=time.monotonic()+3\n"
            f"while not pathlib.Path({str(release)!r}).exists() and time.monotonic()<deadline:\n"
            "    time.sleep(0.01)\n"
            f"pathlib.Path({str(mutation)!r}).write_text('write after Git returned')\n"
            "sys.stdout.buffer.write(data)\n",
            encoding="utf-8",
        )
        self.raw_git("config", "filter.anvil-lifecycle.clean", shlex.join([sys.executable, str(script)]))
        self.raw_git("config", "filter.anvil-lifecycle.required", "true")
        (self.path / ".gitattributes").write_text("tracked.txt filter=anvil-lifecycle\n")
        (self.path / "tracked.txt").write_text("requires filtering\n")
        return ready, release, mutation

    def assert_git_filter_stopped(self, *, interrupt):
        ready, release, mutation = self.git_filter_fixture()
        original_wait = subprocess.Popen.wait
        supervised = False

        def wait_for_filter(process, *args, **kwargs):
            nonlocal supervised
            if not supervised:
                supervised = True
                # Synchronize with the real filter before shortening the wait,
                # avoiding assumptions about Git/Python startup speed on CI.
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "Git did not start its filter")
                if interrupt:
                    raise KeyboardInterrupt
            return original_wait(process, *args, **kwargs)

        with patch("anvil.workspaces._GIT_TIMEOUT", 0.1), patch.object(subprocess.Popen, "wait", wait_for_filter):
            with self.assertRaises(KeyboardInterrupt if interrupt else WorkspaceError):
                self.repo.git("add", "--", "tracked.txt")
        self.assertFalse(mutation.exists(), "filter wrote before the supervisor finished cleanup")
        release.write_text("run now if still alive")
        time.sleep(0.35)
        self.assertFalse(mutation.exists(), "Git filter survived process cleanup and wrote afterward")

    def test_git_timeout_terminates_filter_descendants_before_returning(self):
        self.assert_git_filter_stopped(interrupt=False)

    def test_git_interrupt_terminates_filter_descendants_before_propagating(self):
        self.assert_git_filter_stopped(interrupt=True)


if __name__ == "__main__":
    unittest.main()


class RepositoryOwnership(unittest.TestCase):
    """AGENTS.md: the supervisor owns Git entirely; worker turns never commit.

    Nothing in the library enforces this the way sqlite enforces its connection
    affinity, so a worker thread reaching a Repository write would simply have
    worked. This is the half of the claim that needed code rather than review.
    """

    # Borrow the real-repository fixture without inheriting its tests, which
    # subclassing would re-run in full.
    raw_git = WorkspaceTests.raw_git
    setUp = WorkspaceTests.setUp

    @staticmethod
    def elsewhere(call):
        outcome = {}

        def run():
            try:
                outcome["value"] = call()
            except BaseException as exc:  # noqa: BLE001 - reported, not handled
                outcome["error"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        return outcome

    def test_a_worker_thread_cannot_write_git(self):
        repo = Repository(self.path)
        base = repo.head()
        outcome = self.elsewhere(
            lambda: repo.create_worktree(self.root / "elsewhere", base))
        self.assertIsInstance(outcome.get("error"), WorkspaceError)
        self.assertIn("only the owning thread writes Git", str(outcome["error"]))
        self.assertFalse((self.root / "elsewhere").exists())

    def test_reads_stay_available_to_any_thread(self):
        """Only writes are owned: a thread may still look."""
        repo = Repository(self.path)
        outcome = self.elsewhere(repo.head)
        self.assertEqual(outcome.get("value"), repo.head(), outcome.get("error"))

    def test_adopt_moves_ownership_and_the_previous_owner_then_fails(self):
        repo = Repository(self.path)
        base = repo.head()
        self.elsewhere(repo.adopt)
        with self.assertRaisesRegex(WorkspaceError, "only the owning thread writes Git"):
            repo.create_worktree(self.root / "after", base)
        outcome = self.elsewhere(
            lambda: repo.create_worktree(self.root / "after", base))
        self.assertIsNone(outcome.get("error"), outcome.get("error"))
        self.assertTrue((self.root / "after").exists())
        self.assertEqual(len(repo._adoptions), 1)

    def test_every_write_method_is_owned(self):
        """The guard covers the write API, not one convenient member of it."""
        import inspect
        owned = {name for name, member in inspect.getmembers(Repository, inspect.isfunction)
                 if 'self._assert_owner("' in inspect.getsource(member)}
        self.assertEqual(owned, {"create_worktree", "create_amended_worktree", "create_branch",
                                 "commit_candidate", "commit_exhausted", "prepare_integration", "retain",
                                 "advance_branch", "remove_worktree"})
