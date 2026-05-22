# SPDX-License-Identifier: Apache-2.0
"""Tests for article-revision tracking (ADR 0015).

Three layers:

  1. compute_content_hash — pure function. Deterministic, order-
     insensitive on image_urls, sensitive to text changes.
  2. generate_diff_summary — pure function. Surfaces numeric
     shifts (the "4 → 1" case), length deltas, image count,
     reference changes, title changes.
  3. db.insert_article integration — first insert has no
     revision row; same content re-inserted has none; different
     content writes a row with the old fields; multiple edits
     chain chronologically.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import db, revisions
from kahzaabu.claims_db import init_full_schema


def _mkconn() -> sqlite3.Connection:
    """Fresh in-memory DB with the full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_full_schema(conn)
    return conn


def _article(
    id_: int = 100,
    title: str = "Original title",
    body: str = "Body says 4 schools will open.",
    reference: str = "2026-100",
    image_urls=None,
) -> db.Article:
    return db.Article(
        id=id_,
        language="EN",
        paired_id=None,
        category="press_release",
        category_id=1,
        title=title,
        body_text=body,
        body_html=f"<p>{body}</p>",
        reference=reference,
        published_date="2026-05-22",
        image_urls=image_urls or ["https://x.com/a.jpg"],
        raw_page_html="<html>raw</html>",
    )


# ───────────────────────────────────────────────────────────────────
# compute_content_hash
# ───────────────────────────────────────────────────────────────────

class HashStability(unittest.TestCase):
    def test_deterministic(self):
        h1 = revisions.compute_content_hash("t", "b", "r", '["a","b"]')
        h2 = revisions.compute_content_hash("t", "b", "r", '["a","b"]')
        self.assertEqual(h1, h2)
        # SHA-256 hex is 64 chars
        self.assertEqual(len(h1), 64)

    def test_none_and_empty_string_equivalent(self):
        """A missing field should not differ from the literal empty
        string — otherwise an article that loses a field would
        trigger a fake revision even though the field was just
        normalised."""
        h_none = revisions.compute_content_hash(None, None, None, None)
        h_emp = revisions.compute_content_hash("", "", "", "")
        self.assertEqual(h_none, h_emp)

    def test_image_url_order_insensitivity(self):
        """The press office sometimes shuffles photos in the JSON
        array without changing the content. We don't want that to
        trigger a fake revision."""
        h1 = revisions.compute_content_hash(
            "t", "b", "r", '["url1", "url2", "url3"]')
        h2 = revisions.compute_content_hash(
            "t", "b", "r", '["url3", "url1", "url2"]')
        self.assertEqual(h1, h2,
            "Hash must be invariant under image URL ordering — "
            "the same set of photos shouldn't trigger a revision")

    def test_text_change_yields_different_hash(self):
        a = revisions.compute_content_hash("t", "4 schools", "r", "[]")
        b = revisions.compute_content_hash("t", "1 school",  "r", "[]")
        self.assertNotEqual(a, b)

    def test_title_change_yields_different_hash(self):
        a = revisions.compute_content_hash("Old title", "b", "r", "[]")
        b = revisions.compute_content_hash("New title", "b", "r", "[]")
        self.assertNotEqual(a, b)

    def test_image_added_yields_different_hash(self):
        a = revisions.compute_content_hash("t", "b", "r", '["x"]')
        b = revisions.compute_content_hash("t", "b", "r", '["x", "y"]')
        self.assertNotEqual(a, b)

    def test_malformed_json_does_not_crash(self):
        # The function should hash gracefully even if image_urls is
        # garbage — defensive against bad data.
        h = revisions.compute_content_hash("t", "b", "r", "not-json")
        self.assertEqual(len(h), 64)


# ───────────────────────────────────────────────────────────────────
# generate_diff_summary
# ───────────────────────────────────────────────────────────────────

