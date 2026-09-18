"""Whole-repository invariants from AGENTS.md, checked statically.

These belong to no single area: they read the CLI, the README, the entry
skill and the schemas together. Keep this module fast and purely static so
it never needs a place in SLOW_FIRST in tools/run-tests.py.
"""

import ast
import importlib
from pathlib import Path
import re
import sys
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


class StandardLibraryOnly(unittest.TestCase):
    """AGENTS.md: use the standard library unless a dependency has a concrete
    benefit.

    Making that checkable turns adding a dependency into a deliberate act: it
    means editing ALLOWED below, next to a comment naming the benefit, rather
    than an import passing unremarked in review.
    """

    #: Distribution roots src/anvil/ may import beyond the standard library.
    #: Each entry needs a comment naming the concrete benefit it buys.
    ALLOWED: frozenset[str] = frozenset()

    def test_no_module_imports_beyond_the_standard_library(self):
        source = Path(__file__).resolve().parents[1] / "src" / "anvil"
        outside = {}
        for path in sorted(source.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    if (root not in sys.stdlib_module_names and root != "anvil"
                            and root not in self.ALLOWED):
                        outside.setdefault(root, set()).add(
                            f"{path.relative_to(source)}:{node.lineno}")
        self.assertEqual(outside, {}, "\n".join([
            "these imports are neither standard library nor anvil:",
            *(f"  {root}: {', '.join(sorted(where))}" for root, where in sorted(outside.items())),
            "",
            "If the dependency is deliberate, add its distribution root to ALLOWED",
            "with a comment naming the concrete benefit. AGENTS.md: use Python",
            "3.11+ and the standard library unless a dependency has a concrete benefit."]))


class EnforcedClaimsNameRealTests(unittest.TestCase):
    """Every marker in AGENTS.md must name a test that exists and is collected.

    A claim with no marker is understood to be unenforced, which is accurate
    rather than embarrassing: section 10 of docs/INVARIANT_TESTS.md lists the
    claims that must never acquire one, because a test for them would be vacuous
    or would freeze one reading of a judgment call. What this forbids is the
    opposite failure -- a marker that points at a test which was renamed,
    deleted, or never written, so the document claims an enforcement it does not
    have.
    """

    #: `[test_module.py::Class]`, or `::Class::test_method` where a whole class
    #: is not dedicated to the one claim.
    MARKER = re.compile(r"\[(test_[a-z_0-9]+)\.py::([A-Za-z_][A-Za-z_0-9]*)"
                        r"(?:::([a-z_][A-Za-z_0-9]*))?\]")
    ROOT = Path(__file__).resolve().parents[1]

    def markers(self):
        return self.MARKER.findall((self.ROOT / "AGENTS.md").read_text(encoding="utf-8"))

    def test_agents_md_marks_at_least_the_claims_this_milestone_enforced(self):
        found = self.markers()
        self.assertGreaterEqual(len(found), 9,
                                "markers disappeared from AGENTS.md; enforcement is now invisible")
        self.assertEqual(len(set(found)), len(found), "the same enforcer is cited twice")

    def test_every_marker_names_a_test_that_exists_and_is_collected(self):
        import unittest as framework
        missing = []
        for module, name, method in self.markers():
            if not (self.ROOT / "tests" / f"{module}.py").is_file():
                missing.append(f"{module}.py does not exist")
                continue
            reference = ".".join(part for part in (module, name, method) if part)
            # Resolve explicitly rather than through loadTestsFromName: for a
            # name it cannot find, the loader returns a suite holding a
            # _FailedTest instead of raising, so a marker citing a renamed class
            # would look like one collected test and pass.
            try:
                imported = importlib.import_module(module)
            except Exception as exc:
                missing.append(f"{reference}: {type(exc).__name__} importing {module}")
                continue
            case = getattr(imported, name, None)
            if not (isinstance(case, type) and issubclass(case, framework.TestCase)):
                missing.append(f"{reference}: {name} is not a TestCase in {module}.py")
                continue
            if method and not callable(getattr(case, method, None)):
                missing.append(f"{reference}: {name} has no test named {method}")
                continue
            collected = framework.defaultTestLoader.loadTestsFromName(reference)
            if collected.countTestCases() == 0:
                missing.append(f"{reference}: resolves but the runner collects nothing")
        self.assertEqual(missing, [], "\n".join([
            "AGENTS.md cites enforcers that do not resolve:",
            *(f"  {item}" for item in missing),
            "",
            "Either restore the test or drop the marker. An unmarked claim is honest;",
            "a marker naming nothing claims an enforcement the suite does not provide."]))
