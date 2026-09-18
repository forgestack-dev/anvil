"""Whole-repository invariants from AGENTS.md, checked statically.

These belong to no single area: they read the CLI, the README, the entry
skill and the schemas together. Keep this module fast and purely static so
it never needs a place in SLOW_FIRST in tools/run-tests.py.
"""

import ast
from pathlib import Path
import unittest

from anvil.cli import SUBCOMMANDS


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