class DiffSummaryGenerator(unittest.TestCase):
    def test_numeric_shift_detected(self):
        """The original motivating case: 4 → 1."""
        old = {"body_text": "The spokesperson said 4 schools will open."}
        new = {"body_text": "The spokesperson said 1 school will open."}
        summary = revisions.generate_diff_summary(old, new)
        self.assertIn("4", summary,
            "Diff must mention the removed number — that's the "
            "fact-check-relevant signal")
        self.assertIn("1", summary,
            "Diff must mention the added number")

    def test_image_count_change_detected(self):
        old = {"image_urls": '["a", "b", "c"]'}
        new = {"image_urls": '["a"]'}
        self.assertIn("3", revisions.generate_diff_summary(old, new))
        self.assertIn("1", revisions.generate_diff_summary(old, new))

    def test_title_change_flagged(self):
        old = {"title": "Old title"}
        new = {"title": "New title"}
        self.assertIn("title", revisions.generate_diff_summary(old, new))

    def test_reference_change_flagged(self):
        old = {"reference": "2026-100"}
        new = {"reference": "2026-101"}
        summary = revisions.generate_diff_summary(old, new)
        self.assertIn("reference", summary)
        self.assertIn("2026-100", summary)
        self.assertIn("2026-101", summary)

    def test_length_delta_above_threshold_flagged(self):
        old = {"body_text": "short"}
        new = {"body_text": "x" * 200}
        self.assertIn("length", revisions.generate_diff_summary(old, new))

    def test_length_delta_below_threshold_not_flagged(self):
        """Whitespace-only or punctuation tweaks shouldn't trigger
        a length diff — would be noise."""
        old = {"body_text": "hello world"}
        new = {"body_text": "hello  world"}   # one extra space
        summary = revisions.generate_diff_summary(old, new)
        self.assertNotIn("length", summary,
            "1-char diff should not trigger length flag — would "
            "make the summary noisy for trivial whitespace edits")

    def test_no_substantive_change_says_so(self):
        old = {"title": "t", "body_text": "b", "reference": "r",
               "image_urls": "[]"}
        new = old.copy()
        summary = revisions.generate_diff_summary(old, new)
        self.assertIn("no detectable", summary)


# ───────────────────────────────────────────────────────────────────
# db.insert_article integration
# ───────────────────────────────────────────────────────────────────

