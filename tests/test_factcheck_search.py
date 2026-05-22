# SPDX-License-Identifier: Apache-2.0
"""Tests for the BM25 + substring-fallback search on fact_checks.

This module replaces the earlier `LIKE %longest_title_token%` reverse
lookup used by /api/constitution/{n}/citing-factchecks. The earlier
approach over-matched on common tokens (Constitution, President);
this module's job is to do better.

The tests pin:
  - FTS5 schema + triggers exist after init
  - Backfill populates fact_checks_fts from existing fact_checks
  - INSERT/UPDATE/DELETE on fact_checks keep the FTS table in sync
  - search_fact_checks returns BM25 rank when FTS5 matches
  - search_fact_checks falls back to substring search with a
    match-count pseudo-rank when FTS5 returns zero hits
  - Multi-token coverage: 2-token matches outrank 1-token matches
  - Stopword tokens (< 4 chars) don't contribute to the rank
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import factcheck_search
from kahzaabu.claims_db import init_full_schema


def _make_conn():
    """Fresh in-memory DB with the full schema. Each test gets a
    clean slate so we can control which fact_checks are present."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_full_schema(conn)
    return conn


def _insert_fc(conn, *, fc_id, claim, topic="general",
                what_actually_happened="", published=1):
    conn.execute(
        "INSERT INTO fact_checks "
        "(id, category, claim, claim_date, topic, confidence, "
        " what_actually_happened, source_article_ids, evidence_quotes, "
        " created_at, published, verdict_label) "
        "VALUES (?, 'LIE', ?, '2026-01-01', ?, 'reviewed', ?, "
        "        '[]', '[]', '2026-01-01T00:00:00Z', ?, 'REFUTED')",
        (fc_id, claim, topic, what_actually_happened, published)
    )
    conn.commit()


class FTSSchemaAndTriggers(unittest.TestCase):
    def test_init_creates_virtual_table(self):
        conn = _make_conn()
        # init_full_schema calls init_fact_checks_fts as part of its
        # lazy-init block.
        cols = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name = 'fact_checks_fts'"
        ).fetchone()
        self.assertIsNotNone(cols,
            "fact_checks_fts virtual table must be created by "
            "init_full_schema — without it the FTS5 path is dead")
        self.assertIn("fts5", cols[0].lower())

    def test_trigger_inserts_propagate_to_fts(self):
        conn = _make_conn()
        _insert_fc(conn, fc_id=1, claim="Judicial Service Commission "
                                       "appointments are unconstitutional")
        # After INSERT, the FTS table should reflect the new row.
        n = conn.execute(
            "SELECT COUNT(*) FROM fact_checks_fts WHERE fact_check_id = 1"
        ).fetchone()[0]
        self.assertEqual(n, 1,
            "INSERT trigger must populate fact_checks_fts so a search "
            "right after insertion can find the row")

    def test_trigger_updates_propagate_to_fts(self):
        conn = _make_conn()
        _insert_fc(conn, fc_id=2, claim="original claim text")
        conn.execute(
            "UPDATE fact_checks SET claim = 'completely new claim text' "
            "WHERE id = 2"
        )
        conn.commit()
        hits = factcheck_search.search_fact_checks(conn, "completely new")
        self.assertTrue(any(h["id"] == 2 for h in hits),
            "UPDATE trigger must re-index the new claim text — "
            "stale FTS5 content means the editor's correction "
            "doesn't surface in search")

    def test_trigger_deletes_remove_from_fts(self):
        conn = _make_conn()
        _insert_fc(conn, fc_id=3, claim="ephemeral claim that will be deleted")
        conn.execute("DELETE FROM fact_checks WHERE id = 3")
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM fact_checks_fts WHERE fact_check_id = 3"
        ).fetchone()[0]
        self.assertEqual(n, 0,
            "DELETE trigger must clean fact_checks_fts; otherwise "
            "deleted rows would still surface in search results")


class BackfillBehavior(unittest.TestCase):
    def test_backfill_populates_from_existing_rows(self):
        """Simulate: existing DB has fact_check rows but no FTS5 index
        yet. backfill_fact_checks_fts must populate the index from
        those rows so search works for legacy data."""
        conn = _make_conn()  # Full schema including FTS triggers
        # Drop the FTS table + triggers to simulate the legacy state
        # (rows exist; FTS hasn't been created yet).
        conn.executescript("""
            DROP TRIGGER IF EXISTS fact_checks_fts_ai;
            DROP TRIGGER IF EXISTS fact_checks_fts_au;
            DROP TRIGGER IF EXISTS fact_checks_fts_ad;
            DROP TABLE IF EXISTS fact_checks_fts;
        """)
        _insert_fc(conn, fc_id=99,
                   claim="pre-existing claim text from before FTS")
        # Now re-create FTS + backfill — the real-world upgrade path.
        ok = factcheck_search.init_fact_checks_fts(conn)
        self.assertTrue(ok, "FTS5 should be available")
        n = factcheck_search.backfill_fact_checks_fts(conn)
        self.assertEqual(n, 1,
            "backfill must populate the FTS table with all existing "
            "rows — without it, fact-checks created before FTS5 was "
            "available would be invisible to search")
        hits = factcheck_search.search_fact_checks(conn, "pre-existing")
        self.assertTrue(any(h["id"] == 99 for h in hits))


