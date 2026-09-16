"""Trusted execution configuration boundaries, without starting commands."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from anvil.config import RunConfig, WorkerConfig
from anvil.contracts import ContractError, Task
from anvil.execution import (MAX_ORIENTATION_BYTES, _review_prompt, _worker_prompt,
                             orientation_text)


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

    def test_automatic_skill_selection_is_opt_in_bounded_and_round_trips(self):
        self.assertIsNone(self.parse(configuration()).skill_selection)
        config = self.parse(configuration() | {"skill_selection": {"mode": "rules"}})
        self.assertEqual(config.skill_selection, {"mode": "rules", "max_skills": 2})
        self.assertEqual(self.parse(config.to_dict()), config)
        for value in (None, [], "rules", {}, {"mode": "model"},
                      {"mode": "rules", "unknown": True}):
            with self.subTest(value=value), self.assertRaises(ContractError):
                self.parse(configuration() | {"skill_selection": value})
        for maximum in (None, True, 0, 5, 1.0, "2"):
            with self.subTest(maximum=maximum), self.assertRaises(ContractError):
                self.parse(configuration() | {
                    "skill_selection": {"mode": "rules", "max_skills": maximum},
                })

    def test_worker_pool_keeps_reviewer_selection_and_defaults_process_limit(self):
        document = configuration() | {
            "agent": "claude-code", "agent_binary": "reviewer-claude",
            "workers": [{"id": "implementation", "agent": "codex"},
                        {"id": "tests", "agent": "claude-code"}],
        }
        config = self.parse(document)
        self.assertEqual((config.agent, config.executable), ("claude-code", "reviewer-claude"))
        self.assertEqual(config.workers, (WorkerConfig("implementation"),
                                         WorkerConfig("tests", "claude-code")))
        self.assertEqual([worker.executable for worker in config.workers], ["codex", "claude"])
        self.assertEqual(config.max_processes, 2)
        self.assertEqual(self.parse(config.to_dict()), config)
        document["workers"][0]["id"] = "changed"
        serialized = config.to_dict()
        serialized["workers"][0]["agent_binary"] = "changed"
        self.assertEqual(config.workers[0], WorkerConfig("implementation"))
        self.assertEqual(self.parse(configuration()).workers, ())
        self.assertIsNone(self.parse(configuration()).max_processes)
        self.assertNotIn("workers", self.parse(configuration()).to_dict())
        self.assertNotIn("max_processes", self.parse(configuration()).to_dict())

    def test_worker_paths_resolve_from_config_and_preserve_claude_alias(self):
        target = self.root / "tools" / "nested"
        target.mkdir(parents=True)
        (target.parent / "dispatch").write_text("intended executable")
        (self.root / "linked-tools").symlink_to(target, target_is_directory=True)
        alias = self.root / "claude-wrapper"
        alias.symlink_to(target.parent / "dispatch")
        document = configuration() | {"workers": [
            {"id": "codex", "agent": "codex", "agent_binary": "./claude-wrapper"},
            {"id": "claude", "agent": "claude-code", "agent_binary": "./claude-wrapper"},
            {"id": "traversal", "agent": "claude-code", "agent_binary": "./linked-tools/../dispatch"},
            {"id": "lookup", "agent": "claude-code", "agent_binary": "custom-claude"},
        ]}
        config_path = self.root / "run.json"
        config_path.write_text(json.dumps(document))
        config = RunConfig.load(config_path)
        self.assertEqual(config.workers[0].executable, str(target.parent / "dispatch"))
        self.assertEqual(config.workers[1].executable, str(alias))
        self.assertEqual(Path(config.workers[2].executable).read_text(), "intended executable")
        self.assertEqual(config.workers[3].executable, "custom-claude")
        self.assertEqual(RunConfig.from_document(config.to_dict(), base=Path("/elsewhere")), config)

    def test_worker_pool_rejects_malformed_slots_and_duplicate_ids(self):
        invalid = [None, "codex", {}, [], [{"id": "a"}], [{"agent": "codex"}],
                   [None], ["codex"], [{"id": "a", "agent": "codex", "unknown": True}],
                   [{"id": "a", "agent": "claude"}],
                   [{"id": "a", "agent": "codex", "codex_binary": "codex"}],
                   [{"id": "a", "agent": "codex"}, {"id": "a", "agent": "claude-code"}],
                   [{"id": str(index), "agent": "codex"} for index in range(9)]]
        for field, values in (("id", [None, True, [], "", " ", "a/b", "x" * 81, "a\n"]),
                              ("agent", [None, True, [], "", "Claude", "claude"]),
                              ("agent_binary", [None, True, [], "", " \n", "bad\0binary"])):
            invalid.extend([[{"id": "a", "agent": "codex"} | {field: value}] for value in values])
        for workers in invalid:
            with self.subTest(workers=workers), self.assertRaises(ContractError):
                self.parse(configuration() | {"workers": workers})

    def test_process_limits_are_bounded_strict_integers_and_require_workers(self):
        pool = configuration() | {"workers": [{"id": "one", "agent": "codex"}]}
        for limit in (None, False, True, 0, -1, 9, 1.0, "2", [], {}, 10**400):
            with self.subTest(limit=limit), self.assertRaises(ContractError):
                self.parse(pool | {"max_processes": limit})
        for limit in (1, 2, 8):
            with self.subTest(limit=limit):
                self.assertEqual(self.parse(pool | {"max_processes": limit}).max_processes, limit)
                with self.assertRaisesRegex(ContractError, "requires workers"):
                    self.parse(configuration() | {"max_processes": limit})
        maximum = self.parse(configuration() | {"workers": [
            {"id": str(index), "agent": "codex"} for index in range(8)
        ]})
        self.assertEqual(maximum.max_processes, 8)

    def test_direct_worker_pool_construction_validates_and_round_trips(self):
        paths = (self.root / "repo", self.root / "tickets.json", (("check",),), self.root / "state")
        workers = (WorkerConfig("codex-worker"), WorkerConfig("claude-worker", "claude-code"))
        config = RunConfig(*paths, workers=workers, max_processes=1)
        self.assertEqual(self.parse(config.to_dict()), config)
        for fields in ({"workers": [workers[0]]}, {"workers": ("codex",)},
                       {"workers": workers * 2}, {"workers": workers, "max_processes": True},
                       {"workers": workers, "max_processes": 0}, {"max_processes": 1}):
            with self.subTest(fields=fields), self.assertRaises(ContractError):
                RunConfig(*paths, **fields)
        for fields in ({"id": "../escape"}, {"id": "valid", "agent": "claude"},
                       {"id": "valid", "agent_binary": " \n"}):
            with self.subTest(fields=fields), self.assertRaises(ContractError):
                WorkerConfig(**fields)


if __name__ == "__main__":
    unittest.main()


class AgentTurnsAndOrientation(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.document = {"version": 1, "repo": ".", "tickets": "tickets.json",
                         "verification": [["true"]]}

    def config(self, **extra):
        return RunConfig.from_document({**self.document, **extra}, base=self.base)

    def test_defaults_are_absent(self):
        config = self.config()
        self.assertIsNone(config.agent_turns)
        self.assertIsNone(config.orientation)
        self.assertNotIn("agent_turns", config.to_dict())
        self.assertNotIn("orientation", config.to_dict())

    def test_agent_turns_round_trips(self):
        config = self.config(agent_turns=64)
        self.assertEqual(config.agent_turns, 64)
        self.assertEqual(config.to_dict()["agent_turns"], 64)

    def test_agent_turns_rejects_invalid(self):
        for value in (0, 1001, "64", 2.5, True, None):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    self.config(agent_turns=value)

    def test_orientation_resolves_and_round_trips(self):
        (self.base / "orient.md").write_text("map", encoding="utf-8")
        config = self.config(orientation="orient.md")
        self.assertEqual(config.orientation, self.base / "orient.md")
        self.assertEqual(config.to_dict()["orientation"], str(self.base / "orient.md"))


class OrientationText(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()

    def config(self, orientation):
        return RunConfig.from_document(
            {"version": 1, "repo": ".", "tickets": "tickets.json",
             "verification": [["true"]], **({} if orientation is None else {"orientation": orientation})},
            base=self.base)

    def test_absent_orientation_is_empty(self):
        self.assertEqual(orientation_text(self.config(None)), "")

    def test_reads_committed_text(self):
        (self.base / "orient.md").write_text("store.py owns the ledger.", encoding="utf-8")
        self.assertEqual(orientation_text(self.config("orient.md")), "store.py owns the ledger.")

    def test_rejects_oversized_missing_blank_and_non_utf8(self):
        (self.base / "big.md").write_text("x" * (MAX_ORIENTATION_BYTES + 1), encoding="utf-8")
        (self.base / "blank.md").write_text("   \n", encoding="utf-8")
        (self.base / "bad.md").write_bytes(b"\xff\xfe\x00")
        for name in ("big.md", "blank.md", "bad.md", "missing.md"):
            with self.subTest(name=name):
                with self.assertRaises(ContractError):
                    orientation_text(self.config(name))


class OrientationPrompt(unittest.TestCase):
    def task(self):
        return Task(id="t", title="T", objective="O", depends_on=(),
                    acceptance_criteria=("first",))

    def test_supplied_text_appears_before_the_ticket(self):
        prompt = _worker_prompt(self.task(), "a" * 40, orientation="MODULE MAP")
        self.assertIn("MODULE MAP", prompt)
        self.assertIn("supplied by the operator", prompt)
        self.assertLess(prompt.index("MODULE MAP"), prompt.index("Ticket:"))

    def test_absent_orientation_adds_nothing(self):
        prompt = _worker_prompt(self.task(), "a" * 40)
        self.assertNotIn("supplied by the operator", prompt)

    def test_review_receives_orientation_with_an_evidence_guard(self):
        prompt = _review_prompt(self.task(), "a" * 40, "b" * 40, {"summary": "done"},
                                orientation="MODULE MAP")
        self.assertIn("MODULE MAP", prompt)
        self.assertIn("navigation, not evidence", prompt)
        self.assertLess(prompt.index("MODULE MAP"), prompt.index("Ticket:"))

    def test_review_without_orientation_adds_nothing(self):
        prompt = _review_prompt(self.task(), "a" * 40, "b" * 40, {"summary": "done"})
        self.assertNotIn("navigation, not evidence", prompt)

    def test_orientation_does_not_displace_the_supplied_diff(self):
        prompt = _review_prompt(self.task(), "a" * 40, "b" * 40, {"summary": "done"},
                                supplied_diff="DIFF BODY", orientation="MODULE MAP")
        self.assertTrue(prompt.rstrip().endswith("DIFF BODY"))
        self.assertLess(prompt.index("MODULE MAP"), prompt.index("DIFF BODY"))
