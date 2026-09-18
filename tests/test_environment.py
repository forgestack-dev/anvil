"""The managed environment builder, and the closure of its launch sites."""

from __future__ import annotations

import ast
from dataclasses import replace
import json
import subprocess
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_execution
from anvil.environment import managed_environment
from anvil.execution import run_serial


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

    def test_git_descendants_never_see_the_value(self):
        import subprocess
        from anvil.workspaces import Repository
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            probe = root / "seen.txt"
            script = root / "probe.sh"
            # A `!` alias runs as a child of Git itself, so it observes exactly
            # what Git handed down rather than what this process holds.
            script.write_text(f'#!/bin/sh\nprintf "%s\\n" "${{{self.SECRET}:-ABSENT}}" >> {probe}\n')
            script.chmod(0o755)
            for command in (("init", "-q"), ("-c", "user.name=T", "-c", "user.email=t@t.invalid",
                                             "commit", "-qm", "base", "--allow-empty")):
                subprocess.run(["git", *command], cwd=root, check=True,
                               capture_output=True)
            repository = Repository(root, exclude=(self.SECRET,))
            repository.git("-c", f"alias.probe=!{script}", "probe")
            self.assertEqual(probe.read_text().split(), ["ABSENT"])

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


# AGENTS.md: "A configured `credential_exclusion` reaches every process a run
# starts, not only its agent turns ... Enumerate the launch sites when adding
# one; run `51fec4cd` records what an unlisted site costs."
INVARIANT = ("AGENTS.md: a configured credential_exclusion reaches every process a run "
             "starts, not only its agent turns. Enumerate the launch sites when adding one; "
             "run 51fec4cd records what an unlisted site costs.")
CONFIGURED = "the run configuration's credential_exclusion"
ADAPTER = "an adapter's exclude attribute"
ABSENT = "the documented absence of a run configuration"
SINK = "the sink every other site forwards through, not a launch site"
SOURCES = (CONFIGURED, ADAPTER, ABSENT)

#: Every process launch in src/anvil/, keyed by module, enclosing qualified name
#: and callee. Keying on the enclosing function rather than a line number keeps
#: ordinary edits from churning this table; two launches may share a key, so the
#: value carries a count alongside the exclusion source each one draws from.
LAUNCH_SITES = {
    ("adapters/claude.py", "ClaudeRunner.run", "run_process"): (1, ADAPTER),
    ("adapters/claude.py", "doctor.run_probe", "run_process"): (1, CONFIGURED),
    ("adapters/codex.py", "CodexRunner.run", "run_process"): (1, ADAPTER),
    ("adapters/codex.py", "doctor", "subprocess.run"): (2, CONFIGURED),
    ("execution.py", "verify", "run_process"): (1, CONFIGURED),
    ("processes.py", "run_process", "subprocess.Popen"): (1, SINK),
    ("routing.py", "preflight", "run_process"): (2, CONFIGURED),
    ("skill_management.py", "scope_for", "run_process"): (1, ABSENT),
    ("workspaces.py", "Repository.git", "run_process"): (1, CONFIGURED),
}

_SUBPROCESS_LAUNCHERS = {"Popen", "run", "call", "check_call", "check_output"}


def _callee(node: ast.Call) -> str | None:
    """The launcher this call names, or None if it launches no process."""
    function = node.func
    if isinstance(function, ast.Name) and function.id == "run_process":
        return "run_process"
    if (isinstance(function, ast.Attribute) and function.attr in _SUBPROCESS_LAUNCHERS
            and isinstance(function.value, ast.Name) and function.value.id == "subprocess"):
        return f"subprocess.{function.attr}"
    return None


def launch_sites(root: Path) -> dict[tuple[str, str, str], list[str | None]]:
    """Every process launch under root, mapped to each site's env expression.

    Covers subprocess directly as well as run_process: the two probes in the
    Codex adapter never pass through run_process, so a check written only
    against it would miss exactly the kind of site this exists to catch.
    """
    found: dict[tuple[str, str, str], list[str | None]] = {}
    for path in sorted(root.rglob("*.py")):
        enclosing: list[str] = []
        module = str(path.relative_to(root))

        class Visitor(ast.NodeVisitor):
            def _scoped(self, node):
                enclosing.append(node.name)
                self.generic_visit(node)
                enclosing.pop()

            visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

            def visit_Call(self, node):
                callee = _callee(node)
                if callee is not None:
                    environment = next((ast.unparse(keyword.value) for keyword in node.keywords
                                        if keyword.arg == "env"), None)
                    found.setdefault((module, ".".join(enclosing), callee), []).append(environment)
                self.generic_visit(node)

        Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


