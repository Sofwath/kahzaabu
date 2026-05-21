# SPDX-License-Identifier: Apache-2.0
"""Unit tests for V2 Slice 1 — claims enrichment.

Pins:
- Schema migration is idempotent.
- New columns (polarity, subject_normalized, is_checkable) exist after init.
- insert_claims is backward-compatible: missing keys store NULL.
- Polarity values are validated against the 6-label set (ADR 0002).
- is_checkable coerces from bool / int / str.

Run:
    .venv/bin/python -m unittest tests.test_claims_enrichment
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import claims_db, db


def _make_db():
    """In-memory DB with full schema bootstrapped."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    claims_db.init_claims_schema(conn)
    return conn


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


class SchemaMigrationTests(unittest.TestCase):
    def test_new_columns_exist_after_init(self):
        conn = _make_db()
        cols = _columns(conn, "claims")
        self.assertIn("polarity", cols)
        self.assertIn("subject_normalized", cols)
        self.assertIn("is_checkable", cols)

    def test_migration_is_idempotent(self):
        """Calling init_claims_schema twice should not error or duplicate."""
        conn = _make_db()
        # second call should not raise
        claims_db.init_claims_schema(conn)
        cols = _columns(conn, "claims")
        self.assertIn("polarity", cols)

    def test_new_indexes_exist(self):
        conn = _make_db()
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='claims'"
        )}
        self.assertIn("idx_claims_polarity", indexes)
        self.assertIn("idx_claims_subject_n", indexes)
        self.assertIn("idx_claims_checkable", indexes)


class InsertClaimsBackwardCompatTests(unittest.TestCase):
    """Old call-sites that don't pass V2 fields must still work."""

    def setUp(self):
        self.conn = _make_db()
        # Insert a fake article to satisfy FK
        self.conn.execute(
            "INSERT INTO articles (id, language, title, category, "
            "category_id, scraped_at) VALUES (1, 'EN', 't', 'press_release', "
            "1, '2025-01-01')",
        )
        run = self.conn.execute(
            "INSERT INTO extraction_runs (started_at) VALUES ('2025-01-01') "
            "RETURNING id"
        ).fetchone()
        self.run_id = run[0]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_old_shape_still_works(self):
        """Pre-V2 claim dicts (no polarity/subject_normalized/is_checkable)
        must insert with NULLs in the new columns."""
        n = claims_db.insert_claims(
            self.conn, self.run_id, 1, "EN",
            [{"type": "numeric_promise", "subject": "housing",
              "value": "5000", "deadline": "2025", "quote": "q"}],
        )
        self.assertEqual(n, 1)
        r = self.conn.execute(
            "SELECT polarity, subject_normalized, is_checkable FROM claims"
        ).fetchone()
        self.assertIsNone(r["polarity"])
        self.assertIsNone(r["subject_normalized"])
        self.assertIsNone(r["is_checkable"])

    def test_v2_shape_stores_correctly(self):
        claims_db.insert_claims(
            self.conn, self.run_id, 1, "EN",
            [{"type": "numeric_promise", "polarity": "PROMISE",
              "subject_normalized": "President Muizzu",
              "is_checkable": True, "quote": "q"}],
        )
        r = self.conn.execute(
            "SELECT polarity, subject_normalized, is_checkable FROM claims"
        ).fetchone()
        self.assertEqual(r["polarity"], "PROMISE")
        self.assertEqual(r["subject_normalized"], "President Muizzu")
        self.assertEqual(r["is_checkable"], 1)


class PolarityValidationTests(unittest.TestCase):
    """ADR 0002 — invalid polarity labels must be coerced to NULL, not stored
    silently. Protects against an LLM drifting to unexpected labels."""

    def setUp(self):
        self.conn = _make_db()
        self.conn.execute(
            "INSERT INTO articles (id, language, title, category, "
            "category_id, scraped_at) VALUES (1, 'EN', 't', 'press_release', "
            "1, '2025-01-01')",
        )
        self.conn.commit()
        self.run_id = 1

    def test_all_six_canonical_labels_accepted(self):
        labels = ["AFFIRM", "DENY", "PROMISE", "DENIAL_OF_PROMISE",
                  "CLAIM_OF_FACT", "NEUTRAL"]
        for i, lab in enumerate(labels):
            claims_db.insert_claims(
                self.conn, self.run_id, 1, "EN",
                [{"type": "x", "polarity": lab, "quote": str(i)}],
            )
        rows = self.conn.execute(
            "SELECT polarity FROM claims ORDER BY id"
        ).fetchall()
        self.assertEqual([r["polarity"] for r in rows], labels)

    def test_invalid_label_coerced_to_null(self):
        claims_db.insert_claims(
            self.conn, self.run_id, 1, "EN",
            [{"type": "x", "polarity": "DEFINITELY_NOT_A_LABEL",
              "quote": "q"}],
        )
        r = self.conn.execute("SELECT polarity FROM claims").fetchone()
        self.assertIsNone(r["polarity"])

    def test_lowercase_label_normalized(self):
        """LLM occasionally emits lowercase — accept and uppercase."""
        claims_db.insert_claims(
            self.conn, self.run_id, 1, "EN",
            [{"type": "x", "polarity": "promise", "quote": "q"}],
        )
        r = self.conn.execute("SELECT polarity FROM claims").fetchone()
        self.assertEqual(r["polarity"], "PROMISE")

    def test_label_with_spaces_normalized(self):
        """'denial of promise' -> 'DENIAL_OF_PROMISE'."""
        claims_db.insert_claims(
            self.conn, self.run_id, 1, "EN",
            [{"type": "x", "polarity": "denial of promise", "quote": "q"}],
        )
        r = self.conn.execute("SELECT polarity FROM claims").fetchone()
        self.assertEqual(r["polarity"], "DENIAL_OF_PROMISE")

    def test_valid_polarities_constant_matches_adr(self):
        """The VALID_POLARITIES set must equal ADR 0002's enumeration."""
        self.assertEqual(
            claims_db.VALID_POLARITIES,
            {"AFFIRM", "DENY", "PROMISE", "DENIAL_OF_PROMISE",
             "CLAIM_OF_FACT", "NEUTRAL"},
        )


class IsCheckableCoercionTests(unittest.TestCase):
    """is_checkable accepts bool, int, str — stores as 0/1 or NULL."""

    def setUp(self):
        self.conn = _make_db()
        self.conn.execute(
            "INSERT INTO articles (id, language, title, category, "
            "category_id, scraped_at) VALUES (1, 'EN', 't', 'press_release', "
            "1, '2025-01-01')",
        )
        self.conn.commit()

    def _insert(self, value):
        claims_db.insert_claims(
            self.conn, 1, 1, "EN",
            [{"type": "x", "is_checkable": value, "quote": str(value)}],
        )
        return self.conn.execute(
            "SELECT is_checkable FROM claims ORDER BY id DESC LIMIT 1"
        ).fetchone()["is_checkable"]

    def test_true_bool(self):       self.assertEqual(self._insert(True), 1)
    def test_false_bool(self):      self.assertEqual(self._insert(False), 0)
    def test_int_1(self):           self.assertEqual(self._insert(1), 1)
    def test_int_0(self):           self.assertEqual(self._insert(0), 0)
    def test_str_true(self):        self.assertEqual(self._insert("true"), 1)
    def test_str_false(self):       self.assertEqual(self._insert("false"), 0)
    def test_str_yes(self):         self.assertEqual(self._insert("yes"), 1)
    def test_none(self):            self.assertIsNone(self._insert(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
