# SPDX-License-Identifier: Apache-2.0
"""Tests for the centralised pricing + model-id registry.

Why a dedicated test file: this module is the single point of change
for LLM pricing across the entire pipeline. A broken `cost()` quietly
miscalculates the daily-spend cap and lets the pipeline overspend
its budget. A renamed alias breaks every stage's @tracked_stage
metric label.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import pricing


class ModelShapeTests(unittest.TestCase):
    def test_model_is_frozen(self):
        """The Model dataclass MUST be frozen — accidental mutation
        is the kind of bug that silently breaks budget tracking."""
        m = pricing.Model(id="x", in_per_m=1.0, out_per_m=2.0)
        with self.assertRaises(FrozenInstanceError):
            m.id = "tampered"

    def test_model_has_default_websearch_cost(self):
        """`web_search_per_call` defaults to 0.0 so callers don't
        need to know about it for non-web stages."""
        m = pricing.Model(id="x", in_per_m=1.0, out_per_m=2.0)
        self.assertEqual(m.web_search_per_call, 0.0)


class RegistryShapeTests(unittest.TestCase):
    def test_registry_keys_match_documented_aliases(self):
        """The aliases used across the codebase. If you rename one,
        every @tracked_stage(model=...) call breaks — this test
        forces a conscious update."""
        self.assertEqual(
            set(pricing.MODELS),
            {"sonnet", "haiku", "haiku-ws"},
        )

    def test_sonnet_is_3_15(self):
        m = pricing.MODELS["sonnet"]
        self.assertEqual(m.in_per_m, 3.0)
        self.assertEqual(m.out_per_m, 15.0)
        self.assertEqual(m.web_search_per_call, 0.0)
        self.assertEqual(m.id, "claude-sonnet-4-6")

    def test_haiku_is_1_5(self):
        m = pricing.MODELS["haiku"]
        self.assertEqual(m.in_per_m, 1.0)
        self.assertEqual(m.out_per_m, 5.0)
        self.assertEqual(m.web_search_per_call, 0.0)
        self.assertEqual(m.id, "claude-haiku-4-5")

    def test_haiku_ws_carries_websearch_surcharge(self):
        m = pricing.MODELS["haiku-ws"]
        self.assertEqual(m.in_per_m, 1.0)
        self.assertEqual(m.out_per_m, 5.0)
        # $0.01 per web_search call matches Anthropic's $10/1000 pricing.
        self.assertEqual(m.web_search_per_call, 0.01)


class CostHelperTests(unittest.TestCase):
    def test_zero_tokens_zero_cost(self):
        self.assertEqual(pricing.cost("sonnet"), 0.0)

    def test_sonnet_million_tokens_each_way(self):
        # 1M in + 1M out at Sonnet rates = 3 + 15 = 18
        self.assertAlmostEqual(
            pricing.cost("sonnet", tokens_in=1_000_000, tokens_out=1_000_000),
            18.0)

    def test_haiku_million_tokens_each_way(self):
        # 1M in + 1M out at Haiku rates = 1 + 5 = 6
        self.assertAlmostEqual(
            pricing.cost("haiku", tokens_in=1_000_000, tokens_out=1_000_000),
            6.0)

    def test_websearch_surcharge_only_on_haiku_ws(self):
        # 100k in + 100k out + 5 searches
        # haiku-ws:  0.1 + 0.5 + 5*0.01 = 0.65
        self.assertAlmostEqual(
            pricing.cost("haiku-ws", tokens_in=100_000, tokens_out=100_000,
                          web_searches=5),
            0.65)
        # plain haiku ignores web_searches (web_search_per_call=0)
        self.assertAlmostEqual(
            pricing.cost("haiku", tokens_in=100_000, tokens_out=100_000,
                          web_searches=5),
            0.6)

    def test_unknown_alias_raises_key_error(self):
        """A typo (`'gpt-4'`) shouldn't silently return 0.0; that
        would let the budget tracker drift. Fail loud."""
        with self.assertRaises(KeyError):
            pricing.cost("nonexistent-alias", tokens_in=1000)


class ModelIdHelperTests(unittest.TestCase):
    def test_resolves_alias_to_canonical_id(self):
        self.assertEqual(pricing.model_id("sonnet"), "claude-sonnet-4-6")
        self.assertEqual(pricing.model_id("haiku"),  "claude-haiku-4-5")

    def test_unknown_alias_raises(self):
        with self.assertRaises(KeyError):
            pricing.model_id("nope")


class StageConsistencyTests(unittest.TestCase):
    """Every stage module's MODEL / PRICE_* constants must derive
    from pricing.MODELS. This guards against future drift where
    someone re-introduces a hardcoded model string."""

    STAGES = ("extractor", "decomposer", "matcher", "contradictions",
               "verifier", "inspector", "dv_compare", "curator",
               "claims_enricher", "manifesto", "qna", "qna_agentic")

    def test_no_stage_hardcodes_a_claude_model_string(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "kahzaabu"
        offenders = []
        for stem in self.STAGES:
            p = root / f"{stem}.py"
            if not p.exists(): continue
            for ln in p.read_text().splitlines():
                # Inside-comment patterns ("# claude-…" prose) are fine.
                stripped = ln.lstrip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                if re.search(r'"claude-(?:sonnet|haiku|opus)-', ln):
                    offenders.append(f"{p.name}: {ln.strip()[:80]}")
        self.assertEqual(offenders, [],
            "Hardcoded Claude model strings found outside comments. "
            "All model IDs must come from kahzaabu.pricing.MODELS. "
            "Offenders:\n  " + "\n  ".join(offenders))

    def test_every_stage_imports_pricing(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "kahzaabu"
        missing = []
        for stem in self.STAGES:
            p = root / f"{stem}.py"
            if not p.exists(): continue
            text = p.read_text()
            if "from . import pricing" not in text:
                missing.append(stem)
        self.assertEqual(missing, [],
            "Stage modules must import the pricing registry:\n  "
            + "\n  ".join(missing))


if __name__ == "__main__":
    unittest.main(verbosity=2)
