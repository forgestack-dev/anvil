"""Isolated installation ownership and updates; never touch real agent folders."""

import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from anvil.skill_management import SkillError, SkillScope, install, scope_for, status, update
from anvil.skill_source import Catalog, Skill, SourceFile


class SkillManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="anvil skill management ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.scope = SkillScope(self.project)
        fetch_patch = patch("anvil.skill_management.fetch_catalog")
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        self.first = self.catalog("a")
        self.fetch.return_value = self.first
        self.fetch.side_effect = self.filtered_catalog

    def filtered_catalog(self, *, ref, names, include_experimental):
        """The source boundary returns the requested selection, not the archive."""
        available = self.fetch.return_value
        selected = {
            name: skill for name, skill in available.skills.items()
            if (name in names if names else
                (include_experimental or not skill.source_path.startswith("skills/in-progress/")))
        }
        return Catalog(revision=available.revision, skills=selected)

    def skill(self, name, *, version="one", experimental=False):
        category = "in-progress" if experimental else "engineering"
        return Skill(
            source_path=f"skills/{category}/{name}",
            files={
                "SKILL.md": SourceFile(f"---\nname: {name}\ndescription: Fixture {version}\n---\nRead references/detail.md.\n".encode()),
                "references/detail.md": SourceFile(f"{name} details {version}\n".encode()),
                "scripts/check": SourceFile(b"#!/bin/sh\nexit 0\n", executable=True),
                "assets/data.bin": SourceFile(b"\x00\xff\x10fixture"),
                "LICENSE.aihero": SourceFile(b"Fixture upstream license notice\n"),
            },
        )

    def catalog(self, revision, *, names=("tdd", "write-a-prd"), version="one", experimental=True):
        skills = {name: self.skill(name, version=version) for name in names}
        if experimental:
            skills["research"] = self.skill("research", version=version, experimental=True)
        return Catalog(revision=revision * 40, skills=skills)

    def target(self, agent, name, *, scope=None):
        selected = scope or self.scope
        directory = ".agents" if agent == "codex" else ".claude"
        return selected.root / directory / "skills" / name

    def assert_skill(self, agent, name, source, *, scope=None):
        directory = self.target(agent, name, scope=scope)
        self.assertTrue(directory.is_dir())
        self.assertFalse(directory.is_symlink())
        actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        self.assertEqual(actual, set(source.files))
        for relative, expected in source.files.items():
            path = directory / relative
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), expected.content)
            self.assertEqual(bool(path.stat().st_mode & 0o111), expected.executable)

    def snapshot(self, root):
        if not root.exists() and not root.is_symlink():
            return None
        result = {}
        for path in (root, *sorted(root.rglob("*"))):
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path), mode)
            elif path.is_dir():
                result[relative] = ("directory", mode)
            else:
                result[relative] = ("file", path.read_bytes(), mode)
        return result

    def owned_snapshot(self):
        return {
            "codex": self.snapshot(self.project / ".agents"),
            "claude": self.snapshot(self.project / ".claude"),
            "manifest": self.scope.manifest.read_bytes() if self.scope.manifest.exists() else None,
        }

    def test_install_copies_complete_stable_skills_to_both_agents_and_records_one_revision(self):
        result = install(self.scope)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["revision"], self.first.revision)
        self.assertEqual(set(result["skills"]), {"tdd", "write-a-prd"})
        self.assertEqual(self.fetch.call_count, 1)
        self.fetch.assert_called_once_with(ref="main", names=(), include_experimental=False)
        for agent in ("codex", "claude-code"):
            for name in ("tdd", "write-a-prd"):
                self.assert_skill(agent, name, self.first.skills[name])
            self.assertFalse(self.target(agent, "research").exists())
        manifest = json.loads(self.scope.manifest.read_text())
        self.assertEqual(manifest["revision"], self.first.revision)
        self.assertEqual(set(manifest["agents"]), {"codex", "claude-code"})
        self.assertEqual(manifest["selection"], {"names": [], "include_experimental": False})

    def test_update_changes_both_targets_adds_new_skills_and_removes_unmodified_owned_skills(self):
        install(self.scope)
        unrelated = self.target("claude-code", "personal-skill")
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text("personal instructions")
        second = self.catalog("b", names=("tdd", "debugging"), version="two")
        second.skills["tdd"].files.pop("references/detail.md")
        second.skills["tdd"].files["references/new.md"] = SourceFile(b"new supporting instructions")
        self.fetch.return_value = second
        result = update(self.scope)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["revision"], second.revision)
        self.assertEqual(set(result["changes"]["added"]), {"debugging"})
        self.assertEqual(set(result["changes"]["removed"]), {"write-a-prd"})
        for agent in ("codex", "claude-code"):
            for name in ("tdd", "debugging"):
                self.assert_skill(agent, name, second.skills[name])
            self.assertFalse(self.target(agent, "write-a-prd").exists())
        self.assertEqual((unrelated / "SKILL.md").read_text(), "personal instructions")

    def test_conflicting_second_target_leaves_first_target_and_manifest_untouched(self):
        conflicting = self.target("claude-code", "tdd")
        conflicting.mkdir(parents=True)
        (conflicting / "SKILL.md").write_text("existing unrelated skill")
        before = self.owned_snapshot()
        with self.assertRaises(SkillError):
            install(self.scope)
        self.assertEqual(self.owned_snapshot(), before)

    def test_update_rejects_local_changes_to_either_copy_without_partial_update(self):
        install(self.scope)
        local = self.target("claude-code", "tdd") / "references/detail.md"
        local.write_text("local changes that must survive")
        before = self.owned_snapshot()
        self.fetch.return_value = self.catalog("b", version="two")
        with self.assertRaises(SkillError):
            update(self.scope)
        self.assertEqual(self.owned_snapshot(), before)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires enforced directory permissions")
    def test_unreadable_local_directory_blocks_update_before_any_change(self):
        install(self.scope)
        local = self.target("codex", "tdd") / "local-notes"
        local.mkdir()
        (local / "draft.txt").write_text("local work must stay in place")
        before = self.owned_snapshot()
        original_mode = stat.S_IMODE(local.stat().st_mode)
        local.chmod(0)
        try:
            try:
                list(local.iterdir())
            except PermissionError:
                pass
            else:
                self.skipTest("filesystem does not enforce the fixture's directory permissions")
            self.fetch.reset_mock()
            self.fetch.return_value = self.catalog("b", version="two")
            report = status(self.scope)
            self.assertEqual(report["status"], "modified")
            self.assertTrue(report["conflicts"])
            with self.assertRaises(SkillError):
                update(self.scope)
            self.fetch.assert_not_called()
            self.assertTrue(local.exists())
        finally:
            local.chmod(original_mode)
        self.assertEqual(self.owned_snapshot(), before)

    def test_update_protects_modified_skill_even_when_it_was_removed_upstream(self):
        install(self.scope)
        (self.target("codex", "write-a-prd") / "SKILL.md").write_text("edited locally")
        before = self.owned_snapshot()
        self.fetch.return_value = self.catalog("b", names=("tdd",), version="two")
        with self.assertRaises(SkillError):
            update(self.scope)
        self.assertEqual(self.owned_snapshot(), before)

    def test_idempotent_install_and_update_do_not_rewrite_existing_evidence(self):
        install(self.scope)
        before = self.owned_snapshot()
        paths = [self.scope.manifest, self.target("codex", "tdd") / "SKILL.md"]
        timestamps = [path.stat().st_mtime_ns for path in paths]
        self.assertEqual(install(self.scope)["status"], "unchanged")
        self.assertEqual(update(self.scope)["status"], "unchanged")
        self.assertEqual(self.owned_snapshot(), before)
        self.assertEqual([path.stat().st_mtime_ns for path in paths], timestamps)

    def test_install_with_changed_source_requires_update(self):
        install(self.scope)
        before = self.owned_snapshot()
        self.fetch.return_value = self.catalog("b", version="two")
        with self.assertRaisesRegex(SkillError, "update"):
            install(self.scope)
        self.assertEqual(self.owned_snapshot(), before)

    def test_update_retains_selected_agent_and_explicit_skill_subset(self):
        install(self.scope, agents=("claude-code",), names=("tdd",))
        second = self.catalog("b", names=("tdd", "write-a-prd", "debugging"), version="two")
        self.fetch.return_value = second
        result = update(self.scope)
        self.assertEqual(set(result["agents"]), {"claude-code"})
        self.assertEqual(result["skills"], ["tdd"])
        self.assert_skill("claude-code", "tdd", second.skills["tdd"])
        self.assertFalse((self.project / ".agents").exists())
        self.assertFalse(self.target("claude-code", "debugging").exists())
        self.assertEqual(json.loads(self.scope.manifest.read_text())["selection"]["names"], ["tdd"])
        self.fetch.assert_called_with(ref="main", names=("tdd",), include_experimental=False)

    def test_update_retains_experimental_opt_in(self):
        install(self.scope, include_experimental=True)
        second = self.catalog("b", version="two")
        self.fetch.return_value = second
        result = update(self.scope)
        self.assertIn("research", result["skills"])
        for agent in ("codex", "claude-code"):
            self.assert_skill(agent, "research", second.skills["research"])
        self.assertTrue(json.loads(self.scope.manifest.read_text())["selection"]["include_experimental"])
        self.fetch.assert_called_with(ref="main", names=(), include_experimental=True)

    def test_status_is_read_only_offline_and_reports_local_content_and_mode_changes(self):
        before = self.snapshot(self.project)
        self.assertEqual(status(self.scope)["status"], "not-installed")
        self.assertEqual(self.snapshot(self.project), before)
        self.fetch.assert_not_called()
        install(self.scope)
        self.fetch.reset_mock()
        before = self.snapshot(self.project)
        self.assertNotEqual(status(self.scope)["status"], "modified")
        self.assertEqual(self.snapshot(self.project), before)
        source = self.target("codex", "tdd") / "SKILL.md"
        source.write_text("local content")
        script = self.target("claude-code", "write-a-prd") / "scripts/check"
        script.chmod(script.stat().st_mode & ~0o111)
        before = self.snapshot(self.project)
        result = status(self.scope)
        self.assertEqual(result["status"], "modified")
        self.assertTrue(result["conflicts"])
        self.assertEqual(self.snapshot(self.project), before)
        self.fetch.assert_not_called()

    def test_extra_or_missing_files_are_local_changes(self):
        install(self.scope)
        extra = self.target("codex", "tdd") / "local-note.md"
        extra.write_text("private note")
        self.assertEqual(status(self.scope)["status"], "modified")
        extra.unlink()
        (self.target("claude-code", "tdd") / "assets/data.bin").unlink()
        self.assertEqual(status(self.scope)["status"], "modified")
        before = self.owned_snapshot()
        with self.assertRaises(SkillError):
            update(self.scope)
        self.assertEqual(self.owned_snapshot(), before)

    def test_install_dry_run_fetches_preview_without_creating_any_files(self):
        before = self.snapshot(self.project)
        result = install(self.scope, dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(set(result["changes"]["added"]), {"tdd", "write-a-prd"})
        self.assertEqual(self.snapshot(self.project), before)
        self.fetch.assert_called_once()

    def test_update_dry_run_retains_current_revision_and_targets(self):
        install(self.scope)
        before = self.snapshot(self.project)
        self.fetch.return_value = self.catalog("b", names=("tdd", "debugging"), version="two")
        result = update(self.scope, dry_run=True)
        self.assertEqual(result["status"], "dry-run")
        self.assertEqual(set(result["changes"]["removed"]), {"write-a-prd"})
        self.assertEqual(self.snapshot(self.project), before)

    def test_dangling_skill_symlink_is_a_conflict_even_for_preview(self):
        destination = self.target("claude-code", "tdd")
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.root / "missing")
        before = self.owned_snapshot()
        with self.assertRaises(SkillError):
            install(self.scope, dry_run=True)
        self.assertEqual(self.owned_snapshot(), before)

    def test_symlinked_agent_parent_never_writes_through_to_another_directory(self):
        external = self.root / "external"
        external.mkdir()
        (external / "precious").write_text("preserve")
        (self.project / ".claude").symlink_to(external, target_is_directory=True)
        before = self.snapshot(external)
        with self.assertRaises(SkillError):
            install(self.scope)
        self.assertEqual(self.snapshot(external), before)
        self.assertFalse((self.project / ".agents").exists())
        self.assertFalse(self.scope.manifest.exists())

    def test_unsafe_manifest_file_path_is_rejected_before_updating(self):
        install(self.scope)
        manifest = json.loads(self.scope.manifest.read_text())
        files = manifest["skills"]["tdd"]["files"]
        files["../../outside"] = next(iter(files.values())).copy()
        self.scope.manifest.write_text(json.dumps(manifest))
        before = self.owned_snapshot()
        self.fetch.return_value = self.catalog("b", version="two")
        with self.assertRaises(SkillError):
            update(self.scope)
        self.assertEqual(self.owned_snapshot(), before)

    def test_failure_replacing_second_agent_rolls_back_both_targets_and_manifest(self):
        install(self.scope)
        before = self.owned_snapshot()
        self.fetch.return_value = self.catalog("b", names=("tdd", "debugging"), version="two")
        original_rename = Path.rename
        failed = False

        def fail_second_target(path, destination):
            nonlocal failed
            if not failed and Path(destination) == self.target("claude-code", "tdd"):
                failed = True
                self.assertIn(b"two", (self.target("codex", "tdd") / "SKILL.md").read_bytes())
                raise OSError("injected second-agent placement failure")
            return original_rename(path, destination)

        with patch("anvil.skill_management.Path.rename", fail_second_target):
            with self.assertRaisesRegex((OSError, SkillError), "injected second-agent"):
                update(self.scope)
        self.assertTrue(failed)
        self.assertEqual(self.owned_snapshot(), before)

    def test_manifest_replace_failure_rolls_back_all_skill_additions_changes_and_removals(self):
        install(self.scope)
        before = self.owned_snapshot()
        self.fetch.return_value = self.catalog("b", names=("tdd", "debugging"), version="two")
        original_replace = Path.replace
        failed = False

        def fail_manifest(path, destination):
            nonlocal failed
            if Path(destination) == self.scope.manifest:
                failed = True
                for agent in ("codex", "claude-code"):
                    self.assertIn(b"two", (self.target(agent, "tdd") / "SKILL.md").read_bytes())
                    self.assertTrue(self.target(agent, "debugging").is_dir())
                    self.assertFalse(self.target(agent, "write-a-prd").exists())
                raise OSError("injected manifest replacement failure")
            return original_replace(path, destination)

        with patch("anvil.skill_management.Path.replace", fail_manifest):
            with self.assertRaisesRegex((OSError, SkillError), "injected manifest"):
                update(self.scope)
        self.assertTrue(failed)
        self.assertEqual(self.owned_snapshot(), before)

    def test_failed_initial_install_cannot_leave_one_agent_installed(self):
        original_replace = Path.replace

        def fail_manifest(path, destination):
            if Path(destination) == self.scope.manifest:
                raise OSError("injected initial manifest replacement failure")
            return original_replace(path, destination)

        with patch("anvil.skill_management.Path.replace", fail_manifest):
            with self.assertRaisesRegex((OSError, SkillError), "injected initial"):
                install(self.scope)
        self.assertFalse(self.scope.manifest.exists())
        for agent in ("codex", "claude-code"):
            for name in ("tdd", "write-a-prd"):
                self.assertFalse(self.target(agent, name).exists())
        self.assertEqual(list(self.project.rglob(".anvil-aihero-*")), [])

    def test_failed_rollback_preserves_recoverable_backups_instead_of_deleting_them(self):
        install(self.scope)
        manifest = self.scope.manifest.read_bytes()
        first_skill = (self.target("codex", "tdd") / "SKILL.md").read_bytes()
        self.fetch.return_value = self.catalog("b", version="two")
        original_replace = Path.replace
        original_rename = Path.rename

        def fail_manifest(path, destination):
            if Path(destination) == self.scope.manifest:
                raise OSError("injected manifest failure")
            return original_replace(path, destination)

        def fail_restore(path, destination):
            if path.parent.name == "old" and Path(destination) == self.target("codex", "tdd"):
                raise OSError("injected restore failure")
            return original_rename(path, destination)

        with patch("anvil.skill_management.Path.replace", fail_manifest), patch("anvil.skill_management.Path.rename", fail_restore):
            with self.assertRaisesRegex(SkillError, "preserved backups"):
                update(self.scope)
        backups = list(self.project.rglob(".anvil-aihero-*/old/tdd/SKILL.md"))
        self.assertTrue(backups)
        self.assertTrue(all(path.read_bytes() == first_skill for path in backups))
        self.assertEqual(self.scope.manifest.read_bytes(), manifest)

    @unittest.skipUnless(os.name == "posix", "POSIX interruption and directory locks")
    def test_sigint_after_first_target_placement_leaves_both_agents_at_one_complete_revision(self):
        install(self.scope)
        second = self.catalog("b", version="two")
        self.fetch.return_value = second
        original_rename = Path.rename
        previous = signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        interrupted = False

        def interrupt_after_placement(path, destination):
            nonlocal interrupted
            result = original_rename(path, destination)
            if not interrupted and Path(destination) == self.target("codex", "tdd"):
                interrupted = True
                os.kill(os.getpid(), signal.SIGINT)
                os.kill(os.getpid(), signal.SIGINT)
            return result

        with patch("anvil.skill_management.Path.rename", interrupt_after_placement):
            with self.assertRaises(KeyboardInterrupt):
                update(self.scope)
        self.assertTrue(interrupted)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertEqual(json.loads(self.scope.manifest.read_text())["revision"], second.revision)
        for agent in ("codex", "claude-code"):
            for name in ("tdd", "write-a-prd"):
                self.assert_skill(agent, name, second.skills[name])
        self.assertEqual(status(self.scope)["status"], "installed")
        self.assertEqual(list(self.project.rglob(".anvil-aihero-*")), [])

    @unittest.skipUnless(os.name == "posix", "POSIX directory locks")
    def test_competing_operation_is_rejected_without_network_or_changes(self):
        import fcntl

        self.scope.manifest.parent.mkdir(parents=True)
        descriptor = os.open(self.scope.manifest.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.snapshot(self.project)
            with self.assertRaisesRegex(SkillError, "another skill operation"):
                install(self.scope)
            with self.assertRaisesRegex(SkillError, "another skill operation"):
                status(self.scope)
            self.assertEqual(self.snapshot(self.project), before)
            self.fetch.assert_not_called()
        finally:
            os.close(descriptor)

    def test_project_and_global_manifests_use_separate_explicit_scopes(self):
        simulated_home = self.root / "home"
        simulated_home.mkdir()
        global_scope = SkillScope(simulated_home, global_scope=True)
        self.assertEqual(self.scope.manifest, self.project / ".anvil" / "aihero.json")
        self.assertEqual(global_scope.manifest, simulated_home / ".local/state/anvil/skills/aihero.json")
        install(global_scope, agents=("codex",), names=("tdd",))
        self.assert_skill("codex", "tdd", self.first.skills["tdd"], scope=global_scope)
        self.assertFalse(self.scope.manifest.exists())
        with patch("anvil.skill_management.Path.home", return_value=simulated_home):
            located = scope_for(global_scope=True)
        self.assertEqual(located.root, simulated_home)
        self.assertTrue(located.global_scope)

    @unittest.skipUnless(shutil.which("git"), "Git project scope discovery")
    def test_project_scope_finds_repository_root_from_nested_directory(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True, capture_output=True, timeout=5)
        nested = self.project / "src" / "package"
        nested.mkdir(parents=True)
        located = scope_for(repo=nested)
        self.assertEqual(located.root, self.project)
        self.assertFalse(located.global_scope)


if __name__ == "__main__":
    unittest.main()