class LaunchSiteRegistry(unittest.TestCase):
    """Close the set of process launches so a new one cannot pass unnoticed.

    tests/test_environment.py already proves the configured exclusion reaches
    each launch that exists. It cannot fail when a new one appears, which is the
    shape of the failure run 51fec4cd recorded.
    """

    @classmethod
    def setUpClass(cls):
        cls.found = launch_sites(Path(__file__).resolve().parents[1] / "src" / "anvil")

    def test_no_launch_site_leaves_its_environment_to_inheritance(self):
        bare = sorted(f"{module}:{enclosing} -> {callee}"
                      for (module, enclosing, callee), envs in self.found.items()
                      if any(env is None for env in envs))
        self.assertEqual(bare, [], "\n".join([
            "these launches name no env, so they inherit the host environment:",
            *bare, INVARIANT]))

    def test_the_launch_sites_are_exactly_the_registered_ones(self):
        counted = {key: len(envs) for key, envs in self.found.items()}
        registered = {key: count for key, (count, _) in LAUNCH_SITES.items()}
        if counted == registered:
            return
        added = {key: counted[key] for key in counted.keys() - registered.keys()}
        removed = sorted(registered.keys() - counted.keys())
        changed = {key: (registered[key], counted[key]) for key in counted.keys() & registered.keys()
                   if counted[key] != registered[key]}
        self.fail("\n".join([
            "the set of process launch sites no longer matches LAUNCH_SITES.",
            *(f"  new: {key} ({count} call(s))" for key, count in sorted(added.items())),
            *(f"  gone: {key}" for key in removed),
            *(f"  count changed: {key} registered {was}, found {now}"
              for key, (was, now) in sorted(changed.items())),
            "",
            "Register each new site with the exclusion source it draws from, one of:",
            *(f"  - {source}" for source in SOURCES),
            "",
            INVARIANT]))

    def test_every_registered_site_names_a_legal_exclusion_source(self):
        for key, (_, source) in LAUNCH_SITES.items():
            with self.subTest(site=key):
                self.assertIn(source, (*SOURCES, SINK))
        sinks = [key for key, (_, source) in LAUNCH_SITES.items() if source == SINK]
        self.assertEqual(sinks, [("processes.py", "run_process", "subprocess.Popen")],
                         "run_process is the one sink; every other site must name a source")


class CanaryReachesNoLaunchedProcess(unittest.TestCase):
    """What a run actually launches, however each site is spelled.

    The registry above proves every written site was reviewed. It cannot prove
    the reviewed expression is right: `env=os.environ` names an environment and
    passes it. This drives one real run with `subprocess.Popen` wrapped and
    inspects what every launch was actually given, which reaches the supervisor's
    own Git commands for free. Neither test subsumes the other.
    """

    setUp = test_execution.SerialExecutionTests.setUp
    SECRET = "ANVIL_TEST_CANARY"
    VALUE = "canary-must-never-be-inherited"

    #: Only launches whose call stack passes through src/anvil/ are the run's.
    #: The fake agent stands in for a CLI that Anvil would launch with a
    #: filtered environment, but it runs in this process, so the Git commands it
    #: issues are children of the test rather than of a launch Anvil controls.
    #: Counting them would measure the harness. A real agent's descendants
    #: inherit the filtered environment of the launch that started it.
    ANVIL = str(Path(__file__).resolve().parents[1] / "src" / "anvil")

    @classmethod
    def _started_by_anvil(cls) -> bool:
        """Whether the code that reached for a process is Anvil's own.

        The nearest caller decides, not the whole stack: Anvil drives the fake
        agent, so its frames sit under every launch either way. Stdlib
        subprocess frames are skipped because run and check_output reach Popen
        through them.
        """
        frame = sys._getframe(2)
        while frame is not None and frame.f_code.co_filename == subprocess.__file__:
            frame = frame.f_back
        return frame is not None and frame.f_code.co_filename.startswith(cls.ANVIL)

    def test_no_process_a_run_launches_receives_the_canary(self):
        given, foreign = [], []
        launch = subprocess.Popen

        def record(*args, **kwargs):
            (given if self._started_by_anvil() else foreign).append(kwargs.get("env"))
            return launch(*args, **kwargs)

        config = replace(self.config, credential_exclusion=(self.SECRET,))
        with patch.dict(os.environ, {self.SECRET: self.VALUE}, clear=False), \
                patch.object(subprocess, "Popen", record):
            result = run_serial(config, runner=test_execution.FakeRunner())

        self.assertEqual(result["status"], "success", result.get("error"))
        self.assertGreater(len(given), 5, "the run launched too little to prove anything")
        self.assertTrue(foreign, "the stack filter matched everything; it would hide a leak")
        inherited = sum(1 for environment in given if environment is None)
        self.assertEqual(inherited, 0,
                         f"{inherited} of {len(given)} launches passed env=None and inherited "
                         f"the whole host environment, including {self.SECRET}")
        # Count only. Reporting the environments themselves would print every
        # value on the host into a failure log, which is the thing this test
        # exists to prevent.
        leaked = sum(1 for environment in given if self.SECRET in environment)
        self.assertEqual(leaked, 0,
                         f"{leaked} of {len(given)} launches received {self.SECRET}; "
                         "an env expression can be reviewed and still be wrong")
