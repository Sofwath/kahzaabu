"""Unit tests for V2 Slice 10 — quality evaluation framework (ADR 0008).

Pins:
- jaccard_f1 over set inputs
- classification_metrics over (predicted, expected) pair lists
- per-stage runner returns expected shape
- load_fixtures picks up JSON files under tests/golden/<stage>/
- render_markdown_report produces valid markdown structure

All tests offline, no LLM, no live DB.

Run:
    .venv/bin/python -m unittest tests.test_eval
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import eval as ev


class JaccardF1Tests(unittest.TestCase):
    def test_identical_sets(self):
        p, r, f = ev.jaccard_f1({1, 2, 3}, {1, 2, 3})
        self.assertEqual((p, r, f), (1.0, 1.0, 1.0))

    def test_disjoint(self):
        p, r, f = ev.jaccard_f1({1, 2}, {3, 4})
        self.assertEqual((p, r, f), (0.0, 0.0, 0.0))

    def test_partial_overlap(self):
        # predicted={1,2}, expected={2,3}
        # precision = 1/2 = 0.5, recall = 1/2 = 0.5, f1 = 0.5
        p, r, f = ev.jaccard_f1({1, 2}, {2, 3})
        self.assertAlmostEqual(p, 0.5)
        self.assertAlmostEqual(r, 0.5)
        self.assertAlmostEqual(f, 0.5)

    def test_both_empty(self):
        # vacuous truth: nothing predicted, nothing expected → perfect
        self.assertEqual(ev.jaccard_f1(set(), set()), (1.0, 1.0, 1.0))

    def test_predicted_empty_expected_nonempty(self):
        self.assertEqual(ev.jaccard_f1(set(), {1, 2}), (0.0, 0.0, 0.0))


class ClassificationMetricsTests(unittest.TestCase):
    def test_all_correct(self):
        pairs = [("A", "A"), ("B", "B"), ("A", "A")]
        m = ev.classification_metrics(pairs)
        self.assertEqual(m["accuracy"], 1.0)
        self.assertEqual(m["macro_f1"], 1.0)

    def test_binary_mixed(self):
        # 2 correct, 1 wrong
        pairs = [("SAME", "SAME"), ("DIFF", "DIFF"), ("SAME", "DIFF")]
        m = ev.classification_metrics(pairs)
        self.assertAlmostEqual(m["accuracy"], 2/3)
        self.assertIn("SAME", m["per_class"])
        self.assertIn("DIFF", m["per_class"])

    def test_multiclass_per_class_f1(self):
        # 4-way verdict mock
        pairs = [
            ("CONTRADICTION", "CONTRADICTION"),
            ("EVOLVING_POSITION", "EVOLVING_POSITION"),
            ("NOT_CONTRADICTORY", "CONTEXT_CHANGED"),  # miss
        ]
        m = ev.classification_metrics(pairs)
        self.assertAlmostEqual(m["accuracy"], 2/3)
        self.assertEqual(m["per_class"]["CONTRADICTION"]["f1"], 1.0)
        # CONTEXT_CHANGED has 0 predicted, 1 expected → recall=0
        self.assertEqual(m["per_class"]["CONTEXT_CHANGED"]["recall"], 0.0)

    def test_empty_returns_zero(self):
        m = ev.classification_metrics([])
        self.assertEqual(m["accuracy"], 0.0)
        self.assertEqual(m["macro_f1"], 0.0)


class FixtureLoaderTests(unittest.TestCase):
    def test_load_existing_truth_score_fixtures(self):
        fixtures = ev.load_fixtures("truth_score")
        # We authored 6 fixtures in Slice 10
        self.assertGreaterEqual(len(fixtures), 1)
        for fx in fixtures:
            self.assertIn("id", fx)
            self.assertIn("input", fx)
            self.assertIn("expected", fx)
            self.assertIn("verified", fx)  # default-applied if missing

    def test_load_unknown_stage_returns_empty(self):
        self.assertEqual(ev.load_fixtures("bogus_stage_does_not_exist"), [])

    def test_limit_respected(self):
        fixtures = ev.load_fixtures("truth_score", limit=2)
        self.assertLessEqual(len(fixtures), 2)

    def test_truth_score_all_verified(self):
        """truth_score is deterministic per ADR 0005 — every fixture
        must be marked verified (ground truth)."""
        fixtures = ev.load_fixtures("truth_score")
        self.assertTrue(all(fx.get("verified") for fx in fixtures))

    def test_matcher_all_verified(self):
        """Matcher fixtures are structural ground truth."""
        fixtures = ev.load_fixtures("matcher")
        self.assertTrue(all(fx.get("verified") for fx in fixtures))

    def test_llm_stages_have_promoted_fixtures(self):
        """Extractor/decomposer/contradictions ship with at least
        some hand-verified fixtures (promoted in the verification
        pass that followed Slice 10). One extractor fixture is
        deliberately left unverified pending taxonomy clarification."""
        for stage in ("decomposer", "contradictions"):
            fixtures = ev.load_fixtures(stage)
            self.assertTrue(
                any(fx.get("verified") for fx in fixtures),
                f"{stage} should have at least one verified fixture")
        # Extractor has 3/4 verified + 1 deliberately unverified
        ex = ev.load_fixtures("extractor")
        n_verified = sum(1 for fx in ex if fx.get("verified"))
        self.assertGreaterEqual(n_verified, 1)
        self.assertLess(n_verified, len(ex),
                         "expect at least one extractor fixture left "
                         "unverified to demonstrate honest gating")

    def test_verified_defaults_to_false_when_missing(self):
        """A fixture file without an explicit `verified` field loads as
        unverified — honest default."""
        with tempfile.TemporaryDirectory() as tmp:
            stage_dir = Path(tmp) / "golden" / "bogus"
            stage_dir.mkdir(parents=True)
            (stage_dir / "01.json").write_text(json.dumps(
                {"id": "x", "input": {}, "expected": {}}))
            with patch.object(ev, "GOLDEN_DIR", Path(tmp) / "golden"):
                fixtures = ev.load_fixtures("bogus")
            self.assertEqual(len(fixtures), 1)
            self.assertFalse(fixtures[0]["verified"])


class TruthScoreRunnerTests(unittest.TestCase):
    """Truth_score is deterministic — eval over the live fixtures must
    pass with score_accuracy == 1.0 unless ADR 0005's mapping changes."""

    def test_runs_against_real_fixtures(self):
        fixtures = ev.load_fixtures("truth_score")
        result = ev._run_truth_score(fixtures)
        self.assertEqual(result["score_accuracy"], 1.0)
        self.assertEqual(result["verdict_metrics"]["accuracy"], 1.0)
        self.assertEqual(result["misses"], [])
        # All 6 truth_score fixtures are verified → verified subset
        # metrics should also be 1.0
        self.assertEqual(result["n_verified"], 6)
        self.assertEqual(result["verdict_metrics_verified"]["accuracy"], 1.0)
        self.assertEqual(result["score_accuracy_verified"], 1.0)

    def test_detects_a_planted_miss(self):
        # Inject a fixture with a wrong expectation
        bad = [{
            "id": "planted-miss",
            "input": {"category": "LIE", "confidence": 0.97},
            "expected": {"verdict_label": "SUPPORTED",  # wrong on purpose
                          "truth_score": 6, "truth_score_label": "TRUE"},
        }]
        result = ev._run_truth_score(bad)
        self.assertLess(result["score_accuracy"], 1.0)
        self.assertGreaterEqual(len(result["misses"]), 1)


