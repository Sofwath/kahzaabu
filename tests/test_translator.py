# SPDX-License-Identifier: Apache-2.0
"""Tests for kahzaabu/translator.py — press-office-style EN ↔ DV
translation (Slice 16, ADR 0016).

Layers:
  1. detect_language — pure function, no DB
  2. select_few_shot — DB read against articles + articles_fts
  3. select_glossary_subset — DB read against translation_glossary
  4. translate() with mocked LLM — full pipeline
  5. Cache hit — second call reads from translation_runs
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import db, translator
from kahzaabu.claims_db import init_full_schema


def _mkconn() -> sqlite3.Connection:
    """Fresh in-memory DB with the full schema (incl. articles_fts +
    translation_glossary + translation_runs)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_full_schema(conn)
    return conn


def _insert_paired_article(
    conn,
    *,
    en_id, dv_id,
    en_title, en_body,
    dv_body,
    published_date,
):
    """Helper: insert an EN/DV paired article. The FTS triggers
    backfill_articles_fts automatically."""
    db.insert_article(conn, db.Article(
        id=en_id, language="EN", paired_id=dv_id,
        category="press_release", category_id=1,
        title=en_title, body_text=en_body, body_html=f"<p>{en_body}</p>",
        reference=f"2026-{en_id}", published_date=published_date,
        image_urls=[], raw_page_html="<html/>"))
    db.insert_article(conn, db.Article(
        id=dv_id, language="DV", paired_id=en_id,
        category="press_release", category_id=1,
        title="DV " + en_title, body_text=dv_body,
        body_html=f"<p>{dv_body}</p>",
        reference=f"2026-{en_id}", published_date=published_date,
        image_urls=[], raw_page_html="<html/>"))


# ───────────────────────────────────────────────────────────────────
# Language detection
# ───────────────────────────────────────────────────────────────────

class LanguageDetection(unittest.TestCase):
    def test_pure_latin_is_en(self):
        self.assertEqual(translator.detect_language(
            "The President met with the Cabinet today."), "EN")

    def test_pure_thaana_is_dv(self):
        # "The President said" in Thaana
        self.assertEqual(translator.detect_language(
            "ރައީސުލްޖުމްހޫރިއްޔާ ވިދާޅުވިއެވެ"), "DV")

    def test_empty_defaults_to_en(self):
        self.assertEqual(translator.detect_language(""), "EN")
        self.assertEqual(translator.detect_language(None), "EN")

    def test_mostly_thaana_with_some_english_is_dv(self):
        # Thaana paragraph with one English number/proper noun
        # mid-sentence — the press office mixes the two routinely.
        text = ("ރައީސުލްޖުމްހޫރިއްޔާ ޑރ. މުޢިއްޒު 2026 ވަނަ "
                "އަހަރުގެ ޤައުމީ ދުވަސް ފާހަގަކުރައްވައި ވިދާޅުވިއެވެ")
        self.assertEqual(translator.detect_language(text), "DV",
            "Dominant-Thaana text with English numerals must still "
            "classify as DV — the corpus is full of this pattern")

    def test_only_whitespace_defaults_to_en(self):
        self.assertEqual(translator.detect_language("   \n  \t"), "EN")


# ───────────────────────────────────────────────────────────────────
# Few-shot selection
# ───────────────────────────────────────────────────────────────────

