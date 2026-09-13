"""Create an opt-in live agent exercise; this script never starts an agent.

Usage: python tests/create_live_fixture.py /absolute/new/directory [--agent claude-code]
Then run the printed configuration using anvil run. Model usage is intentional
and separate from the ordinary unittest suite.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def create(destination: Path, *, agent: str = "codex") -> Path:
    if agent not in ("codex", "claude-code"):
        raise ValueError("agent must be codex or claude-code")
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    repo = destination / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "AGENTS.md").write_text(
        "# Fixture instructions\n\nUse Python's standard library only. "
        "Add unittest coverage for public behavior and retain existing tests. "
        "The implementation lives in labels.py. Run python3 -m unittest discover -s tests -v. "
        "Use ASCII Unicode escape sequences in Python literals when exact non-ASCII code points "
        "matter to a test; visually similar glyphs must not change the intended input. "
        "Do not introduce dependencies, background services, or unrelated changes.\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (repo / "labels.py").write_text('"""Small label helpers, built one ticket at a time."""\n\nVERSION = "0.0.0"\n', encoding="utf-8")
    (repo / "tests/test_baseline.py").write_text(
        "import unittest\nimport labels\n\nclass BaselineTests(unittest.TestCase):\n"
        "    def test_version(self):\n        self.assertEqual(labels.VERSION, '0.0.0')\n",
        encoding="utf-8",
    )
    tasks = [
        {"id": "slugify", "title": "Normalize a label into a slug",
         "objective": "Add slugify(text) to labels.py and cover its public behavior with unittest.",
         "depends_on": [], "acceptance_criteria": [
             "slugify accepts a string, lowercases it, and joins ASCII letter/digit runs with a single hyphen; '  Hello, World!  ' becomes 'hello-world' and 'A__B 42' becomes 'a-b-42'. Every non-ASCII character is a separator; do not transliterate.",
             "Raise ValueError when the string has no ASCII letters or digits, and TypeError for every non-string input.",
             "Tests cover normalization, already normalized text, empty/punctuation-only inputs, and non-string inputs; existing baseline tests continue to pass.",
         ]},
        {"id": "slug-map", "title": "Build a collision-checked map of labels",
         "objective": "Add build_slug_map(labels) in labels.py using the accepted slugify function.",
         "depends_on": ["slugify"], "acceptance_criteria": [
             "Accept a list of strings and return a dictionary from each original label to its slug; an empty list returns {}. Raise TypeError for non-list inputs and use slugify to validate each member.",
             "Raise ValueError for any repeated slug, including a repeated identical label; ['Hello World', 'hello-world'] must fail. Preserve original input order in the resulting dictionary.",
             "Tests cover successful mapping, empty input, collisions including repeated identical labels, invalid collection/member inputs, and previously implemented slugify behavior.",
         ]},
    ]
    tickets = destination / "tickets.json"
    tickets.write_text(json.dumps({"version": 1, "tasks": tasks}, indent=2) + "\n", encoding="utf-8")
    config = destination / "run.json"
    config.write_text(json.dumps({
        "version": 1, "repo": "./repo", "tickets": "./tickets.json", "state_dir": "./runs",
        "agent": agent,
        "verification": [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]],
        "agent_timeout": 300, "check_timeout": 30,
    }, indent=2) + "\n", encoding="utf-8")
    commands = [
        ["init", "-q", "-b", "main"], ["add", "."],
        ["-c", "user.name=Anvil Fixture", "-c", "user.email=anvil@example.invalid",
         "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "commit", "-qm", "Baseline fixture"],
    ]
    for command in commands:
        subprocess.run(["git", "-C", str(repo), *command], check=True, timeout=30)
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--agent", choices=("codex", "claude-code"), default="codex")
    arguments = parser.parse_args()
    print(create(arguments.destination, agent=arguments.agent))
