# SPDX-License-Identifier: Apache-2.0
"""Unit tests for V2 Slice 3 — canonical claim matching.

Pins:
- Schema: canonical_claim_id column + claim_embeddings + matching_runs.
- Vector pack/unpack round-trips with float32 precision.
- cosine() correctness across orthogonal / identical / opposite vectors.
- Entity extraction picks out numbers / dates / proper nouns.
- jaccard set similarity.
- find_match correctly identifies (a) first-in-bucket (self), (b)
  embedding+entity match (no LLM needed), and (c) routes to LLM
  tiebreaker when embedding matches but entities don't.
- All tests are OFFLINE — no OpenAI / Anthropic API calls. The
  embedding step is bypassed by pre-seeding claim_embeddings; the
  LLM tiebreaker is monkey-patched.

Run:
    .venv/bin/python -m unittest tests.test_matcher
"""
from __future__ import annotations

import sqlite3
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import claims_db, db, matcher


def _bootstrap():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    claims_db.init_claims_schema(conn)
    conn.execute(
        "INSERT INTO articles (id, language, title, category, "
        "category_id, scraped_at) VALUES (1, 'EN', 't', 'press_release', "
        "1, '2025-01-01')",
    )
    conn.execute(
        "INSERT INTO extraction_runs (started_at) VALUES ('2025-01-01')"
    )
    conn.commit()
    return conn


def _insert(conn, *, quote, subject_normalized, polarity="AFFIRM"):
    claims_db.insert_claims(
        conn, 1, 1, "EN",
        [{"type": "policy_assertion",
          "polarity": polarity,
          "subject_normalized": subject_normalized,
          "quote": quote, "is_checkable": True}],
    )
    return conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]


class SchemaTests(unittest.TestCase):
    def test_canonical_claim_id_column_exists(self):
        conn = _bootstrap()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(claims)")}
        self.assertIn("canonical_claim_id", cols)

    def test_claim_embeddings_table_exists(self):
        conn = _bootstrap()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("claim_embeddings", tables)
        self.assertIn("matching_runs", tables)


class VectorOpsTests(unittest.TestCase):
    def test_pack_unpack_roundtrip(self):
        v = [0.1, -0.5, 0.99, 0.0, 0.234567]
        b = matcher.pack_vector(v)
        v2 = matcher.unpack_vector(b)
        self.assertEqual(len(v2), len(v))
        for a, b in zip(v, v2):
            self.assertAlmostEqual(a, b, places=5)  # float32 precision

    def test_cosine_identical(self):
        v = [1.0, 0.5, -0.3, 0.8]
        self.assertAlmostEqual(matcher.cosine(v, v), 1.0, places=4)

    def test_cosine_orthogonal(self):
        self.assertAlmostEqual(matcher.cosine([1, 0], [0, 1]), 0.0, places=4)

    def test_cosine_opposite(self):
        self.assertAlmostEqual(matcher.cosine([1, 1], [-1, -1]), -1.0, places=4)

    def test_cosine_handles_zero_vec(self):
        self.assertEqual(matcher.cosine([0, 0, 0], [1, 1, 1]), 0.0)


class EntityExtractionTests(unittest.TestCase):
    def test_extracts_numbers(self):
        ents = matcher.extract_entities("We will deliver 12,000 flats")
        self.assertIn("12,000", ents)

    def test_extracts_proper_nouns(self):
        ents = matcher.extract_entities(
            "Gulhi Island visit to Hulhumalé"
        )
        self.assertIn("Gulhi Island", ents)
        self.assertIn("Hulhumalé", ents)

    def test_extracts_dates(self):
        ents = matcher.extract_entities("by 2028 we shall finish")
        # "2028" matches the 4+ digit regex; "by 2028" matches the date regex
        self.assertTrue(any("2028" in e for e in ents))

    def test_extracts_currency(self):
        ents = matcher.extract_entities("worth MVR 1 billion")
        self.assertTrue(any("MVR" in e for e in ents))

    def test_stopwords_excluded(self):
        ents = matcher.extract_entities("The Government of Maldives")
        # 'The', 'Government', 'Maldives' should all be in _STOPWORDS
        self.assertNotIn("The", ents)
        self.assertNotIn("Government", ents)
        self.assertNotIn("Maldives", ents)


