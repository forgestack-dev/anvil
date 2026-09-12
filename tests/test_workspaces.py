"""Real Git coverage for isolation, candidate ownership, and branch publication."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from anvil.workspaces import Repository, RepositoryLock, WorkspaceError


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

    def test_git_errors_and_timeouts_are_workspace_errors(self):
        with patch("anvil.workspaces.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 120)):
            with self.assertRaisesRegex(WorkspaceError, "cannot run Git"):
                self.repo.head()
        with self.assertRaises(WorkspaceError):
            self.repo.git("rev-parse", "--verify", "a-ref-that-does-not-exist")
        with self.assertRaisesRegex(WorkspaceError, "cannot run Git"):
            self.repo.git("show", "bad\0revision")


if __name__ == "__main__":
    unittest.main()
