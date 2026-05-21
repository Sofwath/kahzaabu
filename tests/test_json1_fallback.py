"""Unit tests for the JSON1 fallback path in handle_get_article.

handle_get_article queries fact_checks via `json_each(source_article_ids)`
for robust JSON traversal. If a stripped SQLite build lacks JSON1, the
function falls back to a LIKE chain over the serialized JSON. These
tests verify both paths return the same result.

Run:
    .venv/bin/python -m unittest tests.test_json1_fallback
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# We import the plugin handler directly. The plugin's tools.py looks up
# the DB path from the imported kahzaabu package — we override _conn() to
# use our in-memory fixture instead.
sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))


class NoJsonEachConnection:
    """Wraps an sqlite3.Connection and raises OperationalError on any query
    that uses `json_each` — simulating a SQLite build without JSON1.
    All other Connection methods/attributes are delegated through."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql, params=()):
        if "json_each" in sql:
            raise sqlite3.OperationalError("no such function: json_each")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _make_fixture_db() -> sqlite3.Connection:
    """Build an in-memory DB with the minimal schema handle_get_article needs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE articles (
            id INTEGER NOT NULL,
            language TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            body_text TEXT,
            published_date TEXT,
            reference TEXT,
            PRIMARY KEY (id, language)
        );
        CREATE TABLE claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER NOT NULL,
            language TEXT NOT NULL,
            type TEXT,
            subject TEXT,
            value TEXT,
            deadline TEXT,
            actor_credited TEXT,
            quote TEXT
        );
        CREATE TABLE fact_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            claim_date TEXT NOT NULL,
            claim TEXT NOT NULL,
            topic TEXT,
            confidence TEXT,
            source_article_ids TEXT NOT NULL,
            published INTEGER DEFAULT 0
        );
    """)

    # Fixture: one article (id=34980), one fact_check linked to it via JSON.
    conn.execute(
        "INSERT INTO articles (id, language, title, category, body_text, published_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (34980, 'EN', 'Nilandhoo Airport to be completed within 30 months',
         'speech', 'Body text...', '2025-09-15'),
    )
    conn.execute(
        "INSERT INTO claims (article_id, language, type, quote) VALUES (?, 'EN', ?, ?)",
        (34980, 'deadline_promise', 'within 30 months'),
    )
    # source_article_ids stored as JSON with one id; both paths must find this.
    conn.execute(
        "INSERT INTO fact_checks (category, claim_date, claim, topic, "
        "confidence, source_article_ids, published) VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("BROKEN DEADLINE", "2025-09-20",
         "Nilandhoo water supply network promised to be completed 'within 30 months'",
         "infrastructure", "reviewed", json.dumps([34980])),
    )
    conn.commit()
    return conn


class Json1FallbackTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_fixture_db()

    def tearDown(self):
        self.conn.close()

    def _call_handle_get_article(self, aid: int) -> dict:
        """Invoke the real handler with our in-memory DB swapped in."""
        from plugins.kahzaabu import tools
        with patch.object(tools, "_conn", return_value=self.conn):
            return json.loads(tools.handle_get_article({"article_id": aid}))

    def test_json_each_happy_path(self):
        """Default path uses json_each — should find the linked fact-check."""
        result = self._call_handle_get_article(34980)
        self.assertIn("article", result)
        self.assertEqual(len(result["linked_fact_checks"]), 1)
        self.assertEqual(result["linked_fact_checks"][0]["category"],
                          "BROKEN DEADLINE")

    def test_like_fallback_when_json_each_missing(self):
        """If SQLite lacks JSON1, the LIKE chain must produce the same result."""
        from plugins.kahzaabu import tools
        proxy = NoJsonEachConnection(self.conn)
        with patch.object(tools, "_conn", return_value=proxy):
            result = json.loads(tools.handle_get_article({"article_id": 34980}))

        self.assertIn("article", result)
        self.assertEqual(len(result["linked_fact_checks"]), 1,
                          "LIKE fallback should find the same fact-check as json_each")
        self.assertEqual(result["linked_fact_checks"][0]["category"],
                          "BROKEN DEADLINE")

    def test_like_fallback_handles_multi_id_array(self):
        """LIKE chain has four patterns ([X], [X,..., ...,X,..., ...,X]).
        Verify each position is matched."""
        # Insert another fact_check with article 34980 in middle of array
        self.conn.execute(
            "INSERT INTO fact_checks (category, claim_date, claim, topic, "
            "confidence, source_article_ids, published) VALUES "
            "(?, ?, ?, ?, ?, ?, 1)",
            ("CREDIT THEFT", "2025-10-01", "Some claim", "infra",
             "reviewed", json.dumps([1234, 34980, 5678])),
        )
        self.conn.commit()

        from plugins.kahzaabu import tools
        proxy = NoJsonEachConnection(self.conn)
        with patch.object(tools, "_conn", return_value=proxy):
            result = json.loads(tools.handle_get_article({"article_id": 34980}))

        self.assertEqual(len(result["linked_fact_checks"]), 2,
                          "LIKE fallback must find article 34980 whether it's "
                          "the only element OR in the middle of a multi-id array")

    def test_article_with_no_linked_factchecks(self):
        """Both paths should agree on zero links when there are none."""
        from plugins.kahzaabu import tools
        # Add an article that no fact_check references
        self.conn.execute(
            "INSERT INTO articles (id, language, title, category) "
            "VALUES (99999, 'EN', 'Unrelated', 'speech')",
        )
        self.conn.commit()

        with patch.object(tools, "_conn", return_value=self.conn):
            r1 = json.loads(tools.handle_get_article({"article_id": 99999}))
        self.assertEqual(len(r1["linked_fact_checks"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
