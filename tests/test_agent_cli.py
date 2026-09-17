from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import unittest
from unittest.mock import patch

from anvil.adapters.codex import CodexDoctor
from anvil.cli import main


class AgentDoctorCliTests(unittest.TestCase):
    def invoke(self, arguments):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(["doctor", *arguments])
        return code, output.getvalue(), error.getvalue()

    def test_default_doctor_keeps_codex_output_and_probes_only_codex(self):
        with patch("anvil.adapters.codex.doctor", return_value=CodexDoctor(
                executable="/tools/codex", compatible=True)) as probe, \
                patch("anvil.cli.shutil.which", return_value="/tools/git"):
            code, output, error = self.invoke(["--json"])
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result["agent"], "codex")
        self.assertEqual(result["codex"]["executable"], "/tools/codex")
        self.assertTrue(result["ticket_skills_available"])
        self.assertTrue(result["spec_preparation_available"])
        probe.assert_called_once_with("codex", probe=True, exclude=())
        self.assertEqual(error, "")

    def test_claude_selection_and_custom_binary_reach_only_claude_probe(self):
        from anvil.adapters.claude import ClaudeDoctor
        for binary in (None, "/tools/claude custom"):
            with self.subTest(binary=binary), \
                    patch("anvil.adapters.claude.doctor", return_value=ClaudeDoctor(
                        executable=binary or "/tools/claude", compatible=True)) as probe, \
                    patch("anvil.adapters.codex.doctor") as codex, \
                    patch("anvil.cli.shutil.which", return_value="/tools/git"):
                arguments = ["--agent", "claude-code", "--json"]
                if binary is not None:
                    arguments.extend(["--agent-binary", binary])
                code, output, error = self.invoke(arguments)
            self.assertEqual(code, 0)
            result = json.loads(output)
            self.assertEqual(result["agent"], "claude-code")
            self.assertTrue(result["claude-code"]["compatible"])
            self.assertNotIn("codex", result)
            probe.assert_called_once_with(binary or "claude", probe=True, exclude=())
            codex.assert_not_called()
            self.assertEqual(error, "")


    def test_configured_exclusion_reaches_the_probe_it_precedes(self):
        import json as _json
        import tempfile
        from pathlib import Path as _Path
        root = _Path(tempfile.mkdtemp())
        config = root / "run.json"
        config.write_text(_json.dumps({
            "version": 1, "repo": str(root), "tickets": str(root / "tickets.json"),
            "verification": [["true"]], "agent": "codex",
            "credential_exclusion": ["OPENAI_API_KEY"]}))
        with patch("anvil.adapters.codex.doctor", return_value=CodexDoctor(
                executable="/tools/codex", compatible=True)) as probe, \
                patch("anvil.cli.shutil.which", return_value="/tools/git"):
            code, output, error = self.invoke(["--config", str(config), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(probe.call_args.kwargs["exclude"], ("OPENAI_API_KEY",))
        self.assertEqual(error, "")

    def test_failed_probe_is_actionable_and_nonzero(self):
        with patch("anvil.cli.probe_agent", return_value=CodexDoctor(
                executable=None, compatible=False, error="Claude executable was not found")):
            code, output, error = self.invoke(["--agent", "claude-code"])
        self.assertEqual(code, 1)
        self.assertIn("Claude Code", output)
        self.assertIn("not found", output)
        self.assertEqual(error, "")

    def test_invalid_custom_binary_is_an_input_error(self):
        code, output, error = self.invoke(["--agent-binary", "", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("nonempty", json.loads(output)["error"])
        self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()
