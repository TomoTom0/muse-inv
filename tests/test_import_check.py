"""Regression tests for import-check input validation and for the
last_verified format check in validate_record.

import-check used to parse items.jsonl on its own, without schema
validation: a record with an invalid status was silently classified as
"missing", a line without an id crashed with KeyError, and duplicate id
lines were counted twice. It now loads through load_jsonl (the same
validation import-jsonl uses), so invalid lines are errors.

validate_record used to accept the basic date format (YYYYMMDD) for
last_verified via fromisoformat, which the DB CHECK (dashed YYYY-MM-DD
GLOB) then rejected at INSERT time with an unhandled IntegrityError.

Run from the repo root: python3 -m unittest discover -s tests
"""
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import muse_inv


def built_rec(**over):
    base = {
        "id": "inv-001", "name": "Tool A", "kind": "tool", "layer": "built",
        "status": "active", "source": "memory/2026-01-01.md",
        "description": "desc A", "alias": "用途A",
        "portability": "unclassified", "depends_on": [],
        "curated_in": "", "last_verified": "",
    }
    base.update(over)
    return base


class ImportCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._old_data, self._old_db = muse_inv.DATA, muse_inv.DB_PATH
        muse_inv.DATA = root / "data"
        muse_inv.DB_PATH = muse_inv.DATA / "muse-inv.db"
        self.addCleanup(self._restore_paths)
        conn = muse_inv.connect(create=True)
        try:
            with conn:
                muse_inv.insert_record(conn, built_rec())
                muse_inv.insert_record(conn, built_rec(
                    id="inv-002", name="Tool B", kind="route",
                    description="desc B", alias="用途B",
                    portability="portable", depends_on=["inv-001"],
                    last_verified="2026-01-02"))
        finally:
            conn.close()
        self.export_dir = root / "export"
        self.export_dir.mkdir()

    def _restore_paths(self):
        muse_inv.DATA, muse_inv.DB_PATH = self._old_data, self._old_db

    def write_items(self, lines):
        text = "".join(
            (ln if isinstance(ln, str) else json.dumps(ln, ensure_ascii=False)) + "\n"
            for ln in lines)
        (self.export_dir / "items.jsonl").write_text(text, encoding="utf-8")

    def run_check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = muse_inv.cmd_import_check(
                argparse.Namespace(path=str(self.export_dir)))
        return rc, out.getvalue()

    def test_classifies_present_missing_conflict(self):
        self.write_items([
            built_rec(),  # identical to the DB row -> present
            built_rec(id="inv-002", name="Tool B", kind="route",
                      description="desc B CHANGED", alias="用途B",
                      portability="portable", depends_on=["inv-001"],
                      last_verified="2026-01-02"),  # same id, differs -> conflict
            built_rec(id="inv-003", name="Tool C", description="desc C",
                      alias="用途C"),  # valid but not in DB -> missing
        ])
        rc, out = self.run_check()
        self.assertEqual(rc, 0)
        self.assertIn("already present (identical): 1", out)
        self.assertIn("missing (would need setup): 1", out)
        self.assertIn("conflicting (same id, different content): 1", out)

    def test_schema_invalid_record_is_an_error_not_a_classification(self):
        self.write_items([built_rec(id="inv-009", status="bogus-status")])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.run_check()
        self.assertEqual(cm.exception.code, 2)

    def test_line_without_id_is_an_error_not_a_traceback(self):
        self.write_items(['{"name": "No ID at all", "kind": "tool"}'])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.run_check()
        self.assertEqual(cm.exception.code, 2)

    def test_duplicate_id_lines_are_an_error(self):
        self.write_items([built_rec(id="inv-003"), built_rec(id="inv-003")])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.run_check()
        self.assertEqual(cm.exception.code, 2)

    def test_invalid_json_line_is_an_error(self):
        self.write_items(["{not json"])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.run_check()
        self.assertEqual(cm.exception.code, 2)


class LastVerifiedValidationTest(unittest.TestCase):
    def test_dashed_iso_date_and_empty_are_valid(self):
        self.assertEqual(
            muse_inv.validate_record(built_rec(last_verified="2026-11-15")), [])
        self.assertEqual(
            muse_inv.validate_record(built_rec(last_verified="")), [])

    def test_basic_format_without_dashes_is_rejected(self):
        errs = muse_inv.validate_record(built_rec(last_verified="20261115"))
        self.assertTrue(any("last_verified" in e for e in errs), errs)

    def test_impossible_date_is_rejected(self):
        errs = muse_inv.validate_record(built_rec(last_verified="2026-02-30"))
        self.assertTrue(any("last_verified" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
