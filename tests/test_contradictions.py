"""Unit tests for V2 Slice 4 — contradiction finder.

Pins:
- contradiction_pairs table + contradiction_finder_runs table exist.
- shortlist_candidates pairs only opposite-polarity claims on the same
  subject_normalized bucket.
- Same-day pairs are excluded (MIN_DAYS_APART).
- Already-classified pairs are excluded (UNIQUE constraint dedupe).
- VALID_CONTRADICTION_VERDICTS matches ADR 0004's 4-way enum.
- _persist_pair stores (claim_a < claim_b) regardless of input order.
- All tests OFFLINE — no LLM calls; verdicts inserted directly.

Run:
    .venv/bin/python -m unittest tests.test_contradictions
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import claims_db, db, contradictions


def _bootstrap():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    claims_db.init_claims_schema(conn)
    return conn


def _seed_article(conn, aid, date):
    conn.execute(
        "INSERT INTO articles (id, language, title, category, "
        "category_id, scraped_at, published_date) VALUES "
        "(?, 'EN', ?, 'press_release', 1, ?, ?)",
        (aid, f"Article {aid}", date, date),
    )


def _seed_claim(conn, *, article_id, polarity, subject_normalized,
                 quote, claim_type="policy_assertion"):
    conn.execute(
        "INSERT INTO extraction_runs (started_at) VALUES (datetime('now'))",
    )
    run_id = conn.execute("SELECT MAX(id) FROM extraction_runs").fetchone()[0]
    claims_db.insert_claims(
        conn, run_id, article_id, "EN",
        [{"type": claim_type, "polarity": polarity,
          "subject_normalized": subject_normalized,
          "is_checkable": True, "quote": quote,
          "subject": subject_normalized}],
    )
    return conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]


class SchemaTests(unittest.TestCase):
    def test_contradiction_pairs_table_exists(self):
        conn = _bootstrap()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("contradiction_pairs", tables)
        self.assertIn("contradiction_finder_runs", tables)

    def test_verdict_check_constraint(self):
        conn = _bootstrap()
        # Insert with invalid verdict should raise
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO contradiction_pairs (claim_a_id, claim_b_id, "
                "subject, verdict, confidence, reasoning_chain, detected_at) "
                "VALUES (1, 2, 'x', 'WAT', 0.5, '[]', datetime('now'))"
            )

    def test_unique_constraint_on_pair(self):
        conn = _bootstrap()
        _seed_article(conn, 1, "2025-01-01")
        _seed_article(conn, 2, "2025-06-01")
        c1 = _seed_claim(conn, article_id=1, polarity="AFFIRM",
                          subject_normalized="x", quote="q1")
        c2 = _seed_claim(conn, article_id=2, polarity="DENY",
                          subject_normalized="x", quote="q2")
        # First insert OK
        contradictions._persist_pair(
            conn, 1, c1, c2, "x", "CONTRADICTION", 0.9, [],
        )
        # Second insert with same pair must be ignored (INSERT OR IGNORE)
        contradictions._persist_pair(
            conn, 1, c1, c2, "x", "NOT_CONTRADICTORY", 0.3, [],
        )
        rows = conn.execute(
            "SELECT verdict FROM contradiction_pairs"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        # First wins (INSERT OR IGNORE)
        self.assertEqual(rows[0][0], "CONTRADICTION")


class ShortlistTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()

    def test_pairs_opposite_polarity_same_subject(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        c1 = _seed_claim(self.conn, article_id=1, polarity="AFFIRM",
                          subject_normalized="housing", quote="we will deliver")
        c2 = _seed_claim(self.conn, article_id=2, polarity="DENY",
                          subject_normalized="housing", quote="we will not deliver")
        cands = contradictions.shortlist_candidates(self.conn, apply_similarity_filter=False)
        self.assertEqual(len(cands), 1)
        a, b, subj = cands[0]
        self.assertEqual(sorted([a, b]), sorted([c1, c2]))
        self.assertEqual(subj, "housing")

    def test_does_not_pair_same_polarity(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        _seed_claim(self.conn, article_id=1, polarity="AFFIRM",
                     subject_normalized="x", quote="q1")
        _seed_claim(self.conn, article_id=2, polarity="AFFIRM",
                     subject_normalized="x", quote="q2")
        cands = contradictions.shortlist_candidates(self.conn, apply_similarity_filter=False)
        self.assertEqual(cands, [])

    def test_does_not_pair_different_subject(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        _seed_claim(self.conn, article_id=1, polarity="AFFIRM",
                     subject_normalized="housing", quote="q1")
        _seed_claim(self.conn, article_id=2, polarity="DENY",
                     subject_normalized="taxes", quote="q2")
        cands = contradictions.shortlist_candidates(self.conn, apply_similarity_filter=False)
        self.assertEqual(cands, [])

    def test_does_not_pair_same_day_claims(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-01-01")  # same date!
        _seed_claim(self.conn, article_id=1, polarity="AFFIRM",
                     subject_normalized="x", quote="q1")
        _seed_claim(self.conn, article_id=2, polarity="DENY",
                     subject_normalized="x", quote="q2")
        cands = contradictions.shortlist_candidates(self.conn, apply_similarity_filter=False)
        self.assertEqual(cands, [])

    def test_neutral_polarity_never_pairs(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        _seed_claim(self.conn, article_id=1, polarity="NEUTRAL",
                     subject_normalized="x", quote="ceremonial 1")
        _seed_claim(self.conn, article_id=2, polarity="DENY",
                     subject_normalized="x", quote="denial of x")
        cands = contradictions.shortlist_candidates(self.conn, apply_similarity_filter=False)
        self.assertEqual(cands, [])

    def test_promise_vs_denial_of_promise_pairs(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        c1 = _seed_claim(self.conn, article_id=1, polarity="PROMISE",
                          subject_normalized="housing", quote="we promise 12k")
        c2 = _seed_claim(self.conn, article_id=2, polarity="DENIAL_OF_PROMISE",
                          subject_normalized="housing", quote="never promised 12k")
        cands = contradictions.shortlist_candidates(self.conn, apply_similarity_filter=False)
        self.assertEqual(len(cands), 1)
        self.assertEqual(sorted([cands[0][0], cands[0][1]]), sorted([c1, c2]))

    def test_excludes_already_classified_pairs(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        c1 = _seed_claim(self.conn, article_id=1, polarity="AFFIRM",
                          subject_normalized="x", quote="q1")
        c2 = _seed_claim(self.conn, article_id=2, polarity="DENY",
                          subject_normalized="x", quote="q2")
        contradictions._persist_pair(
            self.conn, 1, c1, c2, "x", "NOT_CONTRADICTORY", 0.2, [],
        )
        cands = contradictions.shortlist_candidates(self.conn, apply_similarity_filter=False)
        self.assertEqual(cands, [])

    def test_excludes_non_checkable_claims(self):
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        # Manually insert one un-checkable claim
        conn = self.conn
        conn.execute(
            "INSERT INTO extraction_runs (started_at) VALUES (datetime('now'))"
        )
        run = conn.execute("SELECT MAX(id) FROM extraction_runs").fetchone()[0]
        claims_db.insert_claims(
            conn, run, 1, "EN",
            [{"type": "boast", "polarity": "AFFIRM",
              "subject_normalized": "x", "is_checkable": False,
              "quote": "best ever"}],
        )
        claims_db.insert_claims(
            conn, run, 2, "EN",
            [{"type": "denial", "polarity": "DENY",
              "subject_normalized": "x", "is_checkable": True,
              "quote": "we won't"}],
        )
        # The first is_checkable=false; pairing should be empty
        cands = contradictions.shortlist_candidates(conn)
        self.assertEqual(cands, [])


class EnumsAndConstantsTests(unittest.TestCase):
    def test_VALID_CONTRADICTION_VERDICTS_matches_adr(self):
        self.assertEqual(
            claims_db.VALID_CONTRADICTION_VERDICTS,
            {"CONTRADICTION", "EVOLVING_POSITION",
             "CONTEXT_CHANGED", "NOT_CONTRADICTORY"},
        )

    def test_opposite_polarities_covers_all_label_pairs(self):
        # NEUTRAL never pairs
        self.assertEqual(contradictions.OPPOSITE_POLARITIES["NEUTRAL"], set())
        # AFFIRM ↔ DENY/DENIAL_OF_PROMISE
        self.assertIn("DENY", contradictions.OPPOSITE_POLARITIES["AFFIRM"])
        self.assertIn("DENIAL_OF_PROMISE",
                       contradictions.OPPOSITE_POLARITIES["AFFIRM"])
        # PROMISE → DENIAL_OF_PROMISE
        self.assertIn("DENIAL_OF_PROMISE",
                       contradictions.OPPOSITE_POLARITIES["PROMISE"])


class SimilarityFilterTests(unittest.TestCase):
    """The semantic-similarity prefilter is the practical scaling fix —
    polarity-pair alone produces ~100x too many candidates on the live
    corpus. These tests pin the filter behaviour."""

    def setUp(self):
        self.conn = _bootstrap()
        _seed_article(self.conn, 1, "2025-01-01")
        _seed_article(self.conn, 2, "2025-06-01")
        self.c1 = _seed_claim(self.conn, article_id=1, polarity="AFFIRM",
                                subject_normalized="housing",
                                quote="we will build 12000 flats")
        self.c2 = _seed_claim(self.conn, article_id=2, polarity="DENY",
                                subject_normalized="housing",
                                quote="we will not build 12000 flats")
        self.c3 = _seed_claim(self.conn, article_id=2, polarity="DENY",
                                subject_normalized="housing",
                                quote="we will not raise GST")

        # Seed embeddings: c1↔c2 in the contradiction zone (cosine ≈ 0.7,
        # between MIN_SIMILARITY 0.55 and MAX_SIMILARITY 0.95);
        # c1↔c3 orthogonal — different topic, should be filtered out.
        from kahzaabu import matcher
        import math
        # angle 45 degrees → cosine ≈ 0.71 (squarely in the keep zone)
        v_flats_a = [1.0, 0.0] + [0.0] * 382
        v_flats_b = [math.cos(math.radians(45)),
                      math.sin(math.radians(45))] + [0.0] * 382
        v_gst     = [0.0, 0.0, 1.0] + [0.0] * 381   # orthogonal
        for cid, v in [(self.c1, v_flats_a), (self.c2, v_flats_b),
                        (self.c3, v_gst)]:
            claims_db.upsert_claim_embedding(
                self.conn, cid, matcher.pack_vector(v),
                "test-model", 384,
            )

    def test_filter_drops_low_similarity_pairs(self):
        # Without filter: c1↔c2 + c1↔c3 + c2↔c3 (some)
        all_cands = contradictions.shortlist_candidates(
            self.conn, apply_similarity_filter=False)
        self.assertGreaterEqual(len(all_cands), 2)

        # With filter: c1↔c2 stays (high cosine); c1↔c3 dropped
        filtered = contradictions.shortlist_candidates(
            self.conn, min_similarity=0.55)
        nos = [(a, b) for a, b, _ in filtered]
        self.assertIn((min(self.c1, self.c2), max(self.c1, self.c2)), nos)
        self.assertNotIn((min(self.c1, self.c3), max(self.c1, self.c3)), nos)

    def test_filter_drops_paraphrases_above_max(self):
        # c1↔c2 cosine ≈ 0.71. If we lower max_similarity below 0.71,
        # the pair is treated as paraphrase and excluded.
        filtered = contradictions.shortlist_candidates(
            self.conn, min_similarity=0.55, max_similarity=0.65)
        nos = [(a, b) for a, b, _ in filtered]
        self.assertNotIn((min(self.c1, self.c2), max(self.c1, self.c2)), nos)


class PersistTests(unittest.TestCase):
    def test_persist_normalizes_pair_order(self):
        conn = _bootstrap()
        _seed_article(conn, 1, "2025-01-01")
        _seed_article(conn, 2, "2025-06-01")
        c1 = _seed_claim(conn, article_id=1, polarity="AFFIRM",
                          subject_normalized="x", quote="q1")
        c2 = _seed_claim(conn, article_id=2, polarity="DENY",
                          subject_normalized="x", quote="q2")
        # Call with reversed order — should store sorted
        contradictions._persist_pair(
            conn, 1, c2, c1, "x", "CONTRADICTION", 0.9, [{"question": "q?"}],
        )
        r = conn.execute(
            "SELECT claim_a_id, claim_b_id, reasoning_chain "
            "FROM contradiction_pairs",
        ).fetchone()
        self.assertEqual(r[0], min(c1, c2))
        self.assertEqual(r[1], max(c1, c2))
        # reasoning_chain stored as JSON
        rc = json.loads(r[2])
        self.assertEqual(rc, [{"question": "q?"}])

    def test_confidence_clamped_to_unit_interval(self):
        conn = _bootstrap()
        _seed_article(conn, 1, "2025-01-01")
        _seed_article(conn, 2, "2025-06-01")
        c1 = _seed_claim(conn, article_id=1, polarity="AFFIRM",
                          subject_normalized="x", quote="q1")
        c2 = _seed_claim(conn, article_id=2, polarity="DENY",
                          subject_normalized="x", quote="q2")
        # confidence > 1 or < 0 should be clamped
        contradictions._persist_pair(
            conn, 1, c1, c2, "x", "CONTRADICTION", 2.5, [],
        )
        r = conn.execute(
            "SELECT confidence FROM contradiction_pairs"
        ).fetchone()
        self.assertEqual(r[0], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
