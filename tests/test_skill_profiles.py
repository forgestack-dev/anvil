"""Native Claude profiles use isolated fixtures, never real personal skills."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from anvil.cli import main
from anvil.skill_management import SkillError, SkillScope, install, scope_for, status, update
from anvil.skill_source import Catalog, Skill, SourceFile


class SkillProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="anvil skill profiles ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.profile = self.root / "external profile" / "claude"
        self.manifest = self.home / ".local/state/anvil/skills/aihero.json"
        self.codex = self.home / ".agents/skills"
        self.claude = self.profile / "skills"
        home_patch = patch("anvil.skill_management.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.profile)})
        environment.start()
        self.addCleanup(environment.stop)
        fetch_patch = patch("anvil.skill_management.fetch_catalog", return_value=self.catalog("a"))
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

    @staticmethod
    def catalog(revision):
        return Catalog(revision=revision * 40, skills={"tdd": Skill(
            source_path="skills/engineering/tdd",
            files={
                "SKILL.md": SourceFile(f"---\nname: tdd\ndescription: Fixture {revision}\n---\n".encode()),
                "references/details.md": SourceFile(f"Supporting instructions {revision}\n".encode()),
                "scripts/check": SourceFile(b"#!/bin/sh\nexit 0\n", executable=True),
                "LICENSE.aihero": SourceFile(b"Fixture license\n"),
            },
        )})

    def invoke(self, action, *arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["skills", action, "aihero", "--global", "--json", *arguments])
        self.assertEqual(stderr.getvalue(), "")
        return code, json.loads(stdout.getvalue())

    def snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                value = ("symlink", os.readlink(path), mode)
            elif path.is_dir():
                value = ("directory", mode)
            else:
                value = ("file", path.read_bytes(), mode)
            result[path.relative_to(self.root).as_posix()] = value
        return result

    def assert_copy(self, directory, revision):
        expected = self.catalog(revision).skills["tdd"].files
        actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        self.assertEqual(actual, set(expected))
        for name, source in expected.items():
            self.assertEqual((directory / name).read_bytes(), source.content)
            self.assertEqual(bool((directory / name).stat().st_mode & 0o111), source.executable)

    def test_cli_installs_updates_and_reports_the_external_profile(self):
        code, report = self.invoke("install")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "installed")
        expected_agents = {"codex": str(self.codex), "claude-code": str(self.claude)}
        self.assertEqual(report["agents"], expected_agents)
        self.assertFalse((self.home / ".claude").exists())
        self.assert_copy(self.codex / "tdd", "a")
        self.assert_copy(self.claude / "tdd", "a")
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["claude_config_dir"], str(self.profile))

        self.fetch.return_value = self.catalog("b")
        code, report = self.invoke("update")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "updated")
        self.assertEqual(report["agents"], expected_agents)
        self.assert_copy(self.codex / "tdd", "b")
        self.assert_copy(self.claude / "tdd", "b")
        self.fetch.reset_mock()
        before = self.snapshot()
        code, report = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "installed")
        self.assertEqual(report["agents"], expected_agents)
        self.fetch.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_cli_custom_profile_previews_leave_all_files_unchanged(self):
        before = self.snapshot()
        code, report = self.invoke("install", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "dry-run")
        self.assertEqual(report["agents"]["claude-code"], str(self.claude))
        self.assertEqual(self.snapshot(), before)
        self.invoke("install")
        self.fetch.return_value = self.catalog("b")
        before = self.snapshot()
        code, report = self.invoke("update", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "dry-run")
        self.assertEqual(self.snapshot(), before)

    def test_cli_changed_or_unset_profile_cannot_reinterpret_managed_copies(self):
        self.invoke("install")
        before = self.snapshot()
        for replacement in (str(self.root / "other profile"), None, ""):
            with self.subTest(profile=replacement):
                if replacement is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = replacement
                for action, flags in (("status", ()), ("install", ()), ("update", ()),
                                      ("update", ("--dry-run",))):
                    with self.subTest(action=action, flags=flags):
                        self.fetch.reset_mock()
                        code, report = self.invoke(action, *flags)
                        self.assertEqual(code, 2)
                        self.assertIn("CLAUDE_CONFIG_DIR", report["error"])
                        self.fetch.assert_not_called()
                        self.assertEqual(self.snapshot(), before)

    def test_scope_captures_profile_once_for_an_entire_operation(self):
        selected = scope_for(global_scope=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "other profile")
        install(selected)
        self.assert_copy(self.claude / "tdd", "a")
        self.assertFalse((self.root / "other profile").exists())
        self.assertEqual(status(selected)["agents"]["claude-code"], str(self.claude))

    def test_tampered_manifest_destination_cannot_redirect_operations(self):
        self.invoke("install")
        manifest = json.loads(self.manifest.read_text())
        for configured in (str(self.root / "unowned profile"), "relative/profile", None):
            with self.subTest(configured=configured):
                self.manifest.write_text(json.dumps(manifest | {"claude_config_dir": configured}))
                before = self.snapshot()
                self.fetch.reset_mock()
                for action in ("status", "install", "update"):
                    code, report = self.invoke(action)
                    self.assertEqual(code, 2)
                    self.assertIn("error", report)
                self.fetch.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.root / "unowned profile").exists())

    def test_legacy_default_manifest_remains_unchanged_and_rejects_a_new_profile(self):
        os.environ.pop("CLAUDE_CONFIG_DIR")
        self.invoke("install")
        self.assertEqual(json.loads(self.manifest.read_text())["version"], 1)
        before = self.snapshot()
        for setting in (None, "", str(self.home / ".claude")):
            with self.subTest(setting=setting):
                if setting is None:
                    os.environ.pop("CLAUDE_CONFIG_DIR", None)
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = setting
                for action in ("install", "update", "status"):
                    code, report = self.invoke(action)
                    self.assertEqual(code, 0)
                    self.assertEqual(report["status"], "installed" if action == "status" else "unchanged")
                self.assertEqual(self.snapshot(), before)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.profile)
        self.fetch.reset_mock()
        for action in ("install", "update", "status"):
            code, report = self.invoke(action)
            self.assertEqual(code, 2)
            self.assertIn("CLAUDE_CONFIG_DIR", report["error"])
        self.fetch.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_codex_only_install_and_update_ignore_profile_changes(self):
        code, report = self.invoke("install", "--agent", "codex")
        self.assertEqual(code, 0)
        self.assertEqual(report["agents"], {"codex": str(self.codex)})
        self.assertEqual(json.loads(self.manifest.read_text())["version"], 1)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.root / "other profile")
        self.fetch.return_value = self.catalog("b")
        code, report = self.invoke("update")
        self.assertEqual(code, 0)
        self.assert_copy(self.codex / "tdd", "b")
        self.assertFalse(self.profile.exists())
        self.assertFalse((self.root / "other profile").exists())
        self.assertEqual(self.invoke("status")[0], 0)

    @unittest.skipUnless(shutil.which("git"), "Git project scope discovery")
    def test_repository_scope_ignores_personal_profile_environment(self):
        project = self.root / "project"
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True, timeout=5)
        selected = scope_for(repo=project)
        report = install(selected)
        self.assertEqual(report["agents"], {"codex": str(project / ".agents/skills"),
                                           "claude-code": str(project / ".claude/skills")})
        self.assert_copy(project / ".claude/skills/tdd", "a")
        self.assertFalse(self.profile.exists())
        self.assertEqual(json.loads((project / ".anvil/aihero.json").read_text())["version"], 1)

    def test_direct_global_scope_does_not_implicitly_read_the_environment(self):
        report = install(SkillScope(self.home, global_scope=True), agents=("claude-code",))
        self.assertEqual(report["agents"], {"claude-code": str(self.home / ".claude/skills")})
        self.assertFalse(self.profile.exists())

    def test_external_config_symlink_or_file_is_rejected_without_installing_skills(self):
        for kind in ("directory-link", "dangling-link", "file"):
            with self.subTest(kind=kind):
                config = self.root / kind
                if kind == "file":
                    config.write_text("preserve configuration file")
                else:
                    destination = self.root / (kind + "-destination")
                    if kind == "directory-link":
                        destination.mkdir()
                    config.symlink_to(destination, target_is_directory=True)
                os.environ["CLAUDE_CONFIG_DIR"] = str(config)
                code, report = self.invoke("install")
                self.assertEqual(code, 2)
                self.assertIn("error", report)
                self.assertFalse((self.codex / "tdd").exists())
                self.assertFalse(self.manifest.exists())
                if kind == "file":
                    self.assertEqual(config.read_text(), "preserve configuration file")
                else:
                    self.assertTrue(config.is_symlink())
                    self.assertFalse((destination / "skills").exists())

    def test_overlapping_agent_or_metadata_directories_are_rejected(self):
        for config in (self.home / ".agents", self.home / ".agents/skills/tdd",
                       self.home / ".local/state/anvil"):
            with self.subTest(config=config):
                os.environ["CLAUDE_CONFIG_DIR"] = str(config)
                code, report = self.invoke("install")
                self.assertEqual(code, 2)
                self.assertIn("error", report)
                self.assertFalse((self.codex / "tdd").exists())
                self.assertFalse(self.manifest.exists())

    def test_unmanaged_external_skill_prevents_installation_into_codex(self):
        existing = self.claude / "tdd"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("personal instructions")
        code, report = self.invoke("install")
        self.assertEqual(code, 2)
        self.assertIn("unmanaged", report["error"])
        self.assertEqual((existing / "SKILL.md").read_text(), "personal instructions")
        self.assertFalse((self.codex / "tdd").exists())
        self.assertFalse(self.manifest.exists())

    def test_external_local_edits_block_update_before_download(self):
        self.invoke("install")
        (self.claude / "tdd/references/details.md").write_text("local instructions")
        before = self.snapshot()
        self.fetch.reset_mock()
        code, report = self.invoke("update")
        self.assertEqual(code, 2)
        self.assertIn("local", report["error"])
        self.fetch.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_external_second_target_failure_rolls_back_both_copies_and_manifest(self):
        selected = scope_for(global_scope=True)
        install(selected)
        before = self.snapshot()
        self.fetch.return_value = self.catalog("b")
        original_rename = Path.rename

        def fail_external_placement(path, destination):
            if path.parent.name == "new" and Path(destination) == self.claude / "tdd":
                raise OSError("injected external target placement failure")
            return original_rename(path, destination)

        with patch("anvil.skill_management.Path.rename", fail_external_placement):
            with self.assertRaisesRegex(OSError, "injected external"):
                update(selected)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(status(selected)["status"], "installed")


if __name__ == "__main__":
    unittest.main()
