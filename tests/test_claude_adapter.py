"""Claude Code invocation/probe contracts without model calls."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from anvil.adapters.claude import build_invocation, doctor
from anvil.processes import ProcessOutcome


HELP = """--print --input-format --output-format --verbose --json-schema
--no-session-persistence --safe-mode --strict-mcp-config --mcp-config
--disable-slash-commands --no-chrome --permission-prompts --permission-mode
--tools --disallowedTools"""


class ClaudeInvocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="anvil claude invocation ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()

    def build(self, **overrides):
        args = dict(repo=self.repo, prompt="Implement the ticket", schema={"type": "object"})
        args.update(overrides)
        return build_invocation(**args)

    @patch("anvil.adapters.claude.run_process")
    def test_literal_prompt_and_inline_schema_do_not_launch_processes(self, run):
        prompt = 'Fix "quoted" text; $(touch bad)\n`echo secret`\n--dangerously-skip-permissions'
        schema = {"type": "object", "description": "literal $(touch bad)\n`text`"}
        binary = "/Applications/Agent Tools/claude"
        invocation = self.build(prompt=prompt, schema=schema, claude_binary=binary)
        self.assertEqual(invocation.stdin, prompt)
        self.assertNotIn(prompt, invocation.argv)
        self.assertEqual(invocation.argv[0], binary)
        self.assertEqual(json.loads(invocation.argv[invocation.argv.index("--json-schema") + 1]), schema)
        run.assert_not_called()

    def test_worker_and_reviewer_get_only_their_file_tools(self):
        for read_only, expected, mode in ((False, "Read,Glob,Grep,Edit,Write", "acceptEdits"),
                                          (True, "Read,Glob,Grep", "dontAsk")):
            with self.subTest(read_only=read_only):
                argv = self.build(read_only=read_only).argv
                self.assertEqual(argv[argv.index("--tools") + 1], expected)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], mode)
                denied = argv[argv.index("--disallowedTools") + 1].split(",")
                for name in ("Bash", "PowerShell", "Agent", "Task", "Skill", "mcp__*"):
                    self.assertIn(name, denied)
                if read_only:
                    for name in ("Edit", "Write", "NotebookEdit"):
                        self.assertIn(name, denied)
                self.assertEqual(argv[argv.index("--max-turns") + 1], "32")
                self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
                self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {}})
                for flag in ("--safe-mode", "--no-session-persistence", "--disable-slash-commands", "--no-chrome"):
                    self.assertIn(flag, argv)
                for flag in ("--model", "--bare", "--allowedTools", "--dangerously-skip-permissions",
                             "--allow-dangerously-skip-permissions", "--resume", "--continue"):
                    self.assertNotIn(flag, argv)

    def test_worktree_marker_and_invalid_inputs(self):
        (self.repo / ".git").rmdir()
        (self.repo / ".git").write_text("gitdir: ../main/.git/worktrees/example\n")
        self.build()
        for field in ("prompt", "claude_binary"):
            for value in ("", " \n", "before\0after", None):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.build(**{field: value})
        for schema in ([], None, {"value": float("nan")}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                self.build(schema=schema)
        for read_only in (0, 1, "true", None):
            with self.subTest(read_only=read_only), self.assertRaises(ValueError):
                self.build(read_only=read_only)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.build(repo=self.root / "missing")
        (self.repo / ".git").unlink()
        with self.assertRaisesRegex(ValueError, "worktree file"):
            self.build()


class ClaudeDoctorTests(unittest.TestCase):
    @patch("anvil.adapters.claude.run_process")
    @patch("anvil.adapters.claude.shutil.which", return_value=None)
    def test_missing_binary_and_unprobed_binary_never_launch(self, which, run):
        result = doctor(probe=True)
        self.assertIsNone(result.executable)
        self.assertIsNotNone(result.error)
        which.return_value = "/tools/claude"
        result = doctor()
        self.assertEqual(result.executable, "/tools/claude")
        self.assertIsNone(result.compatible)
        self.assertIsNone(result.version)
        run.assert_not_called()

    def probe(self, run, version="2.1.260 (Claude Code)\n", help_text=HELP):
        def fake(argv, **kwargs):
            kwargs["stdout_path"].write_text(version if argv[-1] == "--version" else help_text)
            return ProcessOutcome(0, False)
        run.side_effect = fake
        return doctor(probe=True, timeout=2)

    @patch("anvil.adapters.claude.run_process")
    @patch("anvil.adapters.claude.shutil.which", return_value="/tools/claude")
    def test_probe_withholds_excluded_variables_like_a_turn(self, which, run):
        def fake(argv, **kwargs):
            kwargs["stdout_path"].write_text(
                "2.1.260 (Claude Code)\n" if argv[-1] == "--version" else HELP)
            return ProcessOutcome(0, False)
        run.side_effect = fake
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret", "PATH": "/usr/bin"},
                        clear=True):
            doctor(probe=True, timeout=2, exclude=("ANTHROPIC_API_KEY",))
        for call in run.call_args_list:
            self.assertNotIn("ANTHROPIC_API_KEY", call.kwargs["env"])
            self.assertIn("PATH", call.kwargs["env"])

    @patch("anvil.adapters.claude.run_process")
    @patch("anvil.adapters.claude.shutil.which", return_value="/tools/claude")
    def test_probe_only_runs_bounded_version_and_help_without_model(self, which, run):
        result = self.probe(run)
        self.assertTrue(result.compatible)
        self.assertEqual(result.version, "2.1.260 (Claude Code)")
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ("/tools/claude", "--version"), ("/tools/claude", "--help")])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 2)
            self.assertIsNone(call.kwargs["stdin"])
        self.assertNotIn("--max-turns", HELP)

    @patch("anvil.adapters.claude.run_process")
    @patch("anvil.adapters.claude.shutil.which", return_value="/tools/claude")
    def test_old_or_unrecognized_version_is_rejected_before_help(self, which, run):
        for version in ("2.1.259 (Claude Code)", "2.1.26 (Claude Code)", "unexpected", "", "2.1.260-beta"):
            with self.subTest(version=version):
                run.reset_mock()
                result = self.probe(run, version=version)
                self.assertFalse(result.compatible)
                self.assertIn("2.1.260", result.error)
                self.assertEqual(run.call_count, 1)

    @patch("anvil.adapters.claude.run_process")
    @patch("anvil.adapters.claude.shutil.which", return_value="/tools/claude")
    def test_missing_required_flag_is_reported(self, which, run):
        result = self.probe(run, version="2.2.0", help_text=HELP.replace("--safe-mode", "--safe-modes"))
        self.assertFalse(result.compatible)
        self.assertEqual(result.missing_flags, ("--safe-mode",))

    @patch("anvil.adapters.claude.run_process")
    @patch("anvil.adapters.claude.shutil.which", return_value="/tools/claude")
    def test_probe_exit_timeout_and_bad_output_are_reported(self, which, run):
        for outcome, expected in ((ProcessOutcome(2, False), "code 2"),
                                  (ProcessOutcome(-9, True), "timed out")):
            with self.subTest(outcome=outcome):
                run.side_effect = None
                run.return_value = outcome
                result = doctor(probe=True)
                self.assertFalse(result.compatible)
                self.assertIn(expected, result.error)
        run.side_effect = lambda argv, **kwargs: (kwargs["stdout_path"].write_bytes(b"\xff"), ProcessOutcome(0, False))[1]
        self.assertFalse(doctor(probe=True).compatible)

    def test_invalid_timeout_is_rejected(self):
        for timeout in (0, -1, 31, float("inf"), float("nan"), True, None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                doctor(timeout=timeout)


if __name__ == "__main__":
    unittest.main()
