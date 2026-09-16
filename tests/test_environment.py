"""Tests for the managed environment builder's exclusion set."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from anvil.environment import managed_environment


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
