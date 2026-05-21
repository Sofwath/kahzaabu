# SPDX-License-Identifier: Apache-2.0
"""Unit tests for V2 Slice 5 — fact_check enrichment (ADR 0005).

Pins:
- enrich_fact_check populates verdict_label / truth_score /
  truth_score_label / reasoning_chain.
- reasoning_chain assembly from claim_questions of supporting claims.
- contradiction_pair_id's reasoning_chain takes precedence.
- promote_contradictions_to_factchecks is idempotent — pairs already
  promoted are skipped.
- only_unset mode skips fact_checks that already have verdict_label.

Run:
    .venv/bin/python -m unittest tests.test_fact_check_enricher
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import claims_db, db, fact_check_enricher


def _bootstrap():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    claims_db.init_claims_schema(conn)
    return conn


def _seed_factcheck(conn, *, category, confidence="auto",
                      source_article_ids=None):
    sai = json.dumps(source_article_ids or [])
    conn.execute(
        """INSERT INTO fact_checks
           (category, claim_date, claim, source_article_ids,
            confidence, fingerprint, created_at)
           VALUES (?, '2025-01-01', 'test claim', ?, ?, ?, '2025-01-01')""",
        (category, sai, confidence, f"fp-{category}-{sai}"),
    )
    return conn.execute("SELECT MAX(id) FROM fact_checks").fetchone()[0]


class BasicEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()

    def test_lie_becomes_refuted_and_pants_on_fire_if_high_conf(self):
        fc = _seed_factcheck(self.conn, category="LIE", confidence="reviewed")
        # 'reviewed' = 0.90 — but PANTS_ON_FIRE needs ≥0.95 + category=LIE.
        # 0.90 < 0.95 → FALSE/2, not PANTS_ON_FIRE.
        r = fact_check_enricher.enrich_fact_check(self.conn, fc)
        self.assertEqual(r["verdict_label"], "REFUTED")
        self.assertEqual(r["truth_score"], 2)

    def test_misleading_becomes_half_true(self):
        fc = _seed_factcheck(self.conn, category="MISLEADING")
        r = fact_check_enricher.enrich_fact_check(self.conn, fc)
        self.assertEqual(r["verdict_label"], "CONFLICTING_EVIDENCE")
        self.assertEqual(r["truth_score"], 4)
        self.assertEqual(r["truth_score_label"], "HALF_TRUE")

    def test_persists_to_db(self):
        fc = _seed_factcheck(self.conn, category="BROKEN DEADLINE")
        fact_check_enricher.enrich_fact_check(self.conn, fc)
        row = self.conn.execute(
            "SELECT verdict_label, truth_score, truth_score_label, "
            "reasoning_chain FROM fact_checks WHERE id = ?", (fc,),
        ).fetchone()
        self.assertEqual(row["verdict_label"], "REFUTED")
        self.assertEqual(row["truth_score"], 3)
        # reasoning_chain is valid JSON
        chain = json.loads(row["reasoning_chain"])
        self.assertIsInstance(chain, list)


class ReasoningChainAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()
        # Seed an article + 2 claims + 3 claim_questions
        self.conn.execute(
            "INSERT INTO articles (id, language, title, category, "
            "category_id, scraped_at) VALUES (100, 'EN', 't', "
            "'press_release', 1, '2025-01-01')"
        )
        self.conn.execute(
            "INSERT INTO extraction_runs (started_at) VALUES (datetime('now'))"
        )
        claims_db.insert_claims(
            self.conn, 1, 100, "EN",
            [{"type": "numeric_promise", "polarity": "PROMISE",
              "quote": "5000 flats", "is_checkable": True}],
        )
        c1 = self.conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]
        run = self.conn.execute(
            "INSERT INTO decomposition_runs (started_at) "
            "VALUES (datetime('now')) RETURNING id"
        ).fetchone()[0]
        claims_db.insert_claim_questions(
            self.conn, run, c1,
            [{"question": "Were 5,000 flats promised?",
              "answer_type": "Boolean", "source_medium": "archive"},
             {"question": "How many delivered?",
              "answer_type": "Extractive", "source_medium": "web_search"}],
        )
        self.fc = _seed_factcheck(self.conn, category="BROKEN DEADLINE",
                                    source_article_ids=[100])

    def test_chain_built_from_supporting_claim_questions(self):
        chain = fact_check_enricher._assemble_reasoning_chain(
            self.conn, self.fc, None,
        )
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0]["question"],
                          "Were 5,000 flats promised?")

    def test_empty_chain_when_no_supporting_claims(self):
        fc = _seed_factcheck(self.conn, category="LIE",
                              source_article_ids=[999])  # nonexistent article
        chain = fact_check_enricher._assemble_reasoning_chain(
            self.conn, fc, None,
        )
        self.assertEqual(chain, [])

    def test_contradiction_pair_chain_overrides(self):
        # Seed a contradiction pair with a custom reasoning chain
        ca = self.conn.execute("SELECT id FROM claims LIMIT 1").fetchone()[0]
        # need a 2nd claim for the FK
        claims_db.insert_claims(
            self.conn, 1, 100, "EN",
            [{"type": "denial", "polarity": "DENY",
              "quote": "we never promised", "is_checkable": True}],
        )
        cb = self.conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]
        custom_chain = [{"question": "Custom Q", "answer": "Custom A"}]
        self.conn.execute(
            """INSERT INTO contradiction_pairs
               (claim_a_id, claim_b_id, subject, verdict, confidence,
                reasoning_chain, detected_at)
               VALUES (?, ?, 'x', 'CONTRADICTION', 0.9, ?, datetime('now'))""",
            (min(ca, cb), max(ca, cb), json.dumps(custom_chain)),
        )
        cp_id = self.conn.execute(
            "SELECT MAX(id) FROM contradiction_pairs",
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE fact_checks SET contradiction_pair_id=? WHERE id=?",
            (cp_id, self.fc),
        )
        self.conn.commit()
        chain = fact_check_enricher._assemble_reasoning_chain(
            self.conn, self.fc, cp_id,
        )
        # Contradiction chain wins over claim-questions
        self.assertEqual(chain, custom_chain)


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()
        self.conn.execute(
            "INSERT INTO articles (id, language, title, category, "
            "category_id, scraped_at, published_date) VALUES "
            "(1, 'EN', 't', 'press_release', 1, '2025-01-01', '2025-01-01')"
        )
        self.conn.execute(
            "INSERT INTO articles (id, language, title, category, "
            "category_id, scraped_at, published_date) VALUES "
            "(2, 'EN', 't', 'press_release', 1, '2025-06-01', '2025-06-01')"
        )
        self.conn.execute(
            "INSERT INTO extraction_runs (started_at) VALUES (datetime('now'))"
        )
        claims_db.insert_claims(
            self.conn, 1, 1, "EN",
            [{"type": "numeric_promise", "polarity": "AFFIRM",
              "quote": "we will do x", "subject_normalized": "x",
              "is_checkable": True}],
        )
        ca = self.conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]
        claims_db.insert_claims(
            self.conn, 1, 2, "EN",
            [{"type": "denial", "polarity": "DENY",
              "quote": "we will not do x", "subject_normalized": "x",
              "is_checkable": True}],
        )
        cb = self.conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]
        self.conn.execute(
            """INSERT INTO contradiction_pairs
               (claim_a_id, claim_b_id, subject, verdict, confidence,
                reasoning_chain, detected_at)
               VALUES (?, ?, 'x', 'CONTRADICTION', 0.95,
                       '[{"question":"q?","answer":"a"}]',
                       datetime('now'))""",
            (min(ca, cb), max(ca, cb)),
        )

    def test_promote_creates_factcheck(self):
        n = fact_check_enricher.promote_contradictions_to_factchecks(self.conn)
        self.assertEqual(n, 1)
        r = self.conn.execute(
            "SELECT category, contradiction_pair_id FROM fact_checks"
        ).fetchone()
        self.assertEqual(r["category"], "CONTRADICTION")
        self.assertIsNotNone(r["contradiction_pair_id"])

    def test_promote_is_idempotent(self):
        fact_check_enricher.promote_contradictions_to_factchecks(self.conn)
        n2 = fact_check_enricher.promote_contradictions_to_factchecks(self.conn)
        self.assertEqual(n2, 0)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM fact_checks"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_non_contradiction_pairs_not_promoted(self):
        self.conn.execute(
            "UPDATE contradiction_pairs SET verdict='NOT_CONTRADICTORY'"
        )
        n = fact_check_enricher.promote_contradictions_to_factchecks(self.conn)
        self.assertEqual(n, 0)


class RunEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()
        # Seed several fact_checks across categories
        for cat in ["LIE", "MISLEADING", "BROKEN DEADLINE", "CREDIT THEFT"]:
            _seed_factcheck(self.conn, category=cat)

    def test_runs_against_all_unset(self):
        r = fact_check_enricher.run_enrichment(self.conn)
        self.assertEqual(r["enriched"], 4)
        self.assertEqual(r["by_verdict_label"].get("REFUTED"), 3)         # LIE, BROKEN, CREDIT
        self.assertEqual(r["by_verdict_label"].get("CONFLICTING_EVIDENCE"), 1)  # MISLEADING

    def test_only_unset_skips_already_done(self):
        fact_check_enricher.run_enrichment(self.conn)
        r2 = fact_check_enricher.run_enrichment(self.conn, only_unset=True)
        self.assertEqual(r2["enriched"], 0)

    def test_rebuild_redoes_all(self):
        fact_check_enricher.run_enrichment(self.conn)
        r2 = fact_check_enricher.run_enrichment(self.conn, only_unset=False)
        self.assertEqual(r2["enriched"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
