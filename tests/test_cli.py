from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from anvil.cli import SUBCOMMANDS, main


class CliTests(unittest.TestCase):
    def test_example_plan_is_json_and_does_not_mutate_input(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "tickets.json"
        before = example.read_bytes()
        output = StringIO()
        with redirect_stdout(output):
            result = main(["plan", str(example), "--json"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "dry-run")
        self.assertEqual(example.read_bytes(), before)

    def test_missing_input_is_a_nonzero_json_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with redirect_stdout(output):
                result = main(["validate", str(Path(directory) / "missing.json"), "--json"])
        self.assertEqual(result, 2)
        self.assertFalse(json.loads(output.getvalue())["valid"])

    def test_malformed_input_reports_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "broken.json"
            source.write_text("{", encoding="utf-8")
            error = StringIO()
            with redirect_stderr(error):
                result = main(["plan", str(source)])
        self.assertEqual(result, 2)
        self.assertIn("anvil:", error.getvalue())

    def test_parser_limits_report_json_errors_without_traceback(self):
        payloads = ["{\"version\":" + "1" * 5000 + "}", "[" * 2000 + "]" * 2000]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "oversized.json"
            for payload in payloads:
                with self.subTest(payload_length=len(payload)):
                    source.write_text(payload, encoding="utf-8")
                    output = StringIO()
                    with redirect_stdout(output):
                        result = main(["validate", str(source), "--json"])
                    self.assertEqual(result, 2)
                    self.assertFalse(json.loads(output.getvalue())["valid"])


if __name__ == "__main__":
    unittest.main()


class DocumentedSubcommandTests(unittest.TestCase):
    """AGENTS.md: keep the README and the entry skill aligned with the CLI.

    Presence of the literal `anvil <name>` is a weak check and the right one: a
    stronger one would constrain how the documents are written, while the
    failure worth catching is a subcommand nobody documented at all.
    """

    def test_every_subcommand_appears_in_the_readme_and_the_entry_skill(self):
        root = Path(__file__).resolve().parents[1]
        documents = {
            "README.md": (root / "README.md").read_text(encoding="utf-8"),
            "skills/anvil/SKILL.md": (root / "skills" / "anvil" / "SKILL.md").read_text(encoding="utf-8"),
        }
        self.assertGreater(len(SUBCOMMANDS), 1, "the parser exposes no subcommands")
        missing = {name: sorted(where for where, text in documents.items()
                                if f"anvil {name}" not in text)
                   for name in SUBCOMMANDS}
        self.assertEqual({name: where for name, where in missing.items() if where}, {},
                         "undocumented subcommands; each must appear as `anvil <name>`")
