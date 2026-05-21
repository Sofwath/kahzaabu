# SPDX-License-Identifier: Apache-2.0
"""Unit tests for V2 Slice 6 — ClaimReview JSON-LD export (ADR 0006).

Pins:
- build_jsonld emits a schema.org-shaped dict with required fields.
- reviewRating.ratingValue == truth_score; alternateName == truth_score_label.
- disclaimer is always present (ADR 0006 mandate).
- itemReviewed.appearance pulls from articles.reference when available.
- _drop_none recursively cleans empty containers (indexers reject null).
- cache_jsonld persists to fact_checks.claimreview_jsonld.

Run:
    .venv/bin/python -m unittest tests.test_claimreview
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import claimreview, claims_db, db


def _bootstrap():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    claims_db.init_claims_schema(conn)
    return conn


def _seed_article(conn, aid, title="t", reference=None):
    conn.execute(
        "INSERT INTO articles (id, language, title, category, "
        "category_id, scraped_at, reference) VALUES "
        "(?, 'EN', ?, 'press_release', 1, '2025-01-01', ?)",
        (aid, title, reference),
    )


def _seed_factcheck(conn, **overrides):
    defaults = {
        "category": "LIE",
        "claim_date": "2025-01-01",
        "claim": "the test claim",
        "source_article_ids": "[]",
        "evidence_quotes": "[]",
        "confidence": "auto",
        "fingerprint": "fp-x",
        "created_at": "2025-01-01",
        "published": 1,
        "verdict_label": "REFUTED",
        "truth_score": 2,
        "truth_score_label": "FALSE",
        "speaker": "Mohamed Muizzu",
    }
    defaults.update(overrides)
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" * len(defaults))
    conn.execute(
        f"INSERT INTO fact_checks ({cols}) VALUES ({placeholders})",
        list(defaults.values()),
    )
    return conn.execute("SELECT MAX(id) FROM fact_checks").fetchone()[0]


class BuildJsonldTests(unittest.TestCase):
    def setUp(self):
        self.conn = _bootstrap()

    def test_required_schema_org_fields(self):
        fc = _seed_factcheck(self.conn)
        blob = claimreview.build_jsonld(self.conn, fc)
        self.assertEqual(blob["@context"], "https://schema.org")
        self.assertEqual(blob["@type"], "ClaimReview")
        self.assertIn("datePublished", blob)
        self.assertIn("url", blob)
        self.assertIn("claimReviewed", blob)
        self.assertIn("author", blob)
        self.assertIn("reviewRating", blob)
        self.assertIn("itemReviewed", blob)

    def test_reviewRating_includes_score_and_label(self):
        fc = _seed_factcheck(self.conn, truth_score=1,
                              truth_score_label="PANTS_ON_FIRE")
        blob = claimreview.build_jsonld(self.conn, fc)
        rating = blob["reviewRating"]
        self.assertEqual(rating["@type"], "Rating")
        self.assertEqual(rating["ratingValue"], 1)
        self.assertEqual(rating["bestRating"], 6)
        self.assertEqual(rating["worstRating"], 1)
        # alternateName is humanized
        self.assertEqual(rating["alternateName"], "Pants On Fire")

    def test_author_organization_block(self):
        fc = _seed_factcheck(self.conn)
        blob = claimreview.build_jsonld(self.conn, fc, env={})
        author = blob["author"]
        self.assertEqual(author["@type"], "Organization")
        self.assertEqual(author["name"], "Kahzaabu")
        self.assertIn("url", author)

    def test_org_url_env_override(self):
        fc = _seed_factcheck(self.conn)
        blob = claimreview.build_jsonld(
            self.conn, fc,
            env={"KAHZAABU_PUBLIC_BASE_URL": "https://kahzaabu.example",
                 "KAHZAABU_ORG_URL": "https://about.kahzaabu.example",
                 "KAHZAABU_ORG_SAMEAS": "https://github.com/x,https://twitter.com/y"},
        )
        self.assertEqual(blob["author"]["url"], "https://about.kahzaabu.example")
        self.assertEqual(blob["author"]["sameAs"],
                          ["https://github.com/x", "https://twitter.com/y"])
        # url uses the public base
        self.assertTrue(blob["url"].startswith("https://kahzaabu.example/"))

    def test_itemReviewed_appearance_from_articles_reference(self):
        _seed_article(self.conn, 100, reference="https://presidency.gov.mv/news/123")
        _seed_article(self.conn, 200)  # no reference
        fc = _seed_factcheck(self.conn, source_article_ids="[100, 200]")
        blob = claimreview.build_jsonld(
            self.conn, fc,
            env={"KAHZAABU_PUBLIC_BASE_URL": "https://kahzaabu.example"},
        )
        urls = [a["url"] for a in blob["itemReviewed"]["appearance"]]
        self.assertIn("https://presidency.gov.mv/news/123", urls)
        self.assertIn("https://kahzaabu.example/article/200", urls)

    def test_disclaimer_always_present(self):
        """ADR 0006 mandate."""
        fc = _seed_factcheck(self.conn)
        blob = claimreview.build_jsonld(self.conn, fc)
        self.assertIn("disclaimer", blob)
        self.assertIn("automated analysis", blob["disclaimer"])
        self.assertIn("not legal advice", blob["disclaimer"])

    def test_speaker_default_is_muizzu(self):
        fc = _seed_factcheck(self.conn)
        blob = claimreview.build_jsonld(self.conn, fc)
        self.assertEqual(blob["itemReviewed"]["author"]["name"],
                          "Mohamed Muizzu")

    def test_claim_text_truncation(self):
        long_claim = "x" * 1000
        fc = _seed_factcheck(self.conn, claim=long_claim)
        blob = claimreview.build_jsonld(self.conn, fc)
        self.assertLessEqual(len(blob["claimReviewed"]), 700)
        self.assertTrue(blob["claimReviewed"].endswith("..."))

    def test_prefers_public_summary_over_claim(self):
        fc = _seed_factcheck(self.conn,
                              claim="the FULL technical claim text",
                              public_summary="the public summary version")
        blob = claimreview.build_jsonld(self.conn, fc)
        self.assertEqual(blob["claimReviewed"], "the public summary version")

    def test_nonexistent_id_raises(self):
        with self.assertRaises(ValueError):
            claimreview.build_jsonld(self.conn, 999999)


class DropNoneTests(unittest.TestCase):
    def test_drops_none_values(self):
        self.assertEqual(claimreview._drop_none({"a": 1, "b": None}), {"a": 1})

    def test_drops_empty_dicts(self):
        self.assertEqual(claimreview._drop_none({"a": 1, "b": {}}), {"a": 1})

    def test_recursive(self):
        inp = {"a": {"x": None, "y": 1}, "b": [None, 2, None]}
        self.assertEqual(claimreview._drop_none(inp),
                          {"a": {"y": 1}, "b": [2]})


class CacheTests(unittest.TestCase):
    def test_cache_persists_blob(self):
        conn = _bootstrap()
        fc = _seed_factcheck(conn)
        blob = claimreview.cache_jsonld(conn, fc)
        # Re-read from DB
        cached = conn.execute(
            "SELECT claimreview_jsonld FROM fact_checks WHERE id=?",
            (fc,),
        ).fetchone()[0]
        self.assertEqual(json.loads(cached), blob)

    def test_regenerate_all_only_published(self):
        conn = _bootstrap()
        _seed_factcheck(conn, published=1)
        _seed_factcheck(conn, published=0, fingerprint="fp-unpublished")
        r = claimreview.regenerate_all(conn, only_published=True)
        self.assertEqual(r["regenerated"], 1)

    def test_regenerate_all_includes_unpublished_when_requested(self):
        conn = _bootstrap()
        _seed_factcheck(conn, published=1)
        _seed_factcheck(conn, published=0, fingerprint="fp-unpublished")
        r = claimreview.regenerate_all(conn, only_published=False)
        self.assertEqual(r["regenerated"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
