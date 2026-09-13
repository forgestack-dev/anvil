"""Pinned upstream discovery without network access or upstream execution."""

import gzip
from io import BytesIO
import json
import tarfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from anvil.skill_source import SourceError, SourceFile, fetch_catalog
import anvil.skill_source as source


REVISION = "a" * 40
LICENSE = b"MIT License\nCopyright (c) 2026 Matt Pocock\nfixture permission notice\n"


def skill(path, name=None):
    name = name or path.rsplit("/", 1)[-1]
    return (path + "/SKILL.md", f"---\nname: {name}\ndescription: Fixture\n---\nUnexecuted instructions.\n".encode())


def entries():
    return [("LICENSE", LICENSE), skill("skills/engineering/tdd"),
            ("skills/engineering/tdd/tests.md", b"Referenced tests\r\n"),
            ("skills/engineering/tdd/agents/openai.yaml", b"policy:\n  allow_implicit_invocation: false\n"),
            ("skills/engineering/tdd/scripts/check.sh", b"do not execute this fixture\n", 0o755),
            skill("skills/productivity/grilling"), skill("skills/in-progress/implement-spec"),
            skill("skills/misc/setup-pre-commit")]


def archive(items, *, full_paths=False):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for item in items:
            name, content = item[:2]
            mode = item[2] if len(item) > 2 else 0o644
            kind = item[3] if len(item) > 3 else tarfile.REGTYPE
            member = tarfile.TarInfo(name if full_paths else f"skills-{REVISION}/{name}")
            member.type, member.mode = kind, mode
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "../../outside"
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            output.addfile(member, BytesIO(content) if kind == tarfile.REGTYPE else None)
    return gzip.compress(stream.getvalue())


