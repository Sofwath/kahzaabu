"""Unit tests for V2 Slice 5 — truth_score derivation (ADR 0005).

Pins the deterministic mapping function. Pure unit tests, no DB.

Run:
    .venv/bin/python -m unittest tests.test_truth_score
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import truth_score
from kahzaabu.truth_score import (
    category_to_verdict_label, derive_truth_score, derive_all,
)


class CategoryToVerdictLabelTests(unittest.TestCase):
    def test_lie_refuted(self):
        self.assertEqual(category_to_verdict_label("LIE"), "REFUTED")

    def test_contradiction_refuted(self):
        self.assertEqual(category_to_verdict_label("CONTRADICTION"), "REFUTED")

    def test_broken_deadline_refuted(self):
        self.assertEqual(category_to_verdict_label("BROKEN DEADLINE"), "REFUTED")

    def test_credit_theft_refuted(self):
        self.assertEqual(category_to_verdict_label("CREDIT THEFT"), "REFUTED")

    def test_misleading_conflicting(self):
        self.assertEqual(category_to_verdict_label("MISLEADING"), "CONFLICTING_EVIDENCE")

    def test_shifting_numbers_conflicting(self):
        self.assertEqual(category_to_verdict_label("SHIFTING NUMBERS"), "CONFLICTING_EVIDENCE")

    def test_compound_lie_misleading_resolves_to_lie(self):
        self.assertEqual(category_to_verdict_label("LIE / MISLEADING"), "REFUTED")

    def test_unknown_returns_not_enough(self):
        self.assertEqual(category_to_verdict_label("ZOMBIE_CLAIM"),
                          "NOT_ENOUGH_EVIDENCE")

    def test_null_returns_not_enough(self):
        self.assertEqual(category_to_verdict_label(None),
                          "NOT_ENOUGH_EVIDENCE")
        self.assertEqual(category_to_verdict_label(""),
                          "NOT_ENOUGH_EVIDENCE")

    def test_case_insensitive(self):
        self.assertEqual(category_to_verdict_label("lie"), "REFUTED")
        self.assertEqual(category_to_verdict_label(" Broken Deadline "), "REFUTED")


class DeriveTruthScoreTests(unittest.TestCase):
    def test_supported_high_confidence_is_TRUE(self):
        s, l = derive_truth_score("SUPPORTED", 0.95)
        self.assertEqual((s, l), (6, "TRUE"))

    def test_supported_medium_confidence_is_MOSTLY_TRUE(self):
        s, l = derive_truth_score("SUPPORTED", 0.70)
        self.assertEqual((s, l), (5, "MOSTLY_TRUE"))

    def test_conflicting_evidence_is_HALF_TRUE(self):
        s, l = derive_truth_score("CONFLICTING_EVIDENCE", 0.5)
        self.assertEqual((s, l), (4, "HALF_TRUE"))

    def test_refuted_low_confidence_is_MOSTLY_FALSE(self):
        s, l = derive_truth_score("REFUTED", 0.5)
        self.assertEqual((s, l), (3, "MOSTLY_FALSE"))

    def test_refuted_medium_confidence_is_FALSE(self):
        s, l = derive_truth_score("REFUTED", 0.80)
        self.assertEqual((s, l), (2, "FALSE"))

    def test_refuted_high_confidence_lie_is_PANTS_ON_FIRE(self):
        s, l = derive_truth_score("REFUTED", 0.98, category="LIE")
        self.assertEqual((s, l), (1, "PANTS_ON_FIRE"))

    def test_refuted_high_confidence_NON_lie_is_FALSE(self):
        # PANTS_ON_FIRE reserved for LIE category only
        s, l = derive_truth_score("REFUTED", 0.98, category="BROKEN DEADLINE")
        self.assertEqual((s, l), (2, "FALSE"))

    def test_not_enough_evidence_is_HALF_TRUE(self):
        s, l = derive_truth_score("NOT_ENOUGH_EVIDENCE", 0.5)
        self.assertEqual((s, l), (4, "HALF_TRUE"))

    def test_null_inputs_default_to_HALF_TRUE(self):
        s, l = derive_truth_score(None, None)
        self.assertEqual((s, l), (4, "HALF_TRUE"))

    def test_string_confidence_reviewed(self):
        # 'reviewed' = 0.90 → SUPPORTED branch reaches 6=TRUE
        s, l = derive_truth_score("SUPPORTED", "reviewed")
        self.assertEqual((s, l), (6, "TRUE"))

    def test_string_confidence_auto(self):
        # 'auto' = 0.65 → SUPPORTED branch reaches 5=MOSTLY_TRUE
        s, l = derive_truth_score("SUPPORTED", "auto")
        self.assertEqual((s, l), (5, "MOSTLY_TRUE"))

    def test_string_confidence_rejected(self):
        s, l = derive_truth_score("REFUTED", "rejected")
        self.assertEqual((s, l), (3, "MOSTLY_FALSE"))

    def test_compound_lie_pants_on_fire(self):
        # 'LIE / MISLEADING' resolves to 'LIE' for the PANTS_ON_FIRE gate
        s, l = derive_truth_score("REFUTED", 0.97, category="LIE / MISLEADING")
        self.assertEqual((s, l), (1, "PANTS_ON_FIRE"))


class DeriveAllTests(unittest.TestCase):
    def test_returns_full_triplet(self):
        d = derive_all("LIE", 0.97)
        self.assertEqual(d["verdict_label"], "REFUTED")
        self.assertEqual(d["truth_score"], 1)
        self.assertEqual(d["truth_score_label"], "PANTS_ON_FIRE")

    def test_returns_full_triplet_misleading(self):
        d = derive_all("MISLEADING", "auto")
        self.assertEqual(d["verdict_label"], "CONFLICTING_EVIDENCE")
        self.assertEqual(d["truth_score"], 4)
        self.assertEqual(d["truth_score_label"], "HALF_TRUE")

    def test_handles_full_corpus_categories(self):
        # Every category that exists in the live corpus must produce a
        # non-NULL triplet — proves the function won't NULL anything out.
        live_categories = [
            "BROKEN DEADLINE", "CREDIT THEFT", "SHIFTING NUMBERS",
            "CONTRADICTION", "MISLEADING", "LIE",
            "LIE / MISLEADING", "MISLEADING / CREDIT THEFT",
            "LIE / CONTRADICTION", "LIE / SHIFTING NUMBERS",
        ]
        for c in live_categories:
            with self.subTest(category=c):
                d = derive_all(c, "auto")
                self.assertIn(d["verdict_label"],
                               {"SUPPORTED", "REFUTED",
                                "NOT_ENOUGH_EVIDENCE", "CONFLICTING_EVIDENCE"})
                self.assertIn(d["truth_score"], range(1, 7))
                self.assertIn(d["truth_score_label"],
                               {"TRUE", "MOSTLY_TRUE", "HALF_TRUE",
                                "MOSTLY_FALSE", "FALSE", "PANTS_ON_FIRE"})


class IdempotencyTests(unittest.TestCase):
    """Same input → same output, always. Pin against accidental
    nondeterminism (e.g. dict ordering surprises)."""

    def test_repeated_calls_identical(self):
        for inp in [("LIE", 0.95), ("MISLEADING", "auto"),
                     ("BROKEN DEADLINE", "reviewed"), (None, None)]:
            with self.subTest(input=inp):
                a = derive_all(*inp)
                b = derive_all(*inp)
                self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
