"""Command-boundary checks. These tests do not run Codex or use model credits."""

from pathlib import Path
import os
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from anvil.adapters.codex import build_invocation, doctor


class InvocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="anvil adapter ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.schema = self.root / "result schema.json"
        self.schema.write_text('{"type": "object"}', encoding="utf-8")
        self.output = self.root / "result file.json"

    def build(self, **overrides):
        args = dict(
            repo=self.repo, prompt="Implement the ticket", result_schema=self.schema,
            result_file=self.output,
        )
        args.update(overrides)
        return build_invocation(**args)

    @patch("anvil.adapters.codex.subprocess.run")
    def test_prompt_stays_literal_on_stdin_and_build_never_launches(self, run):
        prompt = 'Fix "quoted" text; $(touch /tmp/not-created)\n`echo secret`\n--dangerously-bypass-approvals-and-sandbox'
        invocation = self.build(prompt=prompt)
        self.assertEqual(invocation.stdin, prompt)
        self.assertNotIn(prompt, invocation.argv)
        self.assertEqual(invocation.argv[-1], "-")
        self.assertFalse(self.output.exists())
        run.assert_not_called()

    def test_space_containing_paths_remain_single_arguments(self):
        binary = "/Applications/Agent Tools/codex"
        invocation = self.build(codex_binary=binary)
        self.assertEqual(invocation.argv[0], binary)
        for flag, expected in (("-C", self.repo), ("--output-schema", self.schema), ("-o", self.output)):
            self.assertEqual(invocation.argv[invocation.argv.index(flag) + 1], str(expected))

    def test_explicit_sandbox_and_no_bypass_or_model_override(self):
        argv = self.build().argv
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn("--json", argv)
        for flag in (
            "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
            "--skip-git-repo-check", "--ignore-rules", "--ignore-user-config", "--full-auto", "--model",
        ):
            self.assertNotIn(flag, argv)

    def test_worktree_git_file_is_accepted(self):
        (self.repo / ".git").rmdir()
        (self.repo / ".git").write_text("gitdir: ../main/.git/worktrees/example\n")
        self.assertEqual(self.build().argv[1], "exec")

    def test_missing_repo_or_git_marker_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.build(repo=self.root / "missing")
        (self.repo / ".git").rmdir()
        with self.assertRaisesRegex(ValueError, "worktree file"):
            self.build()

    def test_schema_requires_readable_json_object(self):
        with self.assertRaisesRegex(ValueError, "existing file"):
            self.build(result_schema=self.root / "missing.json")
        for content in ("not json", "[]", "null"):
            with self.subTest(content=content):
                self.schema.write_text(content)
                with self.assertRaises(ValueError):
                    self.build()

    def test_existing_result_is_not_overwritten(self):
        self.output.write_text("existing evidence")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build()
        self.assertEqual(self.output.read_text(), "existing evidence")

    def test_output_directory_or_dangling_symlink_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build(result_file=self.root)
        self.output.symlink_to(self.root / "missing-target")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build()

    def test_output_parent_must_already_exist(self):
        with self.assertRaisesRegex(ValueError, "parent directory does not exist"):
            self.build(result_file=self.root / "new" / "result.json")
        self.assertFalse((self.root / "new").exists())

    def test_empty_or_nul_prompt_and_binary_are_rejected(self):
        for field in ("prompt", "codex_binary"):
            for value in ("", " \n", "before\0after"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.build(**{field: value})


class DoctorTests(unittest.TestCase):
    @patch("anvil.adapters.codex.subprocess.run")
    @patch("anvil.adapters.codex.shutil.which", return_value=None)
    def test_missing_cli_does_not_launch_anything(self, which, run):
        result = doctor(probe=True)
        self.assertIsNone(result.executable)
        self.assertIsNotNone(result.error)
        run.assert_not_called()

    @patch("anvil.adapters.codex.subprocess.run")
    @patch("anvil.adapters.codex.shutil.which", return_value="/tools/codex")
    def test_default_only_checks_binary_location(self, which, run):
        result = doctor()
        self.assertEqual(result.executable, "/tools/codex")
        self.assertIsNone(result.compatible)
        self.assertIsNone(result.version)
        run.assert_not_called()

    @patch("anvil.adapters.codex.subprocess.run")
    @patch("anvil.adapters.codex.shutil.which", return_value="/tools/codex")
    def test_probe_withholds_excluded_variables_like_a_turn(self, which, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "codex-cli test\n", ""),
            subprocess.CompletedProcess([], 0, "--sandbox workspace-write --cd --json "
                                               "--output-schema --output-last-message", ""),
        ]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "PATH": "/usr/bin"},
                        clear=True):
            doctor(probe=True, timeout=2, exclude=("OPENAI_API_KEY",))
        for call in run.call_args_list:
            self.assertNotIn("OPENAI_API_KEY", call.kwargs["env"])
            self.assertIn("PATH", call.kwargs["env"])

    @patch("anvil.adapters.codex.subprocess.run")
    @patch("anvil.adapters.codex.shutil.which", return_value="/tools/codex")
    def test_probe_only_runs_bounded_version_and_help(self, which, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "codex-cli test\n", ""),
            subprocess.CompletedProcess([], 0, "--sandbox workspace-write --cd --json --output-schema --output-last-message", ""),
        ]
        result = doctor(probe=True, timeout=2)
        self.assertTrue(result.compatible)
        self.assertEqual(result.version, "codex-cli test")
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["/tools/codex", "--version"], ["/tools/codex", "exec", "--help"],
        ])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 2)
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)

    @patch("anvil.adapters.codex.subprocess.run")
    @patch("anvil.adapters.codex.shutil.which", return_value="/tools/codex")
    def test_missing_flags_are_reported(self, which, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, "codex-cli test", ""),
            subprocess.CompletedProcess([], 0, "--sandbox workspace-write --cd --json", ""),
        ]
        result = doctor(probe=True)
        self.assertFalse(result.compatible)
        self.assertEqual(result.missing_flags, ("--output-schema", "--output-last-message"))

    @patch("anvil.adapters.codex.subprocess.run")
    @patch("anvil.adapters.codex.shutil.which", return_value="/tools/codex")
    def test_failed_probe_is_reported_without_raw_stderr(self, which, run):
        run.return_value = subprocess.CompletedProcess([], 2, "", "sensitive local details")
        result = doctor(probe=True)
        self.assertFalse(result.compatible)
        self.assertIn("code 2", result.error)
        self.assertNotIn("sensitive", result.error)
        self.assertEqual(run.call_count, 1)

    @patch("anvil.adapters.codex.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 2))
    @patch("anvil.adapters.codex.shutil.which", return_value="/tools/codex")
    def test_timeout_is_reported(self, which, run):
        result = doctor(probe=True)
        self.assertFalse(result.compatible)
        self.assertIn("TimeoutExpired", result.error)

    def test_invalid_timeouts_are_rejected(self):
        for timeout in (0, -1, 31, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    doctor(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
