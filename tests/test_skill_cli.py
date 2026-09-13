"""Skill-management command routing without downloads or model invocations."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from anvil.cli import main, parser


class SkillCliTests(unittest.TestCase):
    def setUp(self):
        self.scope = object()
        self.report = {
            "source": "aihero", "status": "installed", "revision": "a" * 40,
            "agents": {"codex": "/project/.agents/skills", "claude-code": "/project/.claude/skills"},
            "skills": ["tdd", "write-a-prd"], "manifest": "/project/.anvil/skills/aihero.json",
            "changes": {"added": ["tdd", "write-a-prd"], "updated": [], "removed": []},
            "conflicts": [],
        }
        self.management = SimpleNamespace(
            scope_for=Mock(return_value=self.scope), install=Mock(return_value=self.report),
            update=Mock(return_value=self.report), status=Mock(return_value=self.report),
        )
        # Keep this boundary test independent of filesystem/network implementations.
        module = patch.dict(sys.modules, {"anvil.skill_management": self.management})
        module.start()
        self.addCleanup(module.stop)
        attribute = patch("anvil.skill_management", self.management, create=True)
        attribute.start()
        self.addCleanup(attribute.stop)
        for target in ("anvil.cli.probe_agent", "anvil.execution.run", "anvil.adapters.create_runner"):
            forbidden = patch(target, side_effect=AssertionError("skill management cannot invoke agents"))
            forbidden.start()
            self.addCleanup(forbidden.stop)

    def invoke(self, arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_install_selects_both_agents_in_current_repository_scope(self):
        code, stdout, stderr = self.invoke(["skills", "install", "aihero", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), self.report)
        self.assertEqual(stderr, "")
        self.management.scope_for.assert_called_once_with(None, global_scope=False)
        self.management.install.assert_called_once_with(
            self.scope, agents=("codex", "claude-code"), ref="main", names=(),
            include_experimental=False, dry_run=False,
        )
        self.management.update.assert_not_called()
        self.management.status.assert_not_called()

    def test_install_forwards_repository_selection_ref_and_preview_options(self):
        self.management.install.return_value = self.report | {"status": "dry-run"}
        code, stdout, stderr = self.invoke([
            "skills", "install", "aihero", "--repo", "relative/project", "--agent", "claude-code",
            "--skill", "tdd", "--skill", "implement-spec", "--include-experimental",
            "--ref", "release/next", "--dry-run", "--json",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "dry-run")
        self.assertEqual(stderr, "")
        self.management.scope_for.assert_called_once_with(Path("relative/project"), global_scope=False)
        self.management.install.assert_called_once_with(
            self.scope, agents=("claude-code",), ref="release/next", names=("tdd", "implement-spec"),
            include_experimental=True, dry_run=True,
        )

    def test_global_install_can_select_only_codex(self):
        code, stdout, stderr = self.invoke([
            "skills", "install", "aihero", "--global", "--agent", "codex", "--json",
        ])
        self.assertEqual(code, 0)
        self.management.scope_for.assert_called_once_with(None, global_scope=True)
        self.management.install.assert_called_once_with(
            self.scope, agents=("codex",), ref="main", names=(),
            include_experimental=False, dry_run=False,
        )

    def test_update_uses_recorded_selection_and_forwards_only_ref_and_preview(self):
        self.management.update.return_value = self.report | {"status": "updated"}
        code, stdout, stderr = self.invoke([
            "skills", "update", "aihero", "--repo", "/project", "--ref", "b" * 40,
            "--dry-run", "--json",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "updated")
        self.assertEqual(stderr, "")
        self.management.scope_for.assert_called_once_with(Path("/project"), global_scope=False)
        self.management.update.assert_called_once_with(self.scope, ref="b" * 40, dry_run=True)
        self.management.install.assert_not_called()
        self.management.status.assert_not_called()

    def test_global_update_defaults_to_main(self):
        self.invoke(["skills", "update", "aihero", "--global", "--json"])
        self.management.scope_for.assert_called_once_with(None, global_scope=True)
        self.management.update.assert_called_once_with(self.scope, ref="main", dry_run=False)

    def test_status_only_reads_the_selected_scope_and_preserves_json(self):
        self.management.status.return_value = self.report | {"status": "unchanged"}
        code, stdout, stderr = self.invoke(["skills", "status", "aihero", "--global", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), self.management.status.return_value)
        self.assertEqual(stderr, "")
        self.management.scope_for.assert_called_once_with(None, global_scope=True)
        self.management.status.assert_called_once_with(self.scope)
        self.management.install.assert_not_called()
        self.management.update.assert_not_called()

    def test_modified_installation_reports_conflicts_and_returns_one(self):
        self.management.status.return_value = self.report | {
            "status": "modified", "conflicts": ["tdd/SKILL.md was changed locally"],
        }
        code, stdout, stderr = self.invoke(["skills", "status", "aihero", "--repo", "/project"])
        self.assertEqual(code, 1)
        self.assertIn("AI Hero skills: modified", stdout)
        self.assertIn(f"Revision: {'a' * 40}", stdout)
        self.assertIn("Skills: 2", stdout)
        self.assertIn("codex: /project/.agents/skills", stdout)
        self.assertIn("claude-code: /project/.claude/skills", stdout)
        self.assertIn("Manifest: /project/.anvil/skills/aihero.json", stdout)
        self.assertIn("Conflict: tdd/SKILL.md was changed locally", stdout)
        self.assertEqual(stderr, "")

    def test_status_exit_codes_include_absent_and_modified_installations(self):
        for status, expected in (("not-installed", 0), ("unchanged", 0), ("modified", 1)):
            with self.subTest(status=status):
                self.management.status.return_value = self.report | {"status": status}
                code, stdout, stderr = self.invoke(["skills", "status", "aihero", "--json"])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(stdout)["status"], status)
                self.assertEqual(stderr, "")

    def test_uninstalled_text_status_does_not_require_a_revision(self):
        self.management.status.return_value = {
            "source": "aihero", "status": "not-installed", "revision": None,
            "agents": {}, "skills": [], "manifest": "/project/.anvil/skills/aihero.json", "conflicts": [],
        }
        code, stdout, stderr = self.invoke(["skills", "status", "aihero"])
        self.assertEqual(code, 0)
        self.assertIn("AI Hero skills: not-installed", stdout)
        self.assertIn("Skills: 0", stdout)
        self.assertNotIn("Revision:", stdout)
        self.assertEqual(stderr, "")

    def test_errors_in_scope_and_each_action_return_two_without_tracebacks(self):
        for action in ("install", "update", "status"):
            for failing in ("scope_for", action):
                for failure in (ValueError("invalid upstream selection"), OSError("cannot read skill files")):
                    for as_json in (False, True):
                        with self.subTest(action=action, failing=failing, failure=failure, as_json=as_json):
                            function = getattr(self.management, failing)
                            function.side_effect = failure
                            try:
                                code, stdout, stderr = self.invoke(
                                    ["skills", action, "aihero"] + (["--json"] if as_json else []))
                            finally:
                                function.side_effect = None
                            self.assertEqual(code, 2)
                            if as_json:
                                self.assertEqual(json.loads(stdout), {"error": str(failure)})
                                self.assertEqual(stderr, "")
                            else:
                                self.assertEqual(stdout, "")
                                self.assertEqual(stderr, f"anvil: {failure}\n")

    def test_scope_flags_are_mutually_exclusive_for_every_action(self):
        for action in ("install", "update", "status"):
            with self.subTest(action=action), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as exit:
                parser().parse_args(["skills", action, "aihero", "--repo", "/project", "--global"])
            self.assertEqual(exit.exception.code, 2)
        self.management.scope_for.assert_not_called()

    def test_update_and_status_reject_install_only_options(self):
        invalid = [["skills"], ["skills", "install"], ["skills", "install", "unknown"],
                   ["skills", "install", "aihero", "--agent", "claude"],
                   ["skills", "status", "aihero", "--dry-run"],
                   ["skills", "status", "aihero", "--ref", "main"]]
        for action in ("update", "status"):
            invalid.extend([
                ["skills", action, "aihero", "--agent", "codex"],
                ["skills", action, "aihero", "--skill", "tdd"],
                ["skills", action, "aihero", "--include-experimental"],
            ])
        for arguments in invalid:
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as exit:
                parser().parse_args(arguments)
            self.assertEqual(exit.exception.code, 2)
        self.management.scope_for.assert_not_called()


if __name__ == "__main__":
    unittest.main()
