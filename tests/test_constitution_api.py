# SPDX-License-Identifier: Apache-2.0
"""Tests for the constitution web API (V2 UI polish slice).

Spins up FastAPI via TestClient against the live SQLite DB (which
already has the 301 articles imported). If the DB doesn't have the
articles, every test is skipped with a clear reason.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from kahzaabu.web.app import app
from kahzaabu.web.db_dep import DEFAULT_DB


def _has_articles() -> bool:
    if not DEFAULT_DB.exists():
        return False
    try:
        with sqlite3.connect(DEFAULT_DB) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM constitution_articles").fetchone()[0]
            return n > 0
    except sqlite3.OperationalError:
        return False


@unittest.skipUnless(_has_articles(),
                      "constitution_articles not imported in live DB")
class ConstitutionAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_list_articles_default(self):
        r = self.c.get("/api/constitution/articles?limit=10")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["limit"], 10)
        self.assertGreater(body["total"], 200)  # corpus has 301
        self.assertEqual(len(body["items"]), 10)
        for item in body["items"]:
            self.assertIn("article_no", item)
            self.assertIn("title", item)
            self.assertIn("body", item)

    def test_list_articles_paged(self):
        r1 = self.c.get("/api/constitution/articles?limit=5&offset=0")
        r2 = self.c.get("/api/constitution/articles?limit=5&offset=5")
        ids1 = [i["article_no"] for i in r1.json()["items"]]
        ids2 = [i["article_no"] for i in r2.json()["items"]]
        self.assertEqual(set(ids1) & set(ids2), set())

    def test_get_single_article(self):
        r = self.c.get("/api/constitution/1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["article_no"], 1)
        self.assertIn("title", body)
        self.assertIn("body", body)

    def test_get_missing_article(self):
        r = self.c.get("/api/constitution/999")
        self.assertEqual(r.status_code, 404)

    def test_search_returns_hits(self):
        r = self.c.get("/api/constitution/search?q=religion&limit=3")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["query"], "religion")
        self.assertIsInstance(body["items"], list)
        # The constitution mentions religion in at least the
        # state-religion article — expect non-empty hits.
        self.assertGreater(len(body["items"]), 0)

    def test_search_empty_query_rejected(self):
        r = self.c.get("/api/constitution/search?q=")
        self.assertEqual(r.status_code, 422)  # min_length=1 → validation error


class ConstitutionPageRouteTests(unittest.TestCase):
    """Static page routes resolve even on a fresh DB."""
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_constitution_page_route(self):
        r = self.c.get("/constitution")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Constitution of the Republic of Maldives",
                       r.text)

    def test_factcheck_detail_page_route(self):
        # /factcheck/{id} serves the static page regardless of whether
        # the id exists — the page itself fetches the manifest on load.
        r = self.c.get("/factcheck/1")
        self.assertEqual(r.status_code, 200)
        # The Truth-O-Meter ladder labels MUST be in the page; JS uses
        # them to render the active rung.
        self.assertIn("MOSTLY_TRUE", r.text)
        self.assertIn("PANTS_ON_FIRE", r.text)
        # Page links to the reproducibility manifest endpoint.
        self.assertIn("/api/reproducibility/", r.text)


class TruthScoreLadderVizTests(unittest.TestCase):
    """The /api/viz/truth-score-ladder endpoint always returns the
    6 rungs in canonical order, even when some have zero counts."""
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_returns_six_rungs_in_canonical_order(self):
        r = self.c.get("/api/viz/truth-score-ladder")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["labels"],
                          ["TRUE", "MOSTLY_TRUE", "HALF_TRUE",
                           "MOSTLY_FALSE", "FALSE", "PANTS_ON_FIRE"])
        self.assertEqual(len(body["values"]), 6)
        for v in body["values"]:
            self.assertIsInstance(v, int)
            self.assertGreaterEqual(v, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
