# SPDX-License-Identifier: Apache-2.0
"""Tests for the daily digest renderer (Slice F).

Pure-read function; no LLM, no network. Tests focus on:
  - empty-state messages render correctly (digest doesn't crash on
    a brand-new install with zero articles / fact-checks)
  - articles + fact-checks scraped/published within the window appear
  - things outside the window don't appear
  - article revisions surfaced
  - stale fact-checks (source_changed_at) surfaced
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import db, digest
from kahzaabu.claims_db import init_full_schema


def _mkconn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_full_schema(c)
    return c


def _insert_fc(conn, fc_id, claim="claim", created_at=None,
                source_changed_at=None, source_article_ids="[]"):
    conn.execute(
        """INSERT INTO fact_checks (
            id, category, claim, claim_date, topic, confidence,
            source_article_ids, evidence_quotes, created_at,
            published, verdict_label, truth_score_label,
            source_changed_at
        ) VALUES (?, 'LIE', ?, '2026-05-01', 'topic',
                  'reviewed', ?, '[]', ?, 1, 'REFUTED',
                  'MOSTLY_FALSE', ?)""",
        (fc_id, claim, source_article_ids,
         created_at or datetime.now(timezone.utc).isoformat(),
         source_changed_at)
    )
    conn.commit()


class DigestRendererEmptyDB(unittest.TestCase):
    """A brand-new install (zero articles, zero fact-checks) must
    still produce a valid markdown digest, not crash."""

    def test_empty_db_yields_well_formed_digest(self):
        conn = _mkconn()
        out = digest.render_digest(conn, window_hours=24)
        # Has the expected headings
        self.assertIn("# Kahzaabu digest", out)
        self.assertIn("## New articles", out)
        self.assertIn("## New fact-checks", out)
        self.assertIn("## Article edits detected", out)
        self.assertIn("## Fact-checks needing review", out)
        # Empty-state markers
        self.assertIn("None", out)
        # Window label
        self.assertIn("last 24 hours", out)


class DigestRendererWithData(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()
        # Article scraped TODAY (inside window)
        db.insert_article(self.conn, db.Article(
            id=100, language='EN', paired_id=None,
            category='press_release', category_id=1,
            title='Recent presidential announcement',
            body_text='Body', body_html='<p/>', reference='2026-100',
            published_date='2026-05-22',
            image_urls=[], raw_page_html='<html/>'))
        # Fact-check published today
        _insert_fc(self.conn, fc_id=500,
                    claim="claim about recent issue")

    def test_recent_article_appears(self):
        out = digest.render_digest(self.conn, window_hours=24)
        self.assertIn("Recent presidential announcement", out)
        self.assertIn("[100]", out)

    def test_recent_fact_check_appears(self):
        out = digest.render_digest(self.conn, window_hours=24)
        self.assertIn("fc#500", out)

    def test_revision_surfaces_in_digest(self):
        # Edit the article → triggers archive_revision
        db.insert_article(self.conn, db.Article(
            id=100, language='EN', paired_id=None,
            category='press_release', category_id=1,
            title='Recent presidential announcement',
            body_text='Body — UPDATED', body_html='<p/>',
            reference='2026-100', published_date='2026-05-22',
            image_urls=[], raw_page_html='<html/>'))
        out = digest.render_digest(self.conn, window_hours=24)
        # Section count should be 1, not 0
        self.assertRegex(out, r"## Article edits detected \(1\)")
        # And the revision should be listed
        self.assertIn("article 100", out)

    def test_stale_fact_check_surfaces(self):
        # Manually flag a fact-check as having a changed source
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE fact_checks SET source_changed_at = ? WHERE id = ?",
            (now, 500))
        self.conn.commit()
        out = digest.render_digest(self.conn, window_hours=24)
        self.assertRegex(out, r"## Fact-checks needing review \(1\)")
        self.assertIn("fc#500", out)


class DigestWindowing(unittest.TestCase):
    """Items OUTSIDE the requested window must not appear in the
    digest — otherwise running it daily would surface the same items
    over and over."""

    def test_old_article_excluded(self):
        conn = _mkconn()
        # Insert an article scraped a week ago by directly setting
        # scraped_at (db.insert_article would stamp now()).
        ts_old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        conn.execute(
            """INSERT INTO articles (id, language, paired_id, category,
                category_id, title, body_text, body_html, reference,
                published_date, image_urls, scraped_at, raw_page_html,
                content_hash)
               VALUES (200, 'EN', NULL, 'press_release', 1,
                       'OLD article title', 'body', NULL, '2026-100',
                       '2026-05-15', '[]', ?, '<raw>', NULL)""",
            (ts_old,)
        )
        conn.commit()
        out = digest.render_digest(conn, window_hours=24)
        self.assertNotIn("OLD article title", out,
            "Article scraped 7 days ago must NOT appear in a 24h "
            "digest — otherwise yesterday's news shows up every day")

    def test_window_label_in_output(self):
        """The digest's first paragraph says what window it covers
        so the reader knows what time range these numbers reflect."""
        conn = _mkconn()
        self.assertIn("last 24 hours",
                       digest.render_digest(conn, window_hours=24))
        self.assertIn("last 168h",
                       digest.render_digest(conn, window_hours=168))


class DigestWriteToFile(unittest.TestCase):
    def test_write_creates_parent_directory(self):
        import tempfile
        conn = _mkconn()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "subdir" / "digest.md"
            written = digest.write_digest(conn, out_path)
            self.assertTrue(Path(written).exists())
            self.assertIn("# Kahzaabu digest", Path(written).read_text())


if __name__ == "__main__":
    unittest.main()