class SkillSourceTests(unittest.TestCase):
    def fetch(self, items=None, **kwargs):
        payload = archive(entries() if items is None else items)
        with patch.object(source, "_download", side_effect=[json.dumps({"sha": REVISION}).encode(), payload]):
            return fetch_catalog(**kwargs)

    def test_default_catalog_preserves_full_directories_license_and_executable_flag(self):
        catalog = self.fetch()
        self.assertEqual(catalog.revision, REVISION)
        self.assertEqual(set(catalog.skills), {"tdd", "grilling"})
        tdd = catalog.skills["tdd"]
        self.assertEqual(tdd.source_path, "skills/engineering/tdd")
        self.assertEqual(tdd.files["tests.md"], SourceFile(b"Referenced tests\r\n"))
        self.assertEqual(tdd.files["scripts/check.sh"], SourceFile(b"do not execute this fixture\n", True))
        self.assertEqual(tdd.files["LICENSE.aihero"], SourceFile(LICENSE))
        self.assertEqual(tdd.files["SKILL.md"].content, entries()[1][1])
        self.assertIn("agents/openai.yaml", tdd.files)
        self.assertNotIn("LICENSE", tdd.files)

    def test_ref_is_resolved_once_and_only_exact_commit_archive_is_downloaded(self):
        with patch.object(source, "_download", side_effect=[b'{"sha": "' + REVISION.encode() + b'"}', archive(entries())]) as download:
            catalog = fetch_catalog(ref="feature/catalog")
        self.assertEqual(catalog.revision, REVISION)
        self.assertEqual([call.args[0] for call in download.call_args_list], [
            "https://api.github.com/repos/mattpocock/skills/commits/feature%2Fcatalog",
            f"https://codeload.github.com/mattpocock/skills/tar.gz/{REVISION}",
        ])

    def test_experimental_is_opt_in_while_misc_requires_explicit_selection(self):
        self.assertEqual(set(self.fetch(include_experimental=True).skills), {"tdd", "grilling", "implement-spec"})
        self.assertEqual(set(self.fetch(names=("setup-pre-commit",)).skills), {"setup-pre-commit"})
        self.assertEqual(set(self.fetch(names=("implement-spec",), include_experimental=True).skills), {"implement-spec"})
        with self.assertRaisesRegex(SourceError, "experimental"):
            self.fetch(names=("implement-spec",))
        with self.assertRaisesRegex(SourceError, "not found"):
            self.fetch(names=("missing",))
        with self.assertRaisesRegex(SourceError, "deprecated"):
            self.fetch(entries() + [skill("skills/deprecated/retired")], names=("retired",))

    def test_discovers_nested_skill_directories_without_hardcoded_skill_names(self):
        catalog = self.fetch(entries() + [skill("skills/engineering/more/new-skill")])
        self.assertIn("new-skill", catalog.skills)
        self.assertEqual(catalog.skills["new-skill"].source_path, "skills/engineering/more/new-skill")

    def test_rejects_duplicate_names_and_name_directory_mismatch(self):
        with self.assertRaisesRegex(SourceError, "duplicate upstream skill"):
            self.fetch(entries() + [skill("skills/productivity/tdd")])
        with self.assertRaisesRegex(SourceError, "does not match"):
            self.fetch(entries() + [skill("skills/engineering/example", "different")])

    def test_unrelated_root_symlink_is_ignored_but_selected_links_and_devices_are_rejected(self):
        # The genuine pinned upstream has AGENTS.md -> CLAUDE.md at repository root.
        self.assertIn("tdd", self.fetch(entries() + [("AGENTS.md", b"", 0o777, tarfile.SYMTYPE)]).skills)
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind), self.assertRaisesRegex(SourceError, "regular file"):
                self.fetch(entries() + [("skills/engineering/tdd/resources/linked", b"", 0o644, kind)])
        with self.assertRaisesRegex(SourceError, "root must be a directory"):
            self.fetch(entries() + [("skills/engineering/tdd", b"", 0o777, tarfile.SYMTYPE)])

    def test_license_must_be_regular_present_nonempty_and_copied_without_collision(self):
        for replacement in ([], [("LICENSE", b"")], [("LICENSE", b"", 0o644, tarfile.SYMTYPE)]):
            with self.subTest(replacement=replacement), self.assertRaises(SourceError):
                self.fetch(replacement + entries()[1:])
        self.assertEqual(self.fetch(entries() + [("skills/engineering/tdd/LICENSE.aihero", LICENSE)])
                         .skills["tdd"].files["LICENSE.aihero"].content, LICENSE)
        with self.assertRaisesRegex(SourceError, "different content"):
            self.fetch(entries() + [("skills/engineering/tdd/LICENSE.aihero", b"different")])
        with self.assertRaisesRegex(SourceError, "case collision"):
            self.fetch(entries() + [("skills/engineering/tdd/license.aihero", LICENSE)])

    def test_rejects_traversal_absolute_paths_and_other_roots_even_outside_selection(self):
        prefix = f"skills-{REVISION}/"
        for path in (prefix + "../outside", prefix + "unrelated/../../outside", "/absolute/path",
                     prefix + "./file", prefix + "dir//file", prefix + "dir\\file", "other-root/LICENSE"):
            payload = archive([(prefix + path_name, content, *other) for path_name, content, *other in entries()]
                              + [(path, b"unsafe")], full_paths=True)
            with self.subTest(path=path), patch.object(source, "_download", side_effect=[json.dumps({"sha": REVISION}).encode(), payload]):
                with self.assertRaisesRegex(SourceError, "archive path"):
                    fetch_catalog()

    def test_duplicate_archive_paths_and_case_collisions_are_rejected(self):
        for path in ("skills/engineering/tdd/tests.md", "skills/engineering/tdd/TESTS.md"):
            with self.subTest(path=path), self.assertRaisesRegex(SourceError, "case-colliding"):
                self.fetch(entries() + [(path, b"duplicate")])

    def test_rejects_unsafe_modes_and_bounds_archive_file_sizes_and_entry_count(self):
        with self.assertRaisesRegex(SourceError, "file mode"):
            self.fetch(entries() + [("skills/engineering/tdd/unsafe", b"content", 0o4755)])
        with patch.object(source, "_MAX_FILE", 200):
            with self.assertRaisesRegex(SourceError, "file exceeds"):
                self.fetch(entries() + [("skills/engineering/tdd/large", b"x" * 201)])
        with patch.object(source, "_MAX_ENTRIES", 2):
            with self.assertRaisesRegex(SourceError, "too many entries"):
                self.fetch()
        with patch.object(source, "_MAX_UNPACKED", 100):
            with self.assertRaisesRegex(SourceError, "decompressed"):
                self.fetch()
        with patch.object(source, "_MAX_DOWNLOAD", 100):
            with self.assertRaisesRegex(SourceError, "download size"):
                self.fetch()

    def test_invalid_refs_names_and_flags_fail_before_network_access(self):
        invalid = [{"ref": value} for value in (None, True, "", " ", "x" * 201, "bad\0ref")]
        invalid += [{"names": value} for value in ([], "tdd", ("tdd", "tdd"), ("synced",), (".system",), ("../escape",), ("TDD",), ("x" * 65,), (True,))]
        invalid += [{"include_experimental": value} for value in (None, 0, 1, "true")]
        with patch.object(source, "_download", side_effect=AssertionError("unexpected network access")):
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaises(SourceError):
                    fetch_catalog(**kwargs)

    def test_invalid_commit_metadata_and_archive_never_produce_catalog(self):
        for raw in (b"not json", b"[]", b"{}", b'{"sha": "main"}', b'{"sha": true}'):
            with self.subTest(raw=raw), patch.object(source, "_download", return_value=raw):
                with self.assertRaises(SourceError):
                    fetch_catalog()
        for payload in (b"not gzip", gzip.compress(b"not tar"), archive([])):
            with self.subTest(payload=payload[:20]), patch.object(source, "_download", side_effect=[json.dumps({"sha": REVISION}).encode(), payload]):
                with self.assertRaises(SourceError):
                    fetch_catalog()

    def test_frontmatter_name_must_be_unambiguous_and_safe(self):
        for body in (b"No frontmatter", b"---\ndescription: no name\n---\n", b"---\nname: tdd\nname: tdd\n---\n",
                     b"---\nname: synced\n---\n", b"---\nname: ../escape\n---\n", b"\xff"):
            custom = [(path, body if path.endswith("tdd/SKILL.md") else content, *other)
                      for path, content, *other in entries()]
            with self.subTest(body=body), self.assertRaises(SourceError):
                self.fetch(custom)
        for quoted in ('"tdd"', "'tdd'"):
            custom = [(path, f"---\nname: {quoted}\n---\n".encode() if path.endswith("tdd/SKILL.md") else content, *other)
                      for path, content, *other in entries()]
            self.assertIn("tdd", self.fetch(custom).skills)

    def test_download_bounds_response_and_rejects_redirects_and_transport_errors(self):
        class Response(BytesIO):
            def __init__(self, body, url="https://api.github.com/example"):
                super().__init__(body)
                self.url = url

            def geturl(self):
                return self.url

        with patch.object(source, "urlopen", return_value=Response(b"1234")):
            self.assertEqual(source._download("https://api.github.com/example", 4), b"1234")
        with patch.object(source, "urlopen", return_value=Response(b"12345")):
            with self.assertRaisesRegex(SourceError, "size limit"):
                source._download("https://api.github.com/example", 4)
        with patch.object(source, "urlopen", return_value=Response(b"data", "https://example.invalid/source")):
            with self.assertRaisesRegex(SourceError, "redirected"):
                source._download("https://api.github.com/example", 10)
        with patch.object(source, "urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(SourceError, "cannot download"):
                source._download("https://api.github.com/example", 10)


if __name__ == "__main__":
    unittest.main()
