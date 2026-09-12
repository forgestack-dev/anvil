"""Trusted execution configuration boundaries, without starting commands."""

from copy import deepcopy
import json
import os
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
        self.assertEqual((config.agent, config.executable), ("codex", "codex"))
        for binary, expected in (
            ("codex-nightly", "codex-nightly"),
            (str(self.root / "absolute tool"), str(self.root / "absolute tool")),
            ("~/bin/codex", str((Path.home() / "bin/codex").resolve())),
        ):
            with self.subTest(binary=binary):
                self.assertEqual(self.parse(document | {"codex_binary": binary}).codex_binary, expected)

    def test_agent_selection_defaults_and_custom_executables_round_trip(self):
        for agent, default_binary in (("codex", "codex"), ("claude-code", "claude")):
            with self.subTest(agent=agent, default=True):
                config = self.parse(configuration() | {"agent": agent})
                self.assertEqual((config.agent, config.executable), (agent, default_binary))
                self.assertEqual(config.agent_binary, default_binary)
                self.assertEqual(self.parse(config.to_dict()), config)
            for binary, expected in (
                ("custom agent", "custom agent"),
                ("./bin/custom agent", str(self.root / "bin/custom agent")),
                ("~/bin/custom-agent", str((Path.home() / "bin/custom-agent").resolve())),
                (str(self.root / "absolute agent"), str(self.root / "absolute agent")),
            ):
                with self.subTest(agent=agent, binary=binary):
                    config = self.parse(configuration() | {"agent": agent, "agent_binary": binary})
                    self.assertEqual(config.executable, expected)
                    self.assertEqual(config.to_dict()["agent_binary"], expected)
                    self.assertNotIn("codex_binary", config.to_dict())
                    self.assertEqual(RunConfig.from_document(config.to_dict(), base=Path("/elsewhere")), config)
                    if agent == "codex":
                        self.assertEqual(config.codex_binary, expected)

    def test_legacy_json_and_positional_constructors_preserve_codex_executable(self):
        parsed = self.parse(configuration() | {"codex_binary": "codex-nightly"})
        direct = RunConfig(parsed.repo, parsed.tickets, parsed.verification, parsed.state_dir,
                           "codex-nightly", 900, 300)
        self.assertEqual(direct, parsed)
        self.assertEqual((direct.agent, direct.executable, direct.codex_binary),
                         ("codex", "codex-nightly", "codex-nightly"))
        self.assertEqual(direct.to_dict()["agent_binary"], "codex-nightly")
        self.assertNotIn("codex_binary", direct.to_dict())
        self.assertEqual(self.parse(direct.to_dict()), direct)

    @unittest.skipUnless(os.name == "posix", "POSIX executable symlinks")
    def test_claude_executable_alias_survives_loading_and_serialization(self):
        shim = self.root / "dispatch-shim"
        shim.write_text("shared executable")
        alias = self.root / "claude-wrapper"
        alias.symlink_to(shim.name)
        config_path = self.root / "run.json"
        for binary in ("./claude-wrapper", str(alias)):
            with self.subTest(binary=binary):
                config_path.write_text(json.dumps(configuration() | {
                    "agent": "claude-code", "agent_binary": binary,
                }))
                config = RunConfig.load(config_path)
                self.assertEqual(config.executable, str(alias))
                self.assertEqual(Path(config.executable).read_text(), shim.read_text())
                self.assertEqual(RunConfig.from_document(config.to_dict(), base=Path("/elsewhere")), config)
        # Preserve the existing Codex path contract.
        codex = self.parse(configuration() | {"codex_binary": str(alias)})
        self.assertEqual(codex.executable, str(shim))

    @unittest.skipUnless(os.name == "posix", "POSIX executable symlinks")
    def test_claude_executable_parent_traversal_keeps_filesystem_meaning(self):
        target = self.root / "tools" / "nested"
        target.mkdir(parents=True)
        (target.parent / "claude").write_text("intended executable")
        (self.root / "claude").write_text("different executable")
        (self.root / "linked-tools").symlink_to(target, target_is_directory=True)
        config = self.parse(configuration() | {
            "agent": "claude-code", "agent_binary": "./linked-tools/../claude",
        })
        self.assertEqual(Path(config.executable).read_text(), "intended executable")
        self.assertEqual(RunConfig.from_document(config.to_dict(), base=Path("/elsewhere")), config)

    def test_direct_constructors_support_agent_selection_and_generic_override(self):
        paths = (self.root / "repo", self.root / "tickets.json", (("check",),), self.root / "state")
        claude = RunConfig(*paths, agent="claude-code")
        self.assertEqual((claude.executable, claude.agent_binary), ("claude", "claude"))
        self.assertEqual(self.parse(claude.to_dict()), claude)
        codex = RunConfig(*paths, agent_binary="custom-codex")
        self.assertEqual(codex.codex_binary, "custom-codex")
        self.assertEqual(self.parse(codex.to_dict()), codex)
        with self.assertRaises(ContractError):
            RunConfig(*paths, codex_binary="custom-codex", agent="claude-code")
        with self.assertRaises(ContractError):
            RunConfig(*paths, codex_binary="old-codex", agent_binary="new-codex")

    def test_agent_names_reject_unsupported_and_nonstring_values(self):
        for agent in ("claude", "Codex", "", " ", None, True, 1, [], {}):
            with self.subTest(agent=agent), self.assertRaisesRegex(ContractError, "agent must be"):
                self.parse(configuration() | {"agent": agent})

    def test_legacy_binary_is_rejected_for_claude_and_aliases_cannot_be_combined(self):
        for legacy_binary in ("codex", "custom-codex"):
            with self.subTest(legacy_binary=legacy_binary), self.assertRaisesRegex(ContractError, "only supported"):
                self.parse(configuration() | {"agent": "claude-code", "codex_binary": legacy_binary})
        for agent_selection in ({}, {"agent": "codex"}, {"agent": "claude-code"}):
            for generic_binary in ("codex", "custom-agent"):
                with self.subTest(agent_selection=agent_selection, binary=generic_binary):
                    with self.assertRaisesRegex(ContractError, "not both"):
                        self.parse(configuration() | agent_selection | {
                            "codex_binary": "codex", "agent_binary": generic_binary,
                        })

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
        for field in ("repo", "tickets", "state_dir", "codex_binary", "agent_binary"):
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
