"""Codex execution contract using a local fake binary; no agent turns."""

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from anvil.adapters.codex import CodexRunner, build_invocation
from anvil.processes import ProcessError


@unittest.skipUnless(os.name == "posix", "POSIX process execution")
class CodexExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="anvil fake codex ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.artifacts = self.root / "new artifacts"
        self.binary = self.root / "fake codex"
        self.schema = {"type": "object", "properties": {"status": {"type": "string"}}}

    def fake(self, body):
        self.binary.write_text(
            f"#!{sys.executable}\n"
            "import json,pathlib,sys,time\n"
            "args=sys.argv[1:]\n"
            "output=pathlib.Path(args[args.index('-o')+1])\n"
            "prompt=sys.stdin.read()\n" + body + "\n",
            encoding="utf-8",
        )
        self.binary.chmod(0o755)

    def run_fake(self, **overrides):
        args = dict(repo=self.repo, prompt="Implement $(touch bad)\n`literal`", schema=self.schema,
                    artifact_dir=self.artifacts, timeout=3)
        args.update(overrides)
        return CodexRunner(str(self.binary)).run(**args)

    def test_launch_records_artifacts_and_returns_unverified_object(self):
        self.fake(
            "output.write_text(json.dumps({'claimed': 'success', 'argv': args, 'prompt': prompt, 'cwd': str(pathlib.Path.cwd())}))\n"
            "print(json.dumps({'event': 'fake'}))\n"
            "print('diagnostic', file=sys.stderr)"
        )
        result = self.run_fake()
        self.assertEqual(result["claimed"], "success")
        self.assertEqual(result["prompt"], "Implement $(touch bad)\n`literal`")
        self.assertEqual(result["cwd"], str(self.repo))
        argv = result["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        for flag in ("--model", "--full-auto", "--dangerously-bypass-approvals-and-sandbox"):
            self.assertNotIn(flag, argv)
        self.assertEqual(json.loads((self.artifacts / "schema.json").read_text()), self.schema)
        self.assertEqual(json.loads((self.artifacts / "events.jsonl").read_text()), {"event": "fake"})
        self.assertEqual((self.artifacts / "stderr.log").read_text(), "diagnostic\n")
        self.assertFalse((self.repo / "bad").exists())

    def test_read_only_is_explicit(self):
        self.fake("output.write_text(json.dumps({'sandbox': args[args.index('--sandbox')+1]}))")
        self.assertEqual(self.run_fake(read_only=True), {"sandbox": "read-only"})

    def test_timeout_and_nonzero_exit_reject_even_a_valid_result(self):
        for name, tail, expected in (("timeout", "time.sleep(30)", "timed out"),
                                     ("nonzero", "sys.exit(4)", "code 4")):
            with self.subTest(name=name):
                self.fake("output.write_text('{\"status\":\"success\"}')\n" + tail)
                with self.assertRaisesRegex(ProcessError, expected):
                    self.run_fake(artifact_dir=self.root / name, timeout=0.1 if name == "timeout" else 3)

    def test_missing_malformed_and_nonobject_results_are_rejected(self):
        cases = ("pass", "output.write_text('broken')", "output.write_text('[]')",
                 "output.write_bytes(b'\\xff')", "output.symlink_to(output.parent / 'schema.json')",
                 "output.write_text('x' * (4 * 1024 * 1024 + 1))",
                 "output.write_text('{\"status\":\"failed\",\"status\":\"success\"}')",
                 "output.write_text('{\"value\":NaN}')", "output.write_text('{\"value\":Infinity}')",
                 "output.write_text('[' * 2000 + ']' * 2000)")
        for index, body in enumerate(cases):
            with self.subTest(body=body):
                self.fake(body)
                with self.assertRaisesRegex(ProcessError, "invalid result"):
                    self.run_fake(artifact_dir=self.root / f"invalid-{index}")

    def test_artifact_directory_must_be_new(self):
        self.fake("raise AssertionError('must not run')")
        self.artifacts.mkdir()
        marker = self.artifacts / "schema.json"
        marker.write_text("existing evidence")
        with self.assertRaisesRegex(ProcessError, "prepare"):
            self.run_fake()
        self.assertEqual(marker.read_text(), "existing evidence")
        self.assertFalse((self.artifacts / "events.jsonl").exists())

    def test_prepare_and_start_errors_are_reported(self):
        with self.assertRaisesRegex(ProcessError, "schema"):
            self.run_fake(schema=[])
        with self.assertRaisesRegex(ProcessError, "start command"):
            self.run_fake()

    def test_builder_rejects_sandbox_bypass(self):
        schema_file = self.root / "schema.json"
        schema_file.write_text('{}')
        for sandbox in ("danger-full-access", "", None):
            with self.subTest(sandbox=sandbox), self.assertRaisesRegex(ValueError, "sandbox"):
                build_invocation(self.repo, "prompt", schema_file, self.root / "result.json", sandbox=sandbox)


if __name__ == "__main__":
    unittest.main()