class RenderMarkdownReportTests(unittest.TestCase):
    def test_renders_markdown_skeleton(self):
        results = {
            "_meta": {"timestamp": "2026-05-21T00:00:00Z", "small": False,
                       "stages_run": ["truth_score"]},
            "truth_score": {
                "n": 6,
                "n_verified": 6,
                "verdict_metrics": {"accuracy": 1.0, "macro_f1": 1.0,
                                     "per_class": {
                                         "REFUTED": {"precision": 1.0, "recall": 1.0,
                                                      "f1": 1.0, "support": 4},
                                     }, "n": 6},
                "verdict_metrics_verified": {"accuracy": 1.0, "macro_f1": 1.0,
                                              "per_class": {
                                                  "REFUTED": {"precision": 1.0, "recall": 1.0,
                                                               "f1": 1.0, "support": 4},
                                              }, "n": 6},
                "score_accuracy": 1.0,
                "score_accuracy_verified": 1.0,
                "misses": [],
            }
        }
        md = ev.render_markdown_report(results)
        self.assertIn("# Kahzaabu — quality evaluation results", md)
        self.assertIn("## truth_score", md)
        # Honesty preamble must be present
        self.assertIn("Reading the numbers", md)
        self.assertIn("Verified-subset", md)
        self.assertIn("All fixtures (drift detector)", md)
        # truth_score renders per-class table in verified subset
        self.assertIn("REFUTED", md)

    def test_pinned_only_stage_omits_verified_block(self):
        """When a stage has zero verified fixtures, the report should
        NOT render a 'Verified-subset' block (we don't want to imply
        ground truth that isn't there)."""
        results = {
            "_meta": {"timestamp": "2026-05-21T00:00:00Z", "small": False,
                       "stages_run": ["extractor"]},
            "extractor": {
                "n": 4,
                "n_verified": 0,
                "precision": 1.0, "recall": 1.0, "f1": 1.0,
                "misses": [],
            }
        }
        md = ev.render_markdown_report(results)
        self.assertIn("## extractor", md)
        self.assertIn("(verified: **0**, pinned: **4**)", md)
        self.assertNotIn("### Verified-subset", md)
        self.assertIn("### All fixtures (drift detector)", md)


class RunEvalTests(unittest.TestCase):
    def test_runs_all_stages(self):
        results = ev.run_eval()
        self.assertIn("_meta", results)
        for stage in ev.STAGE_RUNNERS:
            self.assertIn(stage, results)

    def test_small_mode_truncates_fixtures(self):
        full = ev.run_eval(stages=["truth_score"], small=False)
        small = ev.run_eval(stages=["truth_score"], small=True)
        self.assertLessEqual(small["truth_score"]["n"],
                              full["truth_score"]["n"])

    def test_unknown_stage_skipped_gracefully(self):
        results = ev.run_eval(stages=["bogus_stage"])
        # No crash; just no entry for that stage
        self.assertNotIn("bogus_stage", results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
