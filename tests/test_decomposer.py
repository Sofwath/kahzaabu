"""Unit tests for V2 Slice 2 — Q&A decomposition.

Pins:
- claim_questions table + decomposition_runs table exist after init.
- insert_claim_questions validates answer_type + source_medium against
  AVeriTeC-aligned enums (ADR 0001).
- Question rows can have NULL answer (the typical state right after
  decomposition; verification fills them later).
- claims_missing_decomposition excludes claims that already have
  questions and 'no_specific_claims' sentinel rows.

Run:
    .venv/bin/python -m unittest tests.test_decomposer
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import claims_db, db


def _bootstrap():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    claims_db.init_claims_schema(conn)
    # seed an article + extraction_run + two claims
    conn.execute(
        "INSERT INTO articles (id, language, title, category, "
        "category_id, scraped_at, published_date) "
        "VALUES (1, 'EN', 'Test article', 'press_release', 1, "
        "'2025-01-01', '2025-01-01')",
    )
    conn.execute(
        "INSERT INTO extraction_runs (started_at) VALUES ('2025-01-01')"
    )
    claims_db.insert_claims(
        conn, 1, 1, "EN",
        [
            {"type": "numeric_promise", "subject": "housing",
             "polarity": "PROMISE", "subject_normalized": "the government",
             "is_checkable": True,
             "quote": "We will deliver 12,000 flats by end of 2025"},
            {"type": "no_specific_claims", "subject": None,
             "polarity": "NEUTRAL", "is_checkable": False, "quote": None},
        ],
    )
    conn.commit()
    return conn


class SchemaTests(unittest.TestCase):
    def test_claim_questions_table_exists(self):
        conn = _bootstrap()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("claim_questions", tables)
        self.assertIn("decomposition_runs", tables)

    def test_claim_questions_columns(self):
        conn = _bootstrap()
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(claim_questions)"
        )}
        for c in ("id", "claim_id", "question", "answer", "answer_type",
                   "source_url", "source_medium", "confidence",
                   "decomposition_run_id", "answered_at", "created_at"):
            self.assertIn(c, cols)

    def test_idempotent_init(self):
        conn = _bootstrap()
        claims_db.init_claims_schema(conn)
        # should not raise; row counts stable
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM claim_questions").fetchone()[0],
            0,
        )


class InsertClaimQuestionsTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()
        self.claim_id = self.conn.execute(
            "SELECT id FROM claims WHERE type='numeric_promise'"
        ).fetchone()[0]
        self.run_id = claims_db.start_decomposition_run(self.conn, "test")

    def test_basic_insert(self):
        n = claims_db.insert_claim_questions(
            self.conn, self.run_id, self.claim_id,
            [
                {"question": "Was 12,000 flats actually promised?",
                 "answer_type": "Boolean", "source_medium": "archive"},
                {"question": "How many delivered as of 2025?",
                 "answer_type": "Extractive", "source_medium": "web_search"},
            ],
        )
        self.assertEqual(n, 2)
        rows = self.conn.execute(
            "SELECT question, answer, answer_type, source_medium "
            "FROM claim_questions WHERE claim_id = ? ORDER BY id",
            (self.claim_id,),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["answer"])
        self.assertEqual(rows[0]["answer_type"], "Boolean")
        self.assertEqual(rows[0]["source_medium"], "archive")

    def test_invalid_answer_type_nulled(self):
        claims_db.insert_claim_questions(
            self.conn, self.run_id, self.claim_id,
            [{"question": "q", "answer_type": "WAT", "source_medium": "archive"}],
        )
        r = self.conn.execute(
            "SELECT answer_type FROM claim_questions"
        ).fetchone()
        self.assertIsNone(r["answer_type"])

    def test_invalid_source_medium_nulled(self):
        claims_db.insert_claim_questions(
            self.conn, self.run_id, self.claim_id,
            [{"question": "q", "answer_type": "Boolean",
              "source_medium": "instagram"}],
        )
        r = self.conn.execute(
            "SELECT source_medium FROM claim_questions"
        ).fetchone()
        self.assertIsNone(r["source_medium"])

    def test_all_four_answer_types_accepted(self):
        types = ["Abstractive", "Extractive", "Boolean", "Unanswerable"]
        for t in types:
            claims_db.insert_claim_questions(
                self.conn, self.run_id, self.claim_id,
                [{"question": f"q-{t}", "answer_type": t}],
            )
        rows = self.conn.execute(
            "SELECT answer_type FROM claim_questions ORDER BY id"
        ).fetchall()
        self.assertEqual([r["answer_type"] for r in rows], types)

    def test_all_four_source_mediums_accepted(self):
        mediums = ["archive", "web_search", "constitution", "manifesto"]
        for m in mediums:
            claims_db.insert_claim_questions(
                self.conn, self.run_id, self.claim_id,
                [{"question": f"q-{m}", "source_medium": m}],
            )
        rows = self.conn.execute(
            "SELECT source_medium FROM claim_questions ORDER BY id"
        ).fetchall()
        self.assertEqual([r["source_medium"] for r in rows], mediums)

    def test_skip_questions_without_question_text(self):
        n = claims_db.insert_claim_questions(
            self.conn, self.run_id, self.claim_id,
            [
                {"question": "good"},
                {"question": "", "answer_type": "Boolean"},
                {"answer_type": "Boolean"},                # missing question
            ],
        )
        self.assertEqual(n, 1)

    def test_valid_enums_match_adr(self):
        self.assertEqual(
            claims_db.VALID_ANSWER_TYPES,
            {"Abstractive", "Extractive", "Boolean", "Unanswerable"},
        )
        self.assertEqual(
            claims_db.VALID_SOURCE_MEDIUMS,
            {"archive", "web_search", "constitution", "manifesto"},
        )


class ClaimsMissingDecompositionTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()
        self.numeric_claim_id = self.conn.execute(
            "SELECT id FROM claims WHERE type='numeric_promise'"
        ).fetchone()[0]

    def test_returns_claims_without_questions(self):
        rows = claims_db.claims_missing_decomposition(self.conn)
        # one numeric_promise; the no_specific_claims sentinel is excluded
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.numeric_claim_id)

    def test_excludes_already_decomposed(self):
        run = claims_db.start_decomposition_run(self.conn, "test")
        claims_db.insert_claim_questions(
            self.conn, run, self.numeric_claim_id,
            [{"question": "q1"}],
        )
        rows = claims_db.claims_missing_decomposition(self.conn)
        self.assertEqual(len(rows), 0)

    def test_excludes_no_specific_claims_sentinel(self):
        rows = claims_db.claims_missing_decomposition(self.conn)
        types = [r["type"] for r in rows]
        self.assertNotIn("no_specific_claims", types)

    def test_limit_respected(self):
        # add a second claim so we have two checkable rows
        self.conn.execute(
            "INSERT INTO claims (article_id, language, type, polarity, "
            "quote, created_at) VALUES (1, 'EN', 'numeric_update', "
            "'CLAIM_OF_FACT', 'q', '2025-01-01')"
        )
        self.conn.commit()
        rows = claims_db.claims_missing_decomposition(self.conn, limit=1)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
