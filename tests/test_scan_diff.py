"""Regression tests for the scan diff comparison (canonical_diff).

Run from the repo root: python3 -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import muse_inv


def rec(**over):
    base = {
        "id": "platform:doc:a", "name": "a.md", "kind": "doc",
        "layer": "platform", "status": "active", "source": "/x/docs/a.md",
        "description": "Doc A", "alias": "", "portability": "unclassified",
        "depends_on": [], "curated_in": "", "last_verified": "2026-11-15",
        "category": "doc",
    }
    base.update(over)
    return base


class CanonicalDiffTest(unittest.TestCase):
    def test_last_verified_only_difference_is_not_a_change(self):
        self.assertEqual(
            muse_inv.canonical_diff(rec()),
            muse_inv.canonical_diff(rec(last_verified="2026-11-25")))

    def test_content_difference_is_a_change(self):
        self.assertNotEqual(
            muse_inv.canonical_diff(rec()),
            muse_inv.canonical_diff(rec(description="Doc A2")))

    def test_canonical_still_includes_last_verified(self):
        # canonical() is the identity comparison used by import-check;
        # it must keep last_verified (only the scan diff excludes it).
        self.assertNotEqual(
            muse_inv.canonical(rec()),
            muse_inv.canonical(rec(last_verified="2026-11-25")))


if __name__ == "__main__":
    unittest.main()