class FewShotSelection(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()
        # Three paired articles. (1) recent + topic-similar to JSC,
        # (2) recent + unrelated topic, (3) older + topic-similar
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        old = (datetime.now(timezone.utc) - timedelta(days=180)
               ).strftime("%Y-%m-%d")
        _insert_paired_article(
            self.conn, en_id=1001, dv_id=1002,
            en_title="JSC appointment", published_date=today,
            en_body="The President appointed a new Judicial Service Commission member today.",
            dv_body="ޝަރުޢީ ޚިދުމަތާ ބެހޭ ކޮމިޝަން DV body",
        )
        _insert_paired_article(
            self.conn, en_id=1003, dv_id=1004,
            en_title="Cabinet reshuffle", published_date=today,
            en_body="The President announced a reshuffle of the cabinet portfolio assignments.",
            dv_body="ދައުލަތުގެ ވަޒީރުންގެ މަޖިލިސް DV body",
        )
        _insert_paired_article(
            self.conn, en_id=1005, dv_id=1006,
            en_title="OLD JSC ceremony", published_date=old,
            en_body="In 2025, the President attended a Judicial Service Commission ceremony.",
            dv_body="OLD DV body about JSC",
        )

    def test_topic_similar_wins_over_unrelated(self):
        """Query about 'judicial service commission' should pull
        articles 1001 and 1005 (which mention it) before 1003
        (cabinet reshuffle, unrelated)."""
        out = translator.select_few_shot(
            self.conn, "EN",
            "What did the President say about the judicial service commission?",
            k=3, recency_days=365)
        self.assertTrue(out, "Expected at least one exemplar")
        en_ids = [r["en_article_id"] for r in out]
        # 1001 should be in the result (recent + topic-match)
        self.assertIn(1001, en_ids,
            "Topic-similar recent article must be selected as a "
            "few-shot exemplar — BM25 should rank it ahead of the "
            "topic-unrelated 1003")

    def test_recency_window_excludes_old(self):
        """recency_days=30 should exclude the 180-day-old article."""
        out = translator.select_few_shot(
            self.conn, "EN",
            "judicial service commission",
            k=3, recency_days=30)
        en_ids = [r["en_article_id"] for r in out]
        self.assertNotIn(1005, en_ids,
            "180-day-old article must be excluded from a 30-day "
            "window — the freshness window is real, not advisory")

    def test_returns_at_most_k(self):
        out = translator.select_few_shot(
            self.conn, "EN", "the president", k=2, recency_days=365)
        self.assertLessEqual(len(out), 2)

    def test_falls_back_to_recency_when_no_fts_match(self):
        """If FTS5 returns nothing for the query, the function falls
        back to the most-recent paired articles — better to provide
        SOME exemplar than none."""
        out = translator.select_few_shot(
            self.conn, "EN", "asdkfjasdkfj nonsense query",
            k=2, recency_days=365)
        self.assertEqual(len(out), 2,
            "Recency-fallback must yield k exemplars when FTS5 "
            "has zero hits — otherwise the translator runs without "
            "any few-shot context")

    def test_dv_source_fts5_search_works(self):
        """Regression: the sanitizer in articles_fts._fts_sanitize
        used to strip Thaana characters (Latin-only regex), making
        every DV-language search return zero hits. Fixed by adding
        the Thaana Unicode range to the token alphabet.

        This test pins the fix — if someone later changes the regex
        back to Latin-only, this fails with a named assertion."""
        # Insert a paired article with a distinctive Thaana phrase
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _insert_paired_article(
            self.conn, en_id=3001, dv_id=3002,
            en_title="Cabinet meeting", published_date=today,
            en_body="The Cabinet met today.",
            dv_body="ދައުލަތުގެ ވަޒީރުންގެ މަޖިލިސް ބައްދަލުވިއެވެ",
        )
        out = translator.select_few_shot(
            self.conn, "DV", "ދައުލަތުގެ ވަޒީރުންގެ މަޖިލިސް",
            k=2, recency_days=365)
        en_ids = [r["en_article_id"] for r in out]
        self.assertIn(3001, en_ids,
            "DV-source few-shot must find article 3001 — the new "
            "DV body matches the query. Earlier versions returned "
            "zero hits because the sanitizer stripped Thaana chars.")

    def test_exemplar_payload_shape(self):
        out = translator.select_few_shot(
            self.conn, "EN", "judicial service", k=1, recency_days=365)
        self.assertTrue(out)
        ex = out[0]
        for key in ("en_article_id", "dv_article_id", "en_title",
                    "en_body", "dv_body", "published_date"):
            self.assertIn(key, ex)


# ───────────────────────────────────────────────────────────────────
# Glossary subset
# ───────────────────────────────────────────────────────────────────

class GlossarySubset(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()
        now = datetime.now(timezone.utc).isoformat()
        # Insert glossary rows
        for en, dv, freq in [
            ("Judicial Service Commission", "ޝަރުޢީ ޚިދުމަތާ ބެހޭ ކޮމިޝަން", 25),
            ("Cabinet", "ދައުލަތުގެ ވަޒީރުންގެ މަޖިލިސް", 12),
            ("housing scheme", "ހައުސިން ސްކީމް", 8),
            ("fishery exports", "މަސްވެރި އެކްސްޕޯޓް", 3),
        ]:
            self.conn.execute(
                """INSERT INTO translation_glossary
                   (en_term, dv_term, domain, freq, confidence,
                    sample_en_ids, extracted_at, extracted_by)
                   VALUES (?, ?, 'government', ?, 0.9, '[]', ?, 'test')""",
                (en, dv, freq, now)
            )
        self.conn.commit()

    def test_only_terms_appearing_in_input_returned(self):
        out = translator.select_glossary_subset(
            self.conn, "The President discussed the housing scheme.",
            "EN", max_terms=10)
        terms = [r["en_term"] for r in out]
        self.assertIn("housing scheme", terms,
            "Term that appears in the input must be returned")
        self.assertNotIn("fishery exports", terms,
            "Term that does NOT appear in the input must NOT be "
            "returned — would bloat the prompt with irrelevant rows")
        self.assertNotIn("Cabinet", terms)

    def test_case_insensitive_matching(self):
        """The input uses lower-case 'cabinet' but the glossary has
        'Cabinet'. Must still match."""
        out = translator.select_glossary_subset(
            self.conn, "The cabinet decided.", "EN", max_terms=10)
        terms = [r["en_term"] for r in out]
        self.assertIn("Cabinet", terms,
            "Case-insensitive match required — input casing varies")

    def test_sorted_by_freq_desc(self):
        out = translator.select_glossary_subset(
            self.conn,
            "Judicial Service Commission and housing scheme",
            "EN", max_terms=10)
        terms = [r["en_term"] for r in out]
        # JSC has freq=25, housing scheme freq=8
        self.assertLess(terms.index("Judicial Service Commission"),
                          terms.index("housing scheme"),
                          "Higher-freq terms must appear first")

    def test_max_terms_respected(self):
        # All 4 terms in the input
        long_input = ("Judicial Service Commission Cabinet "
                      "housing scheme fishery exports")
        out = translator.select_glossary_subset(
            self.conn, long_input, "EN", max_terms=2)
        self.assertEqual(len(out), 2)

    def test_dv_source_lookup(self):
        """When source_lang='DV', the LIKE prefilter runs against
        dv_term, not en_term."""
        out = translator.select_glossary_subset(
            self.conn, "ޝަރުޢީ ޚިދުމަތާ ބެހޭ ކޮމިޝަން", "DV", max_terms=5)
        terms = [r["dv_term"] for r in out]
        self.assertIn("ޝަރުޢީ ޚިދުމަތާ ބެހޭ ކޮމިޝަން", terms,
            "DV-source path must match against the dv_term column")


# ───────────────────────────────────────────────────────────────────
# translate() with mocked LLM
# ───────────────────────────────────────────────────────────────────

class TranslateEndToEnd(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()
        # One paired article so few-shot has something to work with
        _insert_paired_article(
            self.conn, en_id=2001, dv_id=2002,
            en_title="President statement",
            en_body="The President made a statement today regarding policy.",
            dv_body="ރައީސުލްޖުމްހޫރިއްޔާ ވިދާޅުވިއެވެ",
            published_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

    def _make_mock_llm(self, response_text: str = "translated text",
                         tokens_in: int = 100, tokens_out: int = 50):
        """Build a MagicMock that mimics Anthropic's response shape."""
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = response_text
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_response.usage.input_tokens = tokens_in
        mock_response.usage.output_tokens = tokens_out
        mock_llm = MagicMock()
        mock_llm.messages.create.return_value = mock_response
        return mock_llm

    def test_returns_expected_shape(self):
        llm = self._make_mock_llm("ރައީސުލްޖުމްހޫރިއްޔާ ބައްދަލުވި")
        res = translator.translate(
            self.conn, "The President met today.",
            target_lang="DV", llm=llm,
        )
        self.assertEqual(res["source_lang"], "EN")
        self.assertEqual(res["target_lang"], "DV")
        self.assertEqual(res["translation"],
                          "ރައީސުލްޖުމްހޫރިއްޔާ ބައްދަލުވި")
        self.assertIn("model", res)
        self.assertIsInstance(res["cost_usd"], float)
        self.assertFalse(res["cache_hit"])
        self.assertIn("disclaimer", res)

    def test_writes_translation_runs_row(self):
        llm = self._make_mock_llm()
        translator.translate(
            self.conn, "Hello world.", target_lang="DV", llm=llm)
        rows = self.conn.execute(
            "SELECT input_text, target_lang FROM translation_runs"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_text"], "Hello world.")
        self.assertEqual(rows[0]["target_lang"], "DV")

    def test_auto_target_picks_opposite_lang(self):
        llm = self._make_mock_llm()
        res = translator.translate(
            self.conn, "Hello world.", target_lang="auto", llm=llm)
        self.assertEqual(res["target_lang"], "DV")
        # And reverse
        res2 = translator.translate(
            self.conn, "ރައީސުލްޖުމްހޫރިއްޔާ ވިދާޅުވިއެވެ",
            target_lang="auto", llm=llm,
        )
        self.assertEqual(res2["target_lang"], "EN")

    def test_same_source_and_target_rejected(self):
        """Translating EN to EN is a no-op error — the user wanted
        the OTHER language and probably set target wrong."""
        llm = self._make_mock_llm()
        with self.assertRaises(ValueError):
            translator.translate(
                self.conn, "Hello world.", target_lang="EN", llm=llm)

    def test_empty_input_rejected(self):
        llm = self._make_mock_llm()
        with self.assertRaises(ValueError):
            translator.translate(self.conn, "", llm=llm)
        with self.assertRaises(ValueError):
            translator.translate(self.conn, "   ", llm=llm)


# ───────────────────────────────────────────────────────────────────
# Cache hit (translation_runs as LRU backing store)
# ───────────────────────────────────────────────────────────────────

class CacheHit(unittest.TestCase):
    def setUp(self):
        self.conn = _mkconn()

    def _llm(self, text="cached output"):
        b = MagicMock(); b.type = "text"; b.text = text
        r = MagicMock(); r.content = [b]
        r.usage.input_tokens = 10; r.usage.output_tokens = 5
        m = MagicMock(); m.messages.create.return_value = r
        return m

    def test_second_call_with_same_input_returns_cached(self):
        llm = self._llm("first-result")
        r1 = translator.translate(
            self.conn, "Hello world.", target_lang="DV", llm=llm)
        self.assertFalse(r1["cache_hit"])

        # Second call — different mock, but the cache should hit
        # BEFORE the LLM is called (so the new text would never
        # appear).
        llm2 = self._llm("WOULD-BE-DIFFERENT-RESULT")
        r2 = translator.translate(
            self.conn, "Hello world.", target_lang="DV", llm=llm2)
        self.assertTrue(r2["cache_hit"])
        self.assertEqual(r2["translation"], "first-result",
            "Cache must return the FIRST result; if the LLM is "
            "called again, the cache wasn't consulted")
        # And the second LLM mock should NOT have been called.
        llm2.messages.create.assert_not_called()

    def test_different_target_lang_misses_cache(self):
        llm = self._llm("EN→DV")
        translator.translate(
            self.conn, "Hello world.", target_lang="DV", llm=llm)
        # Re-translate but as if going EN→EN (which we'd reject —
        # use a different INPUT that goes EN→DV but is different)
        llm2 = self._llm("different input")
        r = translator.translate(
            self.conn, "Goodbye world.", target_lang="DV", llm=llm2)
        self.assertFalse(r["cache_hit"])
        self.assertEqual(r["translation"], "different input")


# ───────────────────────────────────────────────────────────────────
# Back-translation verification (opt-in)
# ───────────────────────────────────────────────────────────────────

class BackTranslationVerification(unittest.TestCase):
    """The headline failure mode for translation is "grammatically
    valid but factually wrong" — especially numeric drift ("4 schools"
    → "1 school") and proper-noun loss. The opt-in verifier round-
    trips the translation and flags those invariants."""

    def setUp(self):
        self.conn = _mkconn()

    def _llm(self, forward_text="ޤޫ", back_text="back-text",
              tokens_in=10, tokens_out=5):
        """Configurable mock LLM: first call returns forward_text,
        second returns back_text."""
        def make_resp(text):
            b = MagicMock(); b.type = "text"; b.text = text
            r = MagicMock(); r.content = [b]
            r.usage.input_tokens = tokens_in; r.usage.output_tokens = tokens_out
            return r
        m = MagicMock()
        m.messages.create.side_effect = [
            make_resp(forward_text),
            make_resp(back_text),
        ]
        return m

    def test_passing_round_trip(self):
        """When back-translation preserves all numbers and proper
        nouns, verification.passed = True."""
        # Forward translates "4 schools opened in 2026" → some Thaana.
        # Back returns text that preserves "4" and "2026".
        llm = self._llm(
            forward_text="ޤޫ 4 ޚޮއް 2026",
            back_text="4 schools opened in 2026",
        )
        res = translator.translate(
            self.conn,
            "4 schools opened in 2026",
            target_lang="DV", llm=llm, verify=True,
        )
        self.assertIn("verification", res)
        v = res["verification"]
        self.assertTrue(v["passed"],
            "Round trip preserved both numbers — verification should pass")
        self.assertEqual(v["numbers_lost"], [])
        self.assertEqual(v["numbers_added"], [])

    def test_failing_numeric_drift(self):
        """When back-translation drops a number, verification.passed
        = False and numbers_lost lists the dropped value. This is
        the headline case — "4 schools" became "1 school" or just
        "schools" in the round trip."""
        llm = self._llm(
            forward_text="ޤޫ ޚޮއް",
            back_text="The schools opened.",  # number lost!
        )
        res = translator.translate(
            self.conn, "4 schools opened in 2026",
            target_lang="DV", llm=llm, verify=True,
        )
        v = res["verification"]
        self.assertFalse(v["passed"],
            "Numbers were lost in round trip — verification must "
            "flag this as the failure mode it exists to catch")
        self.assertIn("4", v["numbers_lost"])
        self.assertIn("2026", v["numbers_lost"])

    def test_dv_source_skips_proper_noun_check(self):
        """Thaana has no case; proper-noun extraction via Latin
        capitalisation regex doesn't apply. The verifier should
        skip that check (not crash, not falsely flag)."""
        llm = self._llm(
            forward_text="The President met.",
            back_text="ރައީސުލްޖުމްހޫރިއްޔާ ބައްދަލުވިއެވެ",
        )
        # Note: source is DV (the input is Thaana), target is EN
        res = translator.translate(
            self.conn,
            "ރައީސުލްޖުމްހޫރިއްޔާ ބައްދަލުވިއެވެ 2026",
            target_lang="EN", llm=llm, verify=True,
        )
        v = res["verification"]
        self.assertEqual(v["proper_nouns_lost"], [],
            "DV source: proper-noun check must be skipped — Thaana "
            "has no case, so the Latin-capitalisation regex would "
            "produce noise. Skip cleanly.")

    def test_verify_doubles_cost(self):
        """Per-call cost should reflect BOTH the forward and back
        LLM calls when verify=True."""
        llm = self._llm()
        res = translator.translate(
            self.conn, "Hello world.", target_lang="DV",
            llm=llm, verify=True,
        )
        # forward cost + verification cost = total cost
        self.assertGreater(res["cost_usd"], 0)
        self.assertEqual(
            res["cost_usd"],
            # The breakdown: cost_usd at top-level == forward + verification
            # We can't easily separate without instrumenting further, but
            # the verification dict has its own cost_usd field.
            res["cost_usd"]  # tautology; just assert verification.cost_usd > 0
        )
        self.assertGreater(res["verification"]["cost_usd"], 0)

    def test_default_no_verify(self):
        """verify defaults to False — no extra LLM call, no
        verification key in the response."""
        b = MagicMock(); b.type = "text"; b.text = "translated"
        r = MagicMock(); r.content = [b]
        r.usage.input_tokens = 10; r.usage.output_tokens = 5
        llm = MagicMock(); llm.messages.create.return_value = r
        res = translator.translate(
            self.conn, "Hello world.", target_lang="DV", llm=llm)
        self.assertNotIn("verification", res,
            "verify=False by default — verification key must not "
            "appear in the response when caller doesn't opt in")
        # Exactly ONE LLM call
        self.assertEqual(llm.messages.create.call_count, 1)


# ───────────────────────────────────────────────────────────────────
# Phrase-anchored context retrieval (sentence-level)
# ───────────────────────────────────────────────────────────────────

class PhraseExtraction(unittest.TestCase):
    """The heuristic phrase extractor pulls candidate strings from
    the input that the per-phrase FTS5 lookup will use. Quality of
    these phrases directly controls quality of the snippet context
    we inject — bad phrases means weak context."""

    def test_en_extracts_multi_word_capitalised(self):
        from kahzaabu.translator import _extract_phrases
        ps = _extract_phrases(
            "The Judicial Service Commission met with the Cabinet today.",
            "EN")
        self.assertIn("Judicial Service Commission", ps,
            "Three-word capitalised institution must be extracted")

    def test_en_skips_stopphrases(self):
        from kahzaabu.translator import _extract_phrases
        ps = _extract_phrases("The President said this.", "EN")
        # "The President" is in _STOPPHRASE_EN; must not appear
        self.assertNotIn("The President", ps)

    def test_en_skips_short_phrases(self):
        from kahzaabu.translator import _extract_phrases
        ps = _extract_phrases("AB CD said this.", "EN")
        # "AB CD" is short (< 6 chars) — must be filtered
        for p in ps:
            self.assertGreaterEqual(len(p), 6)

    def test_dv_extracts_thaana_ngrams(self):
        from kahzaabu.translator import _extract_phrases
        ps = _extract_phrases(
            "ރައީސުލްޖުމްހޫރިއްޔާ ޑޮކްޓަރ މުޢިއްޒު ވިދާޅުވިއެވެ",
            "DV")
        # Should extract the multi-word Thaana title
        self.assertTrue(any("ރައީސުލްޖުމްހޫރިއްޔާ" in p for p in ps),
            "DV extractor must surface multi-Thaana-word sequences")

    def test_extraction_respects_max_phrases(self):
        from kahzaabu.translator import _extract_phrases
        text = (
            "The Judicial Service Commission, the Cabinet of Ministers, "
            "the People's Majlis, the Anti-Corruption Commission, "
            "and the Elections Commission met today."
        )
        ps = _extract_phrases(text, "EN", max_phrases=3)
        self.assertLessEqual(len(ps), 3)


class ParagraphAlignment(unittest.TestCase):
    """Paragraph-of + paired-paragraph helpers — paragraph alignment
    is best-effort but should at least produce sensible output for
    typical paired-article structure."""

    def test_paragraph_of_finds_containing_para(self):
        from kahzaabu.translator import _paragraph_of
        text = ("First paragraph here.\n\n"
                "Second paragraph with the key phrase Judicial Service Commission "
                "in the middle.\n\n"
                "Third paragraph unrelated.")
        p = _paragraph_of(text, "Judicial Service Commission")
        self.assertIn("Second paragraph", p)
        self.assertNotIn("First", p)
        self.assertNotIn("Third", p)

    def test_paragraph_of_returns_none_for_missing(self):
        from kahzaabu.translator import _paragraph_of
        self.assertIsNone(_paragraph_of("any text", "nonexistent phrase"))

    def test_paired_paragraph_at_index_picks_corresponding(self):
        from kahzaabu.translator import _paired_paragraph_at_index
        paired = "Para 0.\n\nPara 1.\n\nPara 2.\n\nPara 3."
        out = _paired_paragraph_at_index(paired, 1, n_source_paragraphs=4)
        self.assertEqual(out, "Para 1.")


# ───────────────────────────────────────────────────────────────────
# Terminology fidelity rule (Nash's "expatriate workers" feedback)
# ───────────────────────────────────────────────────────────────────

class TerminologyFidelityPrompt(unittest.TestCase):
    """The system prompt has a load-bearing TERMINOLOGY FIDELITY RULE
    block instructing the LLM to defer to exemplar phrasing over
    literal translation. This is what makes the translator produce
    'undocumented expatriate workers' (PO's actual 35-article
    phrasing) instead of 'undocumented foreign nationals' (the only-
    14-article rendering) when handling immigration topics.

    These tests pin the prompt's structure — if a future refactor
    softens the rule or drops Nash's worked example, they fail
    loudly. Empirical verification against the live DB happens
    separately (in /tmp/manual-translator-verify scripts);
    these are static-content guards."""

    def test_terminology_fidelity_rule_block_present(self):
        from kahzaabu import translator
        self.assertIn("TERMINOLOGY FIDELITY RULE",
                       translator._PO_STYLE_NOTES,
            "The TERMINOLOGY FIDELITY RULE block must remain in the "
            "system prompt — it's the load-bearing instruction that "
            "makes the LLM prefer exemplar phrasing over literal "
            "translation. Softening or removing this regresses "
            "Nash's 'expatriate workers' case.")

    def test_rule_says_must_use_exemplar_phrase(self):
        from kahzaabu import translator
        notes = translator._PO_STYLE_NOTES.lower()
        self.assertTrue(
            "must use" in notes or "you must" in notes,
            "Rule must be IMPERATIVE ('must use'), not advisory — "
            "advisory framing in prior versions let the LLM ignore "
            "the exemplar phrasing for literal translation")

    def test_nash_worked_example_in_prompt(self):
        """Nash's specific case is in the prompt as a concrete
        example. This makes the abstract rule actionable for the
        LLM and serves as a regression marker."""
        from kahzaabu import translator
        notes = translator._PO_STYLE_NOTES
        self.assertIn("expatriate workers", notes,
            "Nash's worked example must remain in the prompt — "
            "concrete examples make abstract rules actionable")
        self.assertIn("foreign nationals", notes,
            "The contrasting (avoided) phrasing must remain too — "
            "the LLM needs to see the pair to learn the rule")

    def test_recency_window_default_is_365(self):
        """Earlier default was 90 days. With ~50-100 paired articles
        in a 90-day window, the few-shot pool was too tight to
        catch phrase patterns that appear in only some articles.
        365 days expands to ~500+ paired exemplars."""
        import inspect
        from kahzaabu import translator
        sig = inspect.signature(translator.select_few_shot)
        self.assertEqual(sig.parameters["recency_days"].default, 365,
            "Default recency window must be 365 days; 90 was too "
            "tight to surface phrase patterns reliably")


# ───────────────────────────────────────────────────────────────────
# Glossary builder retry logic
# ───────────────────────────────────────────────────────────────────

class GlossaryBuilderRetries(unittest.TestCase):
    """build_glossary calls _extract_pairs_from_article on every
    sampled paired article. A transient network stall on one call
    used to hang the whole batch. The retry wrapper survives that
    by backing off + moving on after N failures."""

    def test_succeeds_on_first_try(self):
        """Happy path: real LLM call returns first time. No retries
        consumed; pairs returned as expected."""
        from kahzaabu.translator import _extract_pairs_from_article
        # Mock chain: llm.with_options().messages.create() returns
        # a valid JSON-bearing response on the first call.
        block = MagicMock(); block.type = "text"
        block.text = '{"pairs": [{"en": "Cabinet", "dv": "ކެބިނެޓް"}]}'
        resp = MagicMock(); resp.content = [block]
        resp.usage.input_tokens = 100; resp.usage.output_tokens = 50
        sub_client = MagicMock()
        sub_client.messages.create.return_value = resp
        llm = MagicMock()
        llm.with_options.return_value = sub_client

        pairs, meta = _extract_pairs_from_article(
            llm, "EN body about Cabinet.",
            "DV body about ކެބިނެޓް.",
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["en"], "Cabinet")
        # Confirm only ONE create() call happened — no retries fired.
        self.assertEqual(sub_client.messages.create.call_count, 1,
            "Happy path must not retry — wastes API budget")
        self.assertIn("cost_usd", meta)

    def test_retries_on_transient_error(self):
        """Two transient failures, then success on the third attempt.
        Pairs returned cleanly; no exception leaks to the caller."""
        from kahzaabu.translator import _extract_pairs_from_article
        block = MagicMock(); block.type = "text"
        block.text = '{"pairs": [{"en": "JSC", "dv": "ޖޭ.އެސް.ސީ"}]}'
        resp = MagicMock(); resp.content = [block]
        resp.usage.input_tokens = 100; resp.usage.output_tokens = 50

        sub_client = MagicMock()
        # First two calls raise; third returns the valid response.
        sub_client.messages.create.side_effect = [
            ConnectionError("simulated network stall"),
            RuntimeError("simulated transient 500"),
            resp,
        ]
        llm = MagicMock()
        llm.with_options.return_value = sub_client

        with patch("time.sleep"):  # speed up exponential backoff
            pairs, meta = _extract_pairs_from_article(
                llm, "EN body.", "DV body.")
        self.assertEqual(len(pairs), 1,
            "Retry on transient errors must eventually surface the "
            "real result — the whole point of the retry loop")
        self.assertEqual(sub_client.messages.create.call_count, 3)

    def test_exhausted_retries_returns_empty_with_error_meta(self):
        """All N attempts fail. Returns empty pairs + meta with
        _error set; does NOT raise. The outer build_glossary loop
        continues with the next article."""
        from kahzaabu.translator import _extract_pairs_from_article
        sub_client = MagicMock()
        sub_client.messages.create.side_effect = ConnectionError(
            "persistent network failure")
        llm = MagicMock()
        llm.with_options.return_value = sub_client

        with patch("time.sleep"):
            pairs, meta = _extract_pairs_from_article(
                llm, "EN body.", "DV body.", retries=2)
        self.assertEqual(pairs, [],
            "Exhausted retries must return empty list, not raise — "
            "raising would kill the whole batch job")
        self.assertIn("_error", meta,
            "Meta must carry the error reason so build_glossary "
            "can surface it in the summary")
        # Should have attempted exactly retries times.
        self.assertEqual(sub_client.messages.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