class InsertArticleIntegration(unittest.TestCase):
    """The scraper's single upsert path is db.insert_article.
    Verify it correctly compares-and-archives."""

    def test_first_insert_creates_no_revision(self):
        conn = _mkconn()
        db.insert_article(conn, _article(body="4 schools open"))
        n = conn.execute("SELECT COUNT(*) FROM article_revisions").fetchone()[0]
        self.assertEqual(n, 0,
            "First insert must not create a revision — there's "
            "no prior version to archive")
        # content_hash MUST be stored
        row = conn.execute(
            "SELECT content_hash FROM articles WHERE id=100 AND language='EN'"
        ).fetchone()
        self.assertIsNotNone(row["content_hash"])
        self.assertEqual(len(row["content_hash"]), 64)

    def test_same_content_reinsert_creates_no_revision(self):
        """Idempotency: re-scraping unchanged content must not
        write a revision row."""
        conn = _mkconn()
        db.insert_article(conn, _article(body="4 schools open"))
        db.insert_article(conn, _article(body="4 schools open"))
        n = conn.execute("SELECT COUNT(*) FROM article_revisions").fetchone()[0]
        self.assertEqual(n, 0,
            "Same content re-inserted must NOT write a revision — "
            "would create one phantom revision per scrape cycle")

    def test_different_content_creates_one_revision(self):
        """The headline scenario: press office edits 4 → 1."""
        conn = _mkconn()
        db.insert_article(conn, _article(body="The spokesperson said 4 schools will open."))
        db.insert_article(conn, _article(body="The spokesperson said 1 school will open."))
        n = conn.execute("SELECT COUNT(*) FROM article_revisions").fetchone()[0]
        self.assertEqual(n, 1)
        rev = conn.execute(
            "SELECT * FROM article_revisions LIMIT 1"
        ).fetchone()
        # Archived row holds the OLD body
        self.assertIn("4", rev["body_text"],
            "Archived revision must contain the OLD body (the '4' "
            "version), not the new content")
        self.assertNotIn("1 school", rev["body_text"])
        # diff_summary must surface the numeric shift
        self.assertIn("4", rev["diff_summary"])
        self.assertIn("1", rev["diff_summary"])
        # Article now has the NEW body
        new_body = conn.execute(
            "SELECT body_text FROM articles WHERE id=100 AND language='EN'"
        ).fetchone()["body_text"]
        self.assertIn("1 school", new_body)

    def test_multiple_edits_chain_chronologically(self):
        """Repeated edits should produce N revisions where N = number
        of distinct contents minus 1."""
        conn = _mkconn()
        db.insert_article(conn, _article(body="version 1"))
        db.insert_article(conn, _article(body="version 2"))
        db.insert_article(conn, _article(body="version 3"))
        n = conn.execute("SELECT COUNT(*) FROM article_revisions").fetchone()[0]
        self.assertEqual(n, 2,
            "Two edits (v1→v2, v2→v3) must produce two revision rows")
        # Oldest revision archives "version 1"
        rows = revisions.list_revisions(conn, 100, "EN")
        self.assertEqual(len(rows), 2)
        full_first = revisions.get_revision(conn, rows[0]["id"])
        self.assertIn("version 1", full_first["body_text"])

    def test_legacy_null_hash_does_not_trigger_revision(self):
        """A row written before the slice-15 migration has
        content_hash = NULL. Subsequent scrapes must treat NULL as
        'first observation' — store the hash but DON'T write a
        revision."""
        conn = _mkconn()
        # Simulate a pre-migration row: insert with the legacy
        # schema (no content_hash). We do this by hand because
        # db.insert_article computes the hash now.
        from datetime import datetime, timezone
        conn.execute(
            """INSERT INTO articles (id, language, paired_id, category,
                category_id, title, body_text, body_html, reference,
                published_date, image_urls, scraped_at, raw_page_html,
                content_hash)
               VALUES (100, 'EN', NULL, 'press_release', 1,
                       'Title', 'Body says 4 schools', NULL,
                       '2026-100', '2026-05-22', '[]',
                       ?, '<raw>', NULL)""",
            (datetime.now(timezone.utc).isoformat(),)
        )
        conn.commit()
        # Now scrape "the same article" — different text but
        # legacy row had NULL hash. Should NOT create a revision
        # (we can't tell whether legitimate change happened).
        db.insert_article(conn, _article(body="Body says 4 schools"))
        n = conn.execute("SELECT COUNT(*) FROM article_revisions").fetchone()[0]
        self.assertEqual(n, 0,
            "Legacy NULL-hash rows must not trigger phantom "
            "revisions on the first post-migration scrape — there's "
            "no reliable old content to compare against")
        # But the hash must now be stored.
        h = conn.execute(
            "SELECT content_hash FROM articles WHERE id=100 AND language='EN'"
        ).fetchone()["content_hash"]
        self.assertIsNotNone(h)
        self.assertEqual(len(h), 64)


# ───────────────────────────────────────────────────────────────────
# revisions.list_revisions / get_revision
# ───────────────────────────────────────────────────────────────────

class RevisionsAPI(unittest.TestCase):
    def test_list_filters_by_language(self):
        conn = _mkconn()
        # EN: 1 edit
        db.insert_article(conn, _article(id_=200, body="EN v1"))
        db.insert_article(conn, _article(id_=200, body="EN v2"))
        # DV: 1 edit (same id, different language — schema allows)
        dv = _article(id_=200, body="DV v1")
        dv.language = "DV"
        db.insert_article(conn, dv)
        dv2 = _article(id_=200, body="DV v2")
        dv2.language = "DV"
        db.insert_article(conn, dv2)

        en_only = revisions.list_revisions(conn, 200, "EN")
        dv_only = revisions.list_revisions(conn, 200, "DV")
        all_ = revisions.list_revisions(conn, 200)
        self.assertEqual(len(en_only), 1)
        self.assertEqual(len(dv_only), 1)
        self.assertEqual(len(all_), 2)

    def test_get_revision_returns_full_row(self):
        conn = _mkconn()
        db.insert_article(conn, _article(body="v1 with 4 things"))
        db.insert_article(conn, _article(body="v2 with 1 thing"))
        rows = revisions.list_revisions(conn, 100, "EN")
        self.assertEqual(len(rows), 1)
        full = revisions.get_revision(conn, rows[0]["id"])
        self.assertIn("body_text", full)
        self.assertIn("v1 with 4", full["body_text"])

    def test_get_revision_returns_none_for_missing(self):
        conn = _mkconn()
        self.assertIsNone(revisions.get_revision(conn, 9999999))


if __name__ == "__main__":
    unittest.main()
