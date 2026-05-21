# SPDX-License-Identifier: Apache-2.0
"""Unit tests for kahzaabu.constitution — parser + lookup.

Pins the contract: the 2008 PDF must parse into exactly 301 main articles
with clean titles, chapter assignments, and non-empty bodies. If a future
edit to the parser regresses any of these, the test catches it before
the agent starts returning broken citations.

Run:
    .venv/bin/python -m unittest tests.test_constitution_parser
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import constitution


# Articles with known titles — spot-checks across the document.
EXPECTED_TITLES = {
    1: "Constitution",
    10: "State Religion",
    11: "National Language",
    21: "Right to life",
    27: ("Freedom", "Expression"),   # may be one of these (parser may pull either)
    100: ("Removal of President", "Vice President"),
    108: "Manner of Presidential election",
    109: "Qualifications for election as President",
    141: "Judiciary",                # Chapter VI opener
    253: "State of Emergency",       # near the Emergency chapter
}


class ConstitutionParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not constitution.DEFAULT_TXT.exists():
            raise unittest.SkipTest(
                f"Constitution text not present at {constitution.DEFAULT_TXT} — "
                "skipping parser tests"
            )
        cls.records = constitution.parse_constitution()

    def test_parses_all_301_main_articles(self):
        nos = sorted(r["article_no"] for r in self.records)
        self.assertEqual(len(self.records), 301,
                          f"Expected 301 articles, got {len(self.records)}")
        self.assertEqual(nos[0], 1)
        self.assertEqual(nos[-1], 301)
        missing = sorted(set(range(1, 302)) - set(nos))
        self.assertEqual(missing, [],
                          f"Missing article numbers: {missing}")

    def test_every_article_has_non_empty_body(self):
        for r in self.records:
            with self.subTest(article_no=r["article_no"]):
                self.assertTrue(
                    r["body"].strip(),
                    f"Article {r['article_no']} has empty body",
                )

    def test_every_article_has_a_chapter(self):
        for r in self.records:
            with self.subTest(article_no=r["article_no"]):
                self.assertTrue(
                    r["chapter"].strip(),
                    f"Article {r['article_no']} has no chapter assignment",
                )

    def test_every_article_has_a_title(self):
        empty = [r["article_no"] for r in self.records
                 if not r["title"].strip()]
        self.assertEqual(empty, [],
                          f"Articles without titles: {empty}")

    def test_no_title_ends_in_body_marker(self):
        """Catches the bug where 'following:' or similar body lead-ins
        get parsed as part of a title."""
        body_markers = (":", ";", ",", "—", "(")
        offenders = [
            (r["article_no"], r["title"])
            for r in self.records
            if r["title"].rstrip().endswith(body_markers)
        ]
        self.assertEqual(offenders, [],
                          f"Titles ending in body markers: {offenders}")

    def test_spot_check_known_titles(self):
        by_no = {r["article_no"]: r for r in self.records}
        for no, expected in EXPECTED_TITLES.items():
            with self.subTest(article_no=no):
                self.assertIn(no, by_no)
                actual = by_no[no]["title"]
                if isinstance(expected, tuple):
                    self.assertTrue(
                        any(s.lower() in actual.lower() for s in expected),
                        f"Article {no} title {actual!r} contains none of "
                        f"the expected substrings {expected}",
                    )
                else:
                    self.assertIn(expected.lower(), actual.lower(),
                                   f"Article {no} title should contain "
                                   f"{expected!r}, got {actual!r}")

    def test_chapter_assignments_match_known_boundaries(self):
        """Articles 1-15 must be in Chapter I; 16-69 in Chapter II; etc."""
        by_no = {r["article_no"]: r for r in self.records}
        cases = [
            (1, "I"),    (15, "I"),
            (16, "II"),  (69, "II"),
            (70, "III"),
            (108, "IV"),
        ]
        for no, expected_roman in cases:
            with self.subTest(article_no=no, chapter=expected_roman):
                r = by_no.get(no)
                self.assertIsNotNone(r)
                self.assertTrue(
                    r["chapter"].startswith(expected_roman + " "),
                    f"Article {no} expected chapter {expected_roman}, "
                    f"got {r['chapter']!r}",
                )


class ConstitutionLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not constitution.DEFAULT_TXT.exists():
            raise unittest.SkipTest("constitution text not present")
        cls.conn = sqlite3.connect(":memory:")
        cls.conn.row_factory = sqlite3.Row
        constitution.import_constitution(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_lookup_religion_finds_article_10(self):
        hits = constitution.lookup(self.conn, "religion", limit=5)
        nos = [h["article_no"] for h in hits]
        self.assertIn(10, nos,
                       f"Article 10 (State Religion) should be in results "
                       f"for 'religion', got {nos}")

    def test_lookup_president_returns_executive_chapter(self):
        hits = constitution.lookup(self.conn, "President", limit=5)
        self.assertGreater(len(hits), 0)
        # At least one hit should be in Chapter IV (The President)
        chapters = [h["chapter"] for h in hits]
        self.assertTrue(any("IV" in c for c in chapters),
                         f"Expected at least one Chapter IV hit, got {chapters}")

    def test_lookup_no_matches_returns_empty(self):
        hits = constitution.lookup(self.conn,
                                     "xyzzy-no-such-token-anywhere", limit=5)
        self.assertEqual(hits, [])

    def test_lookup_respects_limit(self):
        hits = constitution.lookup(self.conn, "the", limit=3)
        self.assertLessEqual(len(hits), 3)

    def test_lookup_returns_clean_records(self):
        hits = constitution.lookup(self.conn, "judge", limit=3)
        for h in hits:
            self.assertIn("article_no", h)
            self.assertIn("title", h)
            self.assertIn("body", h)
            self.assertIn("chapter", h)
            self.assertIn("source_version", h)

    def test_fts_columns_match_bm25_weights(self):
        """C3 guard: bm25() weights are positional. If FTS_SQL column
        order ever changes without _BM25_WEIGHTS being updated, the
        ranking silently breaks. Pin both."""
        # _FTS_COLUMNS starts with article_no (UNINDEXED, not weighted);
        # _BM25_WEIGHTS covers the indexed columns that follow.
        self.assertEqual(constitution._FTS_COLUMNS[0], "article_no")
        self.assertEqual(constitution._FTS_COLUMNS[1:], ("title", "body"))
        self.assertEqual(len(constitution._BM25_WEIGHTS),
                          len(constitution._FTS_COLUMNS) - 1)
        # Title weight must be >= body weight (the whole point of weighting).
        self.assertGreaterEqual(constitution._BM25_WEIGHTS[0],
                                  constitution._BM25_WEIGHTS[1])

    def test_bm25_quality_known_queries(self):
        """Eight queries with a known-correct top hit. If BM25 weights or
        the FTS5 schema regress, the top-1 ordering shifts and this fires.
        The 10:1 title-vs-body weighting is tuned to keep these stable.

        IF THIS TEST FAILS:

        1. If you just re-imported a newer constitution PDF (e.g. an
           amended text), the article *numbering* may have shifted —
           update the fixture (this dict) to the new correct article
           numbers. The test pins current expectations, not eternal truth.

        2. If you tuned _BM25_WEIGHTS or changed FTS_SQL, that's the
           more likely culprit — investigate before adjusting the fixture.

        3. If the parser changed and article bodies / titles differ,
           the lookup may rank differently — investigate parser first.

        Don't blindly update the fixture without first checking which of
        the three caused the shift; you'd be regressing search quality
        otherwise.
        """
        # (query, expected_article, mode):
        #   mode='top1' — must be the first result
        #   mode='top3' — must appear in the first 3 (used where the
        #                 'correct' answer is genuinely ambiguous between
        #                 multiple related articles)
        cases = [
            ("religion",                10, "top1"),
            ("non-Muslim citizen",       9, "top1"),
            ("judicial independence",  141, "top1"),
            ("presidential election",  108, "top1"),
            ("freedom of expression",   27, "top1"),
            ("tenets of Islam",         16, "top1"),
            # 'removal of president' relates to Art 100 + 122 — accept either
            ("removal of president",   100, "top3"),
            ("removal of president",   122, "top3"),
            # 'emergency' is referenced by 253-258 and 267 — top-3 is enough
            ("emergency",              253, "top3"),
            ("emergency",              267, "top3"),
        ]
        for query, expected, mode in cases:
            with self.subTest(query=query, mode=mode, expected=expected):
                hits = constitution.lookup(self.conn, query, limit=3)
                self.assertGreater(len(hits), 0,
                                    f"{query!r} returned no hits")
                nos = [h["article_no"] for h in hits]
                if mode == "top1":
                    self.assertEqual(
                        hits[0]["article_no"], expected,
                        f"{query!r} expected Art. {expected} as top hit, "
                        f"got Art. {hits[0]['article_no']} "
                        f"({hits[0]['title']!r}). Top-3: {nos}",
                    )
                else:  # top3
                    self.assertIn(
                        expected, nos,
                        f"{query!r} expected Art. {expected} in top-3, "
                        f"got {nos}",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
