"""Tests for the managed environment builder's exclusion set."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from anvil.environment import managed_environment


BASE = {"PATH": "/usr/bin", "HOME": "/home/user", "SECRET_TOKEN": "s3cret",
        "GIT_AUTHOR_NAME": "someone"}


class ManagedEnvironmentTests(unittest.TestCase):
    def test_no_exclusion_matches_current_behavior(self):
        with patch.dict(os.environ, BASE, clear=True):
            without_argument = managed_environment()
            with_none = managed_environment(None)
            with_empty = managed_environment([])

        expected = {"PATH": "/usr/bin", "HOME": "/home/user", "SECRET_TOKEN": "s3cret"}
        self.assertEqual(without_argument, expected)
        self.assertEqual(with_none, expected)
        self.assertEqual(with_empty, expected)

    def test_excludes_present_variable(self):
        with patch.dict(os.environ, BASE, clear=True):
            result = managed_environment(exclude={"SECRET_TOKEN"})

        self.assertNotIn("SECRET_TOKEN", result)
        self.assertEqual(result, {"PATH": "/usr/bin", "HOME": "/home/user"})

    def test_naming_absent_variable_is_a_no_op(self):
        with patch.dict(os.environ, BASE, clear=True):
            result = managed_environment(exclude={"NOT_PRESENT_AT_ALL"})

        self.assertEqual(result, {"PATH": "/usr/bin", "HOME": "/home/user", "SECRET_TOKEN": "s3cret"})

    def test_exclusion_is_case_sensitive(self):
        with patch.dict(os.environ, BASE, clear=True):
            result = managed_environment(exclude={"secret_token"})

        self.assertIn("SECRET_TOKEN", result)
        self.assertEqual(result["SECRET_TOKEN"], "s3cret")

    def test_git_prefixed_variables_are_always_dropped_regardless_of_exclusion(self):
        with patch.dict(os.environ, BASE, clear=True):
            result = managed_environment(exclude={"PATH"})

        self.assertNotIn("GIT_AUTHOR_NAME", result)
        self.assertNotIn("PATH", result)
        self.assertEqual(result, {"HOME": "/home/user", "SECRET_TOKEN": "s3cret"})


if __name__ == "__main__":
    unittest.main()


class CredentialExclusionReachesEverySubprocess(unittest.TestCase):
    """The configured names must not reach any subprocess a run launches."""

    SECRET = "ANVIL_TEST_CREDENTIAL"
    VALUE = "leaked-value-should-never-appear"

    def setUp(self):
        self.patch = patch.dict(os.environ, {self.SECRET: self.VALUE}, clear=False)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def config(self, root):
        from anvil.config import RunConfig
        tickets = root / "tickets.json"
        tickets.write_text(json.dumps({"version": 1, "tasks": [
            {"id": "t", "title": "T", "objective": "O", "depends_on": [],
             "acceptance_criteria": ["a"]}]}))
        return RunConfig(root, tickets, (("true",),), root / "state",
                         credential_exclusion=(self.SECRET,))

    def test_runner_factories_withhold_the_value_from_both_agents(self):
        from anvil.adapters import create_runner
        for agent, binary in (("claude-code", "claude"), ("codex", "codex")):
            with self.subTest(agent=agent):
                runner = create_runner(agent, binary, exclude=(self.SECRET,))
                self.assertEqual(runner.exclude, (self.SECRET,))
                self.assertNotIn(self.SECRET, managed_environment(runner.exclude))

    def test_verification_commands_never_see_the_value(self):
        from anvil.execution import verify
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            probe = root / "seen.txt"
            config = self.config(root)
            config = replace(config, verification=((
                sys.executable, "-c",
                f"import os,pathlib;pathlib.Path({str(probe)!r}).write_text("
                f"os.environ.get({self.SECRET!r}, 'ABSENT'))"),))
            verify(config, root, root / "artifacts")
            self.assertEqual(probe.read_text(), "ABSENT")

    def test_preflight_probes_never_see_the_value(self):
        from anvil.routing import preflight
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            fake = root / "fake-agent"
            probe = root / "seen.txt"
            # preflight launches the probe more than once. Append rather than
            # overwrite, so a leak in any single launch stays visible instead of
            # being masked by a later one.
            fake.write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "${{{self.SECRET}:-ABSENT}}" >> {probe}\n'
                'echo "--model --effort --max-budget-usd"\n'
                'echo "2.1.260 (Claude Code)"\n')
            fake.chmod(0o755)
            try:
                preflight("claude-code", str(fake), None, exclude=(self.SECRET,))
            except Exception:
                pass  # The probe's own contract is not what this test asserts.
            self.assertTrue(probe.exists(), "preflight never launched the probe")
            seen = probe.read_text().split()
            self.assertTrue(seen, "preflight never launched the probe")
            self.assertEqual(set(seen), {"ABSENT"}, f"a probe launch saw the value: {seen}")

    def test_the_value_is_absent_from_argv_and_recorded_artifacts(self):
        from anvil.adapters.claude import build_invocation
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            (root / ".git").mkdir()
            invocation = build_invocation(root, "implement the ticket", {"type": "object"},
                                          "claude", turns=8)
            self.assertNotIn(self.VALUE, " ".join(invocation.argv))
            self.assertNotIn(self.SECRET, " ".join(invocation.argv))
            self.assertNotIn(self.VALUE, invocation.stdin)

    def test_the_frozen_configuration_records_names_and_never_values(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            recorded = json.dumps(self.config(root).to_dict())
            self.assertIn(self.SECRET, recorded)
            self.assertNotIn(self.VALUE, recorded)