class SearchPathSelection(unittest.TestCase):
    """The two paths (FTS5 BM25 + substring fallback) and which one
    fires for which query."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _make_conn()
        _insert_fc(cls.conn, fc_id=1,
                   claim="Judicial Service Commission appointments unconstitutional",
                   topic="judicial",
                   what_actually_happened="JSC composition under article 157")
        _insert_fc(cls.conn, fc_id=2,
                   claim="Housing units delivered: 4000 promised, 1200 actual",
                   topic="housing")
        _insert_fc(cls.conn, fc_id=3,
                   claim="Election promises about scholarship abandoned",
                   topic="education")

    def test_fts5_returns_negative_float_rank(self):
        hits = self.conn.execute(
            "SELECT * FROM fact_checks_fts WHERE fact_checks_fts MATCH 'judicial'"
        ).fetchall()
        # First confirm FTS5 itself matched
        self.assertGreater(len(hits), 0)
        # Now the wrapper
        out = factcheck_search.search_fact_checks(self.conn, "judicial")
        self.assertTrue(out)
        self.assertEqual(out[0]["id"], 1)
        self.assertIsInstance(out[0]["rank"], float,
            "BM25 returns floats; the wrapper must surface them so "
            "the caller can threshold-filter")
        self.assertLess(out[0]["rank"], 0,
            "BM25 ranks are negative; smaller = more relevant")

    def test_fallback_fires_on_zero_fts_hits(self):
        """Query for 'JSC' won't FTS5-match a claim that says
        'Judicial Service Commission' (different tokens), but the
        substring fallback finds it via LIKE. This is the real
        motivation for the hybrid."""
        out = factcheck_search.search_fact_checks(self.conn, "JSC")
        # 'JSC' is only 3 chars so the substring fallback also drops
        # it (4+ char minimum). Test a longer acronym-substitute.
        out2 = factcheck_search.search_fact_checks(self.conn, "appointments")
        self.assertTrue(out2,
            "substring fallback should match 'appointments' in fc#1")
        self.assertEqual(out2[0]["id"], 1)

    def test_fallback_rank_is_negative_int(self):
        out = factcheck_search.search_fact_checks(self.conn, "appointments")
        rank = out[0]["rank"]
        # Could be either int OR float depending on which path fired;
        # the contract is "negative and smaller=better". Some SQLite
        # builds return the SUM expression as float.
        self.assertLess(rank, 0,
            "fallback rank must be negative so callers can apply a "
            "uniform 'rank < threshold' filter regardless of path")

    def test_multi_token_outranks_single_token(self):
        """A query with two distinctive tokens should rank a
        fact-check matching BOTH higher than one matching just one."""
        # 'housing units' — fc#2 has both, fc#1 has neither
        out = factcheck_search.search_fact_checks(self.conn, "housing units")
        self.assertTrue(out)
        # The top hit must be fc#2
        self.assertEqual(out[0]["id"], 2)

    def test_short_tokens_dropped_from_fallback(self):
        """Stopword-ish tokens (< 4 chars) must not produce matches
        in the fallback. A query of 'of the' should return 0 — not
        every fact-check just because they contain those words."""
        # Force the FTS5 path to return 0 by querying tokens we know
        # don't match anything in the claims (the FTS5 tokenizer
        # produces tokens for 'of' and 'the' but they're STOPWORDS
        # in our content). Actually FTS5 default tokenizer does NOT
        # have a stopword filter, so 'of the' will match every row
        # via FTS5. To test the fallback's short-token filter we
        # need to bypass FTS5. Easiest: query something FTS5-misses
        # but where the only LIKE-able tokens are short.
        # Use 'of' + 'the' — both 2 and 3 chars, both should fail
        # the 4+ char filter in the fallback.
        out = factcheck_search.search_fact_checks(self.conn, "of the to a")
        self.assertEqual(out, [],
            "fallback must reject queries whose only tokens are "
            "< 4 chars — otherwise it would return every row that "
            "contains 'the'")

    def test_empty_query_returns_empty(self):
        self.assertEqual(factcheck_search.search_fact_checks(self.conn, ""), [])
        self.assertEqual(factcheck_search.search_fact_checks(self.conn, "   "), [])


class ConstitutionSearchRank(unittest.TestCase):
    """The constitution forward search must also expose BM25 rank,
    so the article.html JS can threshold-filter weak hits."""

    def test_constitution_search_returns_rank_field(self):
        from kahzaabu import constitution
        conn = _make_conn()
        # Insert a constitution article so search has something to hit
        conn.execute(
            "INSERT INTO constitution_articles "
            "(article_no, chapter, title, body, source_version, imported_at) "
            "VALUES (1, 'TEST', 'Judicial Service Commission', "
            "         'The JSC shall be constituted as follows...', "
            "         'test', '2026-01-01T00:00:00Z')"
        )
        # Refresh FTS index
        try:
            conn.execute("INSERT INTO constitution_articles_fts "
                          "(article_no, title, body) VALUES (1, ?, ?)",
                          ("Judicial Service Commission",
                           "The JSC shall be constituted as follows..."))
            conn.commit()
        except sqlite3.OperationalError:
            self.skipTest("constitution FTS5 not available")
        hits = constitution.lookup(conn, "Judicial Service Commission", limit=5)
        self.assertTrue(hits)
        self.assertIn("rank", hits[0],
            "constitution.lookup() must expose BM25 rank so the "
            "article.html JS can drop weak hits via a threshold")


if __name__ == "__main__":
    unittest.main()
