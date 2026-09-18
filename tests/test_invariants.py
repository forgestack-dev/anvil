"""Whole-repository invariants from AGENTS.md, checked statically.

These belong to no single area: they read the CLI, the README, the entry
skill and the schemas together. Keep this module fast and purely static so
it never needs a place in SLOW_FIRST in tools/run-tests.py.
"""

import ast
from pathlib import Path
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
