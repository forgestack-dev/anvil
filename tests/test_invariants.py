"""Whole-repository invariants from AGENTS.md, checked statically.

These belong to no single area: they read the CLI, the README, the entry
skill and the schemas together. Keep this module fast and purely static so
it never needs a place in SLOW_FIRST in tools/run-tests.py.
"""

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
