# SPDX-License-Identifier: Apache-2.0
"""Unit tests for V2 Slice 7 — /api/contradictions endpoint surface.

Pins the list + detail shape so a future refactor of the API doesn't
silently break the /contradictions page that consumes it.

Tests use a FastAPI TestClient against an in-memory DB.

Run:
    .venv/bin/python -m unittest tests.test_contradictions_api
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import claims_db, db


def _bootstrap_db_with_pair(conn):
    conn.execute(
        "INSERT INTO articles (id, language, title, category, "
        "category_id, scraped_at, published_date) VALUES "
        "(1, 'EN', 'Article A', 'press_release', 1, '2025-01-01', '2025-01-01')"
    )
    conn.execute(
        "INSERT INTO articles (id, language, title, category, "
        "category_id, scraped_at, published_date) VALUES "
        "(2, 'EN', 'Article B', 'press_release', 1, '2025-06-01', '2025-06-01')"
    )
    conn.execute(
        "INSERT INTO extraction_runs (started_at) VALUES (datetime('now'))"
    )
    claims_db.insert_claims(
        conn, 1, 1, "EN",
        [{"type": "policy_assertion", "polarity": "AFFIRM",
          "subject_normalized": "x", "quote": "we will do x",
          "is_checkable": True}],
    )
    ca = conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]
    claims_db.insert_claims(
        conn, 1, 2, "EN",
        [{"type": "denial", "polarity": "DENY",
          "subject_normalized": "x", "quote": "we will not do x",
          "is_checkable": True}],
    )
    cb = conn.execute("SELECT MAX(id) FROM claims").fetchone()[0]
    conn.execute(
        """INSERT INTO contradiction_pairs
           (claim_a_id, claim_b_id, subject, verdict, confidence,
            reasoning_chain, detected_at, published)
           VALUES (?, ?, 'x', 'CONTRADICTION', 0.92,
                   '[{"question":"q?","answer":"a","evidence":"e"}]',
                   '2025-06-15T00:00:00+00:00', 1)""",
        (min(ca, cb), max(ca, cb)),
    )
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM contradiction_pairs").fetchone()[0]


class ListContradictionsTests(unittest.TestCase):
    def _client(self):
        # Disable public-mode for tests (the gating logic is tested
        # separately; here we just want shape correctness).
        from fastapi.testclient import TestClient
        import importlib
        # Force a clean in-memory DB on the app
        import kahzaabu.web.db_dep as db_dep
        memconn = sqlite3.connect(":memory:", check_same_thread=False)
        memconn.row_factory = sqlite3.Row
        db.init_db(memconn)
        claims_db.init_claims_schema(memconn)
        cid = _bootstrap_db_with_pair(memconn)
        # Patch get_db to return our in-mem
        from kahzaabu.web.app import app
        app.dependency_overrides[db_dep.get_db] = lambda: memconn
        return TestClient(app), memconn, cid

    def test_list_returns_items(self):
        client, _, cid = self._client()
        r = client.get("/api/contradictions")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertGreaterEqual(d["total"], 1)
        self.assertGreaterEqual(len(d["items"]), 1)

    def test_list_includes_claim_a_and_b_quotes(self):
        client, _, cid = self._client()
        r = client.get("/api/contradictions")
        item = r.json()["items"][0]
        self.assertIn("claim_a", item)
        self.assertIn("claim_b", item)
        self.assertIn("quote", item["claim_a"])

    def test_verdict_filter_validation(self):
        client, _, cid = self._client()
        r = client.get("/api/contradictions?verdict=BOGUS")
        self.assertEqual(r.status_code, 400)

    def test_verdict_filter_works(self):
        client, _, cid = self._client()
        r = client.get("/api/contradictions?verdict=CONTRADICTION")
        d = r.json()
        for item in d["items"]:
            self.assertEqual(item["verdict"], "CONTRADICTION")

    def test_subject_filter_works(self):
        client, _, cid = self._client()
        r = client.get("/api/contradictions?subject=x")
        self.assertEqual(r.status_code, 200)
        for item in r.json()["items"]:
            self.assertEqual(item["subject"], "x")


class DetailContradictionTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        import kahzaabu.web.db_dep as db_dep
        memconn = sqlite3.connect(":memory:", check_same_thread=False)
        memconn.row_factory = sqlite3.Row
        db.init_db(memconn)
        claims_db.init_claims_schema(memconn)
        cid = _bootstrap_db_with_pair(memconn)
        from kahzaabu.web.app import app
        app.dependency_overrides[db_dep.get_db] = lambda: memconn
        return TestClient(app), memconn, cid

    def test_detail_returns_reasoning_chain(self):
        client, _, cid = self._client()
        r = client.get(f"/api/contradictions/{cid}")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["verdict"], "CONTRADICTION")
        self.assertIsInstance(d["reasoning_chain"], list)
        self.assertGreaterEqual(len(d["reasoning_chain"]), 1)

    def test_detail_returns_404_for_nonexistent(self):
        client, _, cid = self._client()
        r = client.get("/api/contradictions/999999")
        self.assertEqual(r.status_code, 404)

    def test_detail_includes_claim_metadata(self):
        client, _, cid = self._client()
        r = client.get(f"/api/contradictions/{cid}")
        d = r.json()
        self.assertIn("claim_a", d)
        self.assertIn("claim_b", d)
        self.assertEqual(d["claim_a"]["polarity"], "AFFIRM")
        self.assertEqual(d["claim_b"]["polarity"], "DENY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
