# SPDX-License-Identifier: Apache-2.0
"""Tests for the articles web API.

Regression guard added 2026-05-22 after an end-user reported
`/article/36701` rendered empty. Root cause: the
`/api/article/{id}` endpoint defaulted `language="EN"`, but
article IDs are unique-but-language-variable (some IDs only
exist as the DV row), so calling `/api/article/36701` with no
explicit language hit a 404 even though the article was in
the DB.

The "full UI review" pass before this regression check used
manual curl against EN article IDs only. Adding a real test
file for the articles API would have caught the bug
immediately — this file fixes that gap.

Spins up FastAPI via TestClient against the live SQLite DB.
If the DB doesn't have at least one EN and one DV article,
the relevant test is skipped with a clear reason.
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


def _sample_id(language: str):
    """Return one valid article id for the requested language,
    or None if no DB / no rows."""
    if not DEFAULT_DB.exists():
        return None
    try:
        with sqlite3.connect(DEFAULT_DB) as conn:
            row = conn.execute(
                "SELECT id FROM articles WHERE language = ? "
                "  AND body_text IS NOT NULL AND body_text != '' "
                "ORDER BY id DESC LIMIT 1",
                (language,)
            ).fetchone()
            return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def _dv_only_id():
    """Return an article id that exists as a DV row but NOT as
    an EN row. This is the exact bug surface: when the JS calls
    /api/article/{id} with no language query, the endpoint must
    resolve to the DV row instead of 404'ing on the missing EN."""
    if not DEFAULT_DB.exists():
        return None
    try:
        with sqlite3.connect(DEFAULT_DB) as conn:
            row = conn.execute(
                "SELECT a.id FROM articles a "
                "WHERE a.language = 'DV' "
                "  AND NOT EXISTS (SELECT 1 FROM articles b "
                "                   WHERE b.id = a.id AND b.language = 'EN') "
                "  AND a.body_text IS NOT NULL AND a.body_text != '' "
                "ORDER BY a.id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
    except sqlite3.OperationalError:
        return None


EN_ID = _sample_id("EN")
DV_ID = _dv_only_id()


@unittest.skipUnless(DEFAULT_DB.exists() and EN_ID,
                      "no EN articles in live DB")
class ArticlesAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    # ── Listing ────────────────────────────────────────────────

    def test_list_articles_default_returns_en(self):
        r = self.c.get("/api/articles?limit=5")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["limit"], 5)
        self.assertGreater(body["total"], 0)
        for item in body["items"]:
            self.assertEqual(item["language"], "EN",
                "default listing must be EN; switching the default "
                "is a UX regression for visitors who don't read DV")

    def test_list_articles_with_dv_filter(self):
        """Listing with ?language=DV should never return EN rows.

        NOTE: the listing query also filters on
        `published_date >= '2023-11-17'`. Many DV articles have an
        empty published_date (scraper limitation — DV pages don't
        expose a stable date field), so an empty result is a
        corpus-coverage finding, not an API bug. This test only
        verifies the language filter is honoured for any rows
        that do come back."""
        r = self.c.get("/api/articles?language=DV&limit=3")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for item in body["items"]:
            self.assertEqual(item["language"], "DV",
                "language filter must be honoured — never mix in EN")

    # ── Single article — happy path ────────────────────────────

    def test_get_article_en(self):
        r = self.c.get(f"/api/article/{EN_ID}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["id"], EN_ID)
        self.assertEqual(body["language"], "EN")
        self.assertIn("title", body)
        self.assertIn("body_text", body)
        self.assertIn("claims", body)
        self.assertIn("fact_checks", body)

    def test_get_article_missing_returns_404(self):
        r = self.c.get("/api/article/999999999")
        self.assertEqual(r.status_code, 404)

    # ── Single article — DV-only id (the bug) ──────────────────

    @unittest.skipUnless(DV_ID is not None,
                         "no DV-only article ids in live DB — "
                         "every DV row has a paired EN row")
    def test_get_article_dv_only_id_resolves(self):
        """The exact symptom the end-user reported: /article/{id}
        for a DV-only article id was returning empty because the
        endpoint defaulted to language=EN and 404'd on the missing
        EN row. With the fix, no-language query should resolve to
        the DV row."""
        r = self.c.get(f"/api/article/{DV_ID}")
        self.assertEqual(
            r.status_code, 200,
            f"/api/article/{DV_ID} (DV-only id) should resolve "
            "without an explicit ?language= param. Got "
            f"{r.status_code}: {r.text}")
        body = r.json()
        self.assertEqual(body["id"], DV_ID)
        self.assertEqual(body["language"], "DV",
            "When the EN row doesn't exist, the response should "
            "carry the DV row's language so the frontend can "
            "render it correctly (Thaana script, dir=auto).")
        # The downstream claims query must use the row's actual
        # language; otherwise a DV article would pull EN claims
        # (or vice-versa).
        self.assertIsInstance(body["claims"], list)

    @unittest.skipUnless(DV_ID is not None,
                         "no DV-only article ids in live DB")
    def test_get_article_explicit_dv_still_works(self):
        """Backwards-compat: ?language=DV still works the way it did
        before the fix. We only changed the default-when-unspecified
        behaviour."""
        r = self.c.get(f"/api/article/{DV_ID}?language=DV")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["language"], "DV")

    @unittest.skipUnless(DV_ID is not None,
                         "no DV-only article ids in live DB")
    def test_get_article_explicit_en_for_dv_only_id_404s(self):
        """If the caller explicitly asks for EN and only DV exists,
        a 404 is the right answer — don't silently substitute, because
        that would mask data-pipeline gaps."""
        r = self.c.get(f"/api/article/{DV_ID}?language=EN")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
