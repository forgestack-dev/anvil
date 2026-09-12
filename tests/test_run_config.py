"""Trusted execution configuration boundaries, without starting commands."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anvil.config import RunConfig
from anvil.contracts import ContractError


def configuration() -> dict:
    return {"version": 1, "repo": "target repo", "tickets": "tickets.json",
            "verification": [["check", "--all"]], "state_dir": "run state"}


class RunConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def parse(self, document):
        return RunConfig.from_document(document, base=self.root)

    def test_paths_resolve_from_configuration_file_and_loading_does_not_execute(self):
        folder = self.root / "configuration"
        folder.mkdir()
        path = folder / "run.json"
        document = configuration() | {
            "repo": "../target repo", "tickets": "input/tickets.json",
            "state_dir": "../saved runs", "codex_binary": "./bin/custom codex",
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        before = sorted(str(item.relative_to(self.root)) for item in self.root.rglob("*"))
        with patch("subprocess.Popen", side_effect=AssertionError("configuration started a command")):
            config = RunConfig.load(path)
        self.assertEqual(config.repo, self.root / "target repo")
        self.assertEqual(config.tickets, folder / "input/tickets.json")
        self.assertEqual(config.state_dir, self.root / "saved runs")
        self.assertEqual(config.codex_binary, str(folder / "bin/custom codex"))
        self.assertEqual(sorted(str(item.relative_to(self.root)) for item in self.root.rglob("*")), before)

    def test_trusted_argument_arrays_preserve_literal_values_without_shell_parsing(self):
        arguments = ["test tool", "$(touch never)", "`uname`", "--name=a b", "$TOKEN", ";", "line\nbreak"]
        document = configuration() | {"verification": [arguments.copy(), ["sh", "-c", "printf 'ok'"]]}
        with patch("subprocess.Popen", side_effect=AssertionError("configuration started a command")):
            config = self.parse(document)
        self.assertEqual(config.verification[0], tuple(arguments))
        self.assertEqual(config.verification[1], ("sh", "-c", "printf 'ok'"))
        document["verification"][0][1] = "changed after validation"
        self.assertEqual(config.verification[0][1], "$(touch never)")
        self.assertEqual(RunConfig.from_document(config.to_dict(), base=Path("/different")), config)

    def test_defaults_and_absolute_home_and_path_lookup_executables(self):
        document = configuration()
        del document["state_dir"]
        config = self.parse(document)
        self.assertEqual(config.state_dir, (Path.home() / ".local/state/anvil").resolve())
        self.assertEqual((config.agent_timeout, config.check_timeout), (900.0, 300.0))
        self.assertEqual(config.codex_binary, "codex")
        for binary, expected in (
            ("codex-nightly", "codex-nightly"),
            (str(self.root / "absolute tool"), str(self.root / "absolute tool")),
            ("~/bin/codex", str((Path.home() / "bin/codex").resolve())),
        ):
            with self.subTest(binary=binary):
                self.assertEqual(self.parse(document | {"codex_binary": binary}).codex_binary, expected)

    def test_malformed_shapes_missing_fields_and_unknown_options_are_rejected(self):
        documents = [None, [], "run", {}, configuration() | {"untrusted_option": True}]
        for field in ("version", "repo", "tickets", "verification"):
            missing = configuration()
            del missing[field]
            documents.append(missing)
        for document in documents:
            with self.subTest(document=document), self.assertRaises(ContractError):
                self.parse(document)
        for version in (True, 1.0, "1", 0, 2, None):
            with self.subTest(version=version), self.assertRaisesRegex(ContractError, "integer 1"):
                self.parse(configuration() | {"version": version})

    def test_path_and_binary_values_must_be_nonblank_strings_without_nul(self):
        for field in ("repo", "tickets", "state_dir", "codex_binary"):
            for value in (None, 1, True, [], "", " \n", "invalid\0path"):
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    self.parse(configuration() | {field: value})

    def test_commands_must_be_nonempty_argument_arrays(self):
        values = [None, "check --all", [], ["check --all"], [[]],
                  [["check", ""]], [["check", None]], [["check", True]],
                  [["check", 42]], [["check", "bad\0argument"]], [["check"], "second"]]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.parse(configuration() | {"verification": value})

    def test_timeouts_reject_wrong_types_nonfinite_out_of_bounds_and_huge_integers(self):
        invalid = [None, True, False, "10", [], {}, 0, -1, 3600.001,
                   float("nan"), float("inf"), float("-inf"), 10**400]
        for field in ("agent_timeout", "check_timeout"):
            for value in invalid:
                with self.subTest(field=field, value=value), self.assertRaises(ContractError):
                    self.parse(configuration() | {field: value})
            for value in (0.001, 1, 3600):
                with self.subTest(field=field, accepted=value):
                    self.assertEqual(getattr(self.parse(configuration() | {field: value}), field), float(value))

    def test_file_loading_rejects_ambiguous_invalid_and_unreadable_json(self):
        path = self.root / "run.json"
        for raw in ("{", '{"version": 1, "version": 1}', '{"agent_timeout": NaN}',
                    '{"version": ' + "9" * 5000 + "}", "[" * 10000 + "]" * 10000):
            with self.subTest(size=len(raw)):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ContractError):
                    RunConfig.load(path)
        path.write_bytes(b"\xff")
        with self.assertRaises(ContractError):
            RunConfig.load(path)
        with self.assertRaises(ContractError):
            RunConfig.load(self.root / "missing.json")
        with self.assertRaises(ContractError):
            RunConfig.load(self.root)

    def test_serialized_configuration_is_independent_of_caller_mutations(self):
        document = configuration()
        original = deepcopy(document)
        config = self.parse(document)
        serialized = config.to_dict()
        serialized["verification"][0].append("changed")
        self.assertEqual(config.verification, (("check", "--all"),))
        self.assertEqual(document, original)


if __name__ == "__main__":
    unittest.main()