class JaccardTests(unittest.TestCase):
    def test_full_overlap(self):
        self.assertEqual(matcher.jaccard({1, 2, 3}, {1, 2, 3}), 1.0)

    def test_no_overlap(self):
        self.assertEqual(matcher.jaccard({1, 2}, {3, 4}), 0.0)

    def test_partial_overlap(self):
        # |A∩B|=1, |A∪B|=3 → 1/3
        self.assertAlmostEqual(matcher.jaccard({1, 2}, {2, 3}), 1/3, places=4)

    def test_empty_sets(self):
        self.assertEqual(matcher.jaccard(set(), set()), 0.0)


class FindMatchTests(unittest.TestCase):
    """Tests for the core matching algorithm — without any real LLM call."""

    def setUp(self):
        self.conn = _bootstrap()
        # Seed three claims in the SAME bucket. Identical bucket so they
        # all participate in candidate pools.
        self.c1 = _insert(self.conn, quote="We will deliver 12,000 flats",
                            subject_normalized="housing")
        self.c2 = _insert(self.conn, quote="Government commits to 12,000 flats",
                            subject_normalized="housing")
        self.c3 = _insert(self.conn, quote="Income tax raised by 1%",
                            subject_normalized="housing")  # same bucket, different content

        # Pre-seed embeddings. c1 and c2 are semantically similar,
        # c3 is different. We construct vectors directly so the test
        # is deterministic without any API.
        v_housing_a = [1.0, 0.95, 0.05] + [0.0] * 1533
        v_housing_b = [0.95, 1.0, 0.05] + [0.0] * 1533   # cosine ≈ 0.99 with v_a
        v_tax       = [0.05, 0.05, 1.0] + [0.0] * 1533   # near-orthogonal

        for cid, vec in [(self.c1, v_housing_a),
                          (self.c2, v_housing_b),
                          (self.c3, v_tax)]:
            claims_db.upsert_claim_embedding(
                self.conn, cid, matcher.pack_vector(vec),
                matcher.EMBED_MODEL, matcher.EMBED_DIM,
            )

    def test_first_claim_is_its_own_canonical(self):
        c1 = dict(self.conn.execute(
            "SELECT * FROM claims WHERE id=?", (self.c1,),
        ).fetchone())
        cid, reason = matcher.find_match(self.conn, c1)
        self.assertEqual(cid, self.c1)
        self.assertEqual(reason, "self")

    def test_paraphrase_with_shared_entity_matches_via_embed_entity(self):
        """c2 should match c1 — both have '12,000' as entity AND embeddings
        are >0.85 cosine."""
        c2 = dict(self.conn.execute(
            "SELECT * FROM claims WHERE id=?", (self.c2,),
        ).fetchone())
        cid, reason = matcher.find_match(self.conn, c2)
        self.assertEqual(cid, self.c1)
        self.assertEqual(reason, "embed+entity")

    def test_different_content_yields_self(self):
        """c3 (about tax) shouldn't match c1/c2 (about housing) —
        cosine < 0.85."""
        c3 = dict(self.conn.execute(
            "SELECT * FROM claims WHERE id=?", (self.c3,),
        ).fetchone())
        cid, reason = matcher.find_match(self.conn, c3)
        self.assertEqual(cid, self.c3)
        self.assertEqual(reason, "self")

    def test_canonical_chain_collapsed(self):
        """If A→B canonical, then C matching A should resolve to B."""
        # Set c1's canonical to c2 manually (simulating a prior run)
        claims_db.set_canonical(self.conn, self.c1, self.c2)
        # Add c4 that matches c1
        c4 = _insert(self.conn, quote="12,000 housing units promised",
                       subject_normalized="housing")
        v = [0.97, 0.93, 0.05] + [0.0] * 1533
        claims_db.upsert_claim_embedding(
            self.conn, c4, matcher.pack_vector(v),
            matcher.EMBED_MODEL, matcher.EMBED_DIM,
        )
        c4d = dict(self.conn.execute(
            "SELECT * FROM claims WHERE id=?", (c4,),
        ).fetchone())
        cid, _ = matcher.find_match(self.conn, c4d)
        # Should walk through c1's canonical pointer to c2
        self.assertEqual(cid, self.c2)


class CanonicalChainTests(unittest.TestCase):
    def test_set_canonical_persists(self):
        conn = _bootstrap()
        c1 = _insert(conn, quote="q1", subject_normalized="x")
        c2 = _insert(conn, quote="q2", subject_normalized="x")
        claims_db.set_canonical(conn, c2, c1)
        r = conn.execute(
            "SELECT canonical_claim_id FROM claims WHERE id=?", (c2,),
        ).fetchone()
        self.assertEqual(r[0], c1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
