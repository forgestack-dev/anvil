"""Claude Code process/result contracts using executable fixtures, never models."""

from contextlib import chdir
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from anvil.adapters.claude import ClaudeRunner, doctor
from anvil.processes import ProcessError


CLAUDE_HELP = (
    "--print --input-format --output-format --verbose --json-schema "
    "--no-session-persistence --safe-mode --strict-mcp-config --mcp-config "
    "--disable-slash-commands --no-chrome --permission-prompts --permission-mode --tools --disallowedTools"
)
TASK_SUMMARY = {
    "type": "system", "subtype": "task_summary", "detail": None,
    "uuid": "9bac0a74-c640-49a4-8bd4-1eb83c7976e0",
    "session_id": "2ae095af-af74-43b1-9c3e-8ce735b95384",
}
SUMMARY_EMISSION = f"print({json.dumps(TASK_SUMMARY)!r}, flush=True)"


@unittest.skipUnless(os.name == "posix", "POSIX process execution")
class ClaudeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="anvil fake claude ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.artifacts = self.root / "new artifacts"
        self.binary = self.root / "fake claude"
        self.schema = {"type": "object", "properties": {"status": {"type": "string"}}}

    def fake(self, body):
        self.binary.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,sys,time\n"
            "args=sys.argv[1:]\n"
            "prompt=sys.stdin.read()\n"
            "def emit(value):\n"
            "    print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'permission_denials':[],'structured_output':value}), flush=True)\n"
            + body + "\n", encoding="utf-8")
        self.binary.chmod(0o755)

    def run_fake(self, **overrides):
        args = dict(repo=self.repo, prompt="Implement $(touch bad)\n`literal`", schema=self.schema,
                    artifact_dir=self.artifacts, timeout=3)
        args.update(overrides)
        return ClaudeRunner(str(self.binary)).run(**args)

    def test_real_process_receives_literal_stdin_and_retains_all_artifacts(self):
        self.fake(
            "print(json.dumps({'type':'system','subtype':'init'}))\n"
            "print('diagnostic', file=sys.stderr)\n"
            "emit({'claimed':'success','argv':args,'prompt':prompt,'cwd':str(pathlib.Path.cwd())})")
        result = self.run_fake()
        self.assertEqual(result["claimed"], "success")
        self.assertEqual(result["prompt"], "Implement $(touch bad)\n`literal`")
        self.assertEqual(result["cwd"], str(self.repo))
        argv = result["argv"]
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), self.schema)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep,Edit,Write")
        self.assertEqual(json.loads((self.artifacts / "schema.json").read_text()), self.schema)
        self.assertEqual(json.loads((self.artifacts / "result.json").read_text()), result)
        events = [json.loads(line) for line in (self.artifacts / "events.jsonl").read_text().splitlines()]
        self.assertEqual([event["type"] for event in events], ["system", "result"])
        self.assertEqual((self.artifacts / "stderr.log").read_text(), "diagnostic\n")
        self.assertFalse((self.repo / "bad").exists())

    def test_explicit_profile_reaches_both_roles(self):
        self.fake("emit({'argv':args})")
        for review in (False, True):
            result = ClaudeRunner(str(self.binary), profile={"model":"test-model", "effort":"high"}).run(
                repo=self.repo, prompt="Inspect fixture", schema=self.schema,
                artifact_dir=self.root / f"profile-{review}", timeout=3, read_only=review)
            argv = result["argv"]
            self.assertEqual(argv[argv.index("--model")+1], "test-model")
            self.assertEqual(argv[argv.index("--effort")+1], "high")

    def test_read_only_tools_and_inherited_git_environment_are_isolated(self):
        self.fake("emit({'tools':args[args.index('--tools')+1], 'mode':args[args.index('--permission-mode')+1],"
                  " 'git':{k:v for k,v in os.environ.items() if k.startswith('GIT_')},"
                  " 'literal':os.environ['ANVIL_TEST_LITERAL']})")
        inherited = {"GIT_DIR": "/another/repo/.git", "GIT_WORK_TREE": "/another/repo",
                     "ANVIL_TEST_LITERAL": "$(touch bad)\n`literal`"}
        for read_only in (True, False):
            with self.subTest(read_only=read_only), patch.dict(os.environ, inherited):
                before = dict(os.environ)
                result = self.run_fake(read_only=read_only, artifact_dir=self.root / f"role-{read_only}")
                self.assertEqual(dict(os.environ), before)
                self.assertEqual(result["git"], {})
                self.assertEqual(result["literal"], inherited["ANVIL_TEST_LITERAL"])
                self.assertEqual(result["tools"], "Read,Glob,Grep" if read_only else "Read,Glob,Grep,Edit,Write")
                self.assertEqual(result["mode"], "dontAsk" if read_only else "acceptEdits")

    def test_excluded_variable_is_withheld_from_the_subprocess_environment(self):
        self.fake("emit({'leaked': 'ANVIL_TEST_SECRET' in os.environ, 'argv': args})")
        for read_only in (True, False):
            with self.subTest(read_only=read_only), patch.dict(os.environ, {"ANVIL_TEST_SECRET": "s3cret-value"}):
                result = ClaudeRunner(str(self.binary), exclude=["ANVIL_TEST_SECRET"]).run(
                    repo=self.repo, prompt="Inspect fixture", schema=self.schema,
                    artifact_dir=self.root / f"exclusion-{read_only}", timeout=3, read_only=read_only)
                self.assertFalse(result["leaked"])
                self.assertNotIn("s3cret-value", json.dumps(result["argv"]))
                artifact_dir = self.root / f"exclusion-{read_only}"
                self.assertNotIn("s3cret-value", (artifact_dir / "events.jsonl").read_text())
                self.assertNotIn("s3cret-value", (artifact_dir / "result.json").read_text())

    def test_task_summary_after_success_preserves_worker_and_reviewer_results_and_raw_stream(self):
        for read_only in (False, True):
            for summary_count in (1, 2):
                with self.subTest(read_only=read_only, summary_count=summary_count):
                    claims = ({"verdict": "approve", "summary": "Inspected candidate", "findings": [],
                               "acceptance": [{"criterion": 1, "satisfied": True, "evidence": "Checked"}]}
                              if read_only else
                              {"status": "completed", "summary": "Implemented ticket", "blockers": [],
                               "acceptance": [{"criterion": 1, "evidence": "Implemented"}]})
                    events = [
                        {"type": "system", "subtype": "init"},
                        {"type": "result", "subtype": "success", "is_error": False,
                         "permission_denials": [], "structured_output": claims, "num_turns": 21,
                         "session_id": TASK_SUMMARY["session_id"]},
                    ] + [TASK_SUMMARY] * summary_count
                    raw_stream = "".join(json.dumps(event) + "\n" for event in events)
                    self.fake(f"sys.stdout.write({raw_stream!r})")
                    artifacts = self.root / f"summary-{read_only}-{summary_count}"
                    result = self.run_fake(read_only=read_only, artifact_dir=artifacts)
                    self.assertEqual(result, claims)
                    self.assertEqual(json.loads((artifacts / "result.json").read_text()), claims)
                    self.assertEqual((artifacts / "events.jsonl").read_bytes(), raw_stream.encode())

    def test_task_summary_does_not_hide_invalid_events_after_the_result(self):
        invalid_tails = (
            "emit({})",  # A second result is ambiguous even after an allowed summary.
            "print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True}))",
            "print(json.dumps({'type':'system','subtype':'permission_denied'}))",
            "print(json.dumps({'type':'system','subtype':'init'}))",
            "print(json.dumps({'type':'system','subtype':'unknown'}))",
            "print(json.dumps({'type':'system'}))",
            "print(json.dumps({'type':'assistant','subtype':'task_summary'}))",
            "print(json.dumps({'type':'tool','subtype':'task_summary'}))",
            "print('{}')", "print('[]')", "print('null')", "print()", "print('broken')",
            "sys.stdout.buffer.write(b'\\xff')",
            "print('{\"type\":\"system\",\"subtype\":\"init\",\"subtype\":\"task_summary\"}')",
            "print('{\"type\":\"system\",\"subtype\":\"task_summary\",\"detail\":{\"x\":1,\"x\":2}}')",
            *[f"print('{{\"type\":\"system\",\"subtype\":\"task_summary\",\"detail\":{number}}}')"
              for number in ("NaN", "Infinity", "1e999")],
        )
        for index, tail in enumerate(invalid_tails):
            with self.subTest(tail=tail):
                self.fake("emit({'status':'completed'})\n" + SUMMARY_EMISSION + "\n" + tail)
                artifacts = self.root / f"invalid-summary-tail-{index}"
                with self.assertRaisesRegex(ProcessError, "invalid result"):
                    self.run_fake(artifact_dir=artifacts)
                self.assertFalse((artifacts / "result.json").exists())
                self.assertIn(json.dumps(TASK_SUMMARY).encode(), (artifacts / "events.jsonl").read_bytes())

    def test_timeout_or_nonzero_exit_rejects_even_a_success_envelope(self):
        for name, tail, expected in (("timeout", "time.sleep(30)", "timed out"),
                                     ("nonzero", "sys.exit(4)", "code 4")):
            with self.subTest(name=name):
                self.fake("emit({'status':'completed'})\n" + SUMMARY_EMISSION + "\n" + tail)
                artifacts = self.root / name
                with self.assertRaisesRegex(ProcessError, expected):
                    self.run_fake(artifact_dir=artifacts, timeout=0.15 if name == "timeout" else 3)
                self.assertFalse((artifacts / "result.json").exists())

    def test_malformed_or_ambiguous_streams_are_rejected(self):
        cases = (
            "pass", "print('broken')", "print('[]')", "sys.stdout.buffer.write(b'\\xff')",
            SUMMARY_EMISSION,
            "print('{\"type\":\"system\",\"type\":\"result\"}')",
            "print('{\"type\":\"system\",\"value\":NaN}')",
            "print('{\"type\":\"system\",\"value\":Infinity}')",
            "print('{\"type\":\"system\",\"value\":1e999}'); emit({})",
            "print('[' * 2000 + ']' * 2000)",
            "emit({}); emit({})", "emit({}); print('{}')", "emit({}); print()",
            "print('{}')", "print('x' * (32 * 1024 * 1024 + 1))",
            "emit({'value':'x' * (4 * 1024 * 1024)})",
            "print('{\"type\":\"result\"')",
            "print('{\"type\":\"system\",\"subtype\":\"permission_denied\"}'); emit({})",
        )
        for index, body in enumerate(cases):
            with self.subTest(body=body):
                self.fake(body)
                artifacts = self.root / f"invalid-{index}"
                with self.assertRaisesRegex(ProcessError, "invalid result"):
                    self.run_fake(artifact_dir=artifacts)
                self.assertFalse((artifacts / "result.json").exists())

    def test_failure_denials_and_missing_output_cannot_be_accepted(self):
        valid = {"type": "result", "subtype": "success", "is_error": False,
                 "permission_denials": [], "structured_output": {"status": "completed"}}
        cases = [
            {"subtype": subtype} for subtype in ("error_max_turns", "error_during_execution",
                                                 "error_max_budget_usd", "error_max_structured_output_retries", "unknown")
        ] + [
            {"is_error": True}, {"is_error": 0}, {"permission_denials": None},
            {"permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "private input"}}]},
            {"structured_output": []}, {"structured_output": "{\"status\":\"completed\"}"},
        ]
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                self.fake(f"print({json.dumps(dict(valid, **changes))!r})\n" + SUMMARY_EMISSION)
                artifacts = self.root / f"envelope-{index}"
                with self.assertRaisesRegex(ProcessError, "invalid result") as raised:
                    self.run_fake(artifact_dir=artifacts)
                self.assertNotIn("private input", str(raised.exception))
                self.assertFalse((artifacts / "result.json").exists())
        for field in ("structured_output", "permission_denials", "is_error", "subtype"):
            with self.subTest(missing=field):
                envelope = {key: value for key, value in valid.items() if key != field}
                self.fake(f"print({json.dumps(envelope)!r})\n" + SUMMARY_EMISSION)
                artifacts = self.root / f"missing-{field}"
                with self.assertRaisesRegex(ProcessError, "invalid result"):
                    self.run_fake(artifact_dir=artifacts)
                self.assertFalse((artifacts / "result.json").exists())

    def test_replaced_event_stream_and_result_symlinks_are_rejected(self):
        target = self.root / "outside"
        target.write_text("preserve existing evidence")
        for name in ("events.jsonl", "result.json"):
            artifacts = self.root / f"symlink-{name}"
            self.fake(
                "emit({'status':'completed'})\n"
                f"path=pathlib.Path({str(artifacts / name)!r})\n"
                "if path.exists(): path.unlink()\n"
                f"path.symlink_to({str(target)!r})")
            with self.subTest(name=name), self.assertRaisesRegex(ProcessError, "invalid result"):
                self.run_fake(artifact_dir=artifacts)
            self.assertEqual(target.read_text(), "preserve existing evidence")

    def test_existing_artifacts_invalid_preparation_and_missing_binary(self):
        self.fake("raise AssertionError('must not run')")
        self.artifacts.mkdir()
        marker = self.artifacts / "schema.json"
        marker.write_text("existing evidence")
        with self.assertRaisesRegex(ProcessError, "prepare"):
            self.run_fake()
        self.assertEqual(marker.read_text(), "existing evidence")
        with self.assertRaisesRegex(ProcessError, "schema"):
            self.run_fake(schema=[], artifact_dir=self.root / "bad-schema")
        with self.assertRaisesRegex(ProcessError, "read_only"):
            self.run_fake(read_only="yes", artifact_dir=self.root / "bad-role")
        self.binary.unlink()
        with self.assertRaisesRegex(ProcessError, "start command"):
            self.run_fake(artifact_dir=self.root / "missing-binary")

    def test_replaced_stream_fifo_is_rejected_without_waiting_for_a_writer(self):
        self.fake(
            "emit({'status':'completed'})\n"
            f"path=pathlib.Path({str(self.artifacts / 'events.jsonl')!r})\n"
            "path.unlink()\n"
            "os.mkfifo(path)")
        with self.assertRaisesRegex(ProcessError, "regular file"):
            self.run_fake()

    def test_doctor_executes_only_help_and_version_with_relative_or_absolute_binary(self):
        self.fake(
            "assert prompt == ''\n"
            "if args == ['--version']: print('2.1.260 (Claude Code)')\n"
            "elif args == ['--help']: print('--print --input-format --output-format --verbose --json-schema '"
            "'--no-session-persistence --safe-mode --strict-mcp-config --mcp-config '"
            "'--disable-slash-commands --no-chrome --permission-prompts --permission-mode --tools --disallowedTools')\n"
            "else: raise AssertionError('unexpected model invocation')")
        for binary in (str(self.binary), "./fake claude", "fake claude"):
            with self.subTest(binary=binary), chdir(self.root), patch.dict(os.environ, {"PATH": "."}):
                report = doctor(binary, probe=True)
                self.assertTrue(report.compatible, report.error)
                self.assertEqual(report.executable, str(self.binary))

    def test_relative_path_selection_is_retained_across_worktree_and_environment_changes(self):
        self.fake(
            "if args == ['--version']: print('2.1.260 (Claude Code)')\n"
            f"elif args == ['--help']: print({CLAUDE_HELP!r})\n"
            "else: emit({'entrypoint':sys.argv[0], 'cwd':str(pathlib.Path.cwd()),"
            " 'tools':args[args.index('--tools')+1]})")
        later_directory = self.root / "later supervisor"
        later_directory.mkdir()
        shadow_directory = self.root / "shadow bin"
        shadow_directory.mkdir()
        for directory in (self.repo, shadow_directory):
            shadow = directory / self.binary.name
            shadow.write_text(f"#!{sys.executable}\nraise SystemExit('wrong executable selected')\n")
            shadow.chmod(0o755)
        with chdir(self.root), patch.dict(os.environ, {"PATH": "."}):
            report = doctor(self.binary.name, probe=True)
            self.assertTrue(report.compatible, report.error)
            runner = ClaudeRunner(self.binary.name)

        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                directory = later_directory if read_only else self.root
                search_path = str(shadow_directory) if read_only else "."
                with chdir(directory), patch.dict(os.environ, {"PATH": search_path}):
                    result = runner.run(
                        repo=self.repo, prompt="Inspect this fixture", schema=self.schema,
                        artifact_dir=self.root / f"selected-{read_only}", timeout=3, read_only=read_only)
                self.assertEqual(result["entrypoint"], report.executable)
                self.assertEqual(result["cwd"], str(self.repo))
                self.assertEqual(result["tools"], "Read,Glob,Grep" if read_only else "Read,Glob,Grep,Edit,Write")

    def test_doctor_and_runner_preserve_basename_dispatch_through_symlink_aliases(self):
        self.fake(
            f"assert pathlib.Path(sys.argv[0]).name == {self.binary.name!r}, 'dispatcher requires its alias'\n"
            "if args == ['--version']: print('2.1.260 (Claude Code)')\n"
            f"elif args == ['--help']: print({CLAUDE_HELP!r})\n"
            "else: emit({'entrypoint':sys.argv[0]})")
        target = self.root / "shared dispatcher"
        self.binary.rename(target)
        self.binary.symlink_to(target.name)
        for path_index, search_path in enumerate((str(self.root), ".")):
            for binary_index, binary in enumerate((str(self.binary), f"./{self.binary.name}", self.binary.name)):
                with self.subTest(search_path=search_path, binary=binary), chdir(self.root), \
                        patch.dict(os.environ, {"PATH": search_path}):
                    report = doctor(binary, probe=True)
                    self.assertTrue(report.compatible, report.error)
                    self.assertEqual(report.executable, str(self.binary))
                    runner = ClaudeRunner(binary)
                    for read_only in (False, True):
                        artifacts = self.root / f"alias-{path_index}-{binary_index}-{read_only}"
                        result = runner.run(repo=self.repo, prompt="Inspect this fixture", schema=self.schema,
                                            artifact_dir=artifacts, timeout=3, read_only=read_only)
                        self.assertEqual(result["entrypoint"], str(self.binary))


if __name__ == "__main__":
    unittest.main()
