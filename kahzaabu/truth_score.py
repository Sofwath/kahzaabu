# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 5 — deterministic derivation of AVeriTeC verdict_label +
PolitiFact-style truth_score from kahzaabu's analytical category and
the confidence column already on fact_checks (ADR 0005).

Two derivation functions, both pure (no I/O, no LLM, no DB). Tests in
tests/test_truth_score.py verify every edge case.

Layer 1: category + confidence  →  verdict_label (AVeriTeC enum)
Layer 2: verdict_label + confidence + category  →  truth_score (1-6) + label

Both functions tolerate NULL inputs gracefully — returns
NOT_ENOUGH_EVIDENCE for layer 1, HALF_TRUE / score=4 for layer 2.
The contract is: any combination of inputs produces a valid output,
NEVER raises.
"""
from __future__ import annotations

from typing import Optional


# ─────────────────────────────────────────────────────────────────
# Layer 1 — kahzaabu category → AVeriTeC verdict_label
# ─────────────────────────────────────────────────────────────────

# Default mapping per ADR 0005. Compound categories ("LIE / MISLEADING")
# resolve to the stronger half — present in the live corpus from
# Slice 0 baseline. The string match is case-insensitive on the first
# whitespace-delimited token to keep this robust.
_CATEGORY_TO_VERDICT = {
    "LIE":                          "REFUTED",
    "CONTRADICTION":                "REFUTED",
    "BROKEN DEADLINE":              "REFUTED",
    "CREDIT THEFT":                 "REFUTED",
    "MISLEADING":                   "CONFLICTING_EVIDENCE",
    "SHIFTING NUMBERS":             "CONFLICTING_EVIDENCE",
}

# Compound categories observed in the corpus map to the stronger half.
_COMPOUND_REWRITES = {
    "LIE / MISLEADING":             "LIE",
    "LIE / CONTRADICTION":          "LIE",
    "LIE / SHIFTING NUMBERS":       "LIE",
    "MISLEADING / CREDIT THEFT":    "CREDIT THEFT",
}


def category_to_verdict_label(category: Optional[str]) -> str:
    """Map a kahzaabu category to an AVeriTeC verdict label.

    Returns one of: SUPPORTED / REFUTED / NOT_ENOUGH_EVIDENCE /
    CONFLICTING_EVIDENCE. Never raises.
    """
    if not category:
        return "NOT_ENOUGH_EVIDENCE"
    c = category.strip().upper()
    c = _COMPOUND_REWRITES.get(c, c)
    return _CATEGORY_TO_VERDICT.get(c, "NOT_ENOUGH_EVIDENCE")


# ─────────────────────────────────────────────────────────────────
# Layer 2 — verdict + confidence + category → truth_score (1-6) + label
# ─────────────────────────────────────────────────────────────────

# PolitiFact 6-rung gradient — the public-facing label per ADR 0005.
# Pure function; no DB, no I/O, no LLM. Same input → same output.
TRUTH_LABELS = {
    6: "TRUE",
    5: "MOSTLY_TRUE",
    4: "HALF_TRUE",
    3: "MOSTLY_FALSE",
    2: "FALSE",
    1: "PANTS_ON_FIRE",
}

# Confidence-string to float mapping for fact_checks.confidence
# (which uses 'auto' | 'reviewed' | 'rejected' enums, not a numeric).
# 'reviewed' = human reviewer agrees → high confidence.
# 'auto' = LLM-curated, no human review → medium confidence.
# 'rejected' = human reviewer disagrees → low confidence; never publishes.
_CONFIDENCE_STRINGS = {
    "reviewed": 0.90,
    "auto":     0.65,
    "rejected": 0.30,
}


def _confidence_as_float(confidence) -> float:
    """Accept either a string (the kahzaabu enum) or a float. Returns
    [0, 1]. NULL / unknown → 0.5."""
    if confidence is None:
        return 0.5
    if isinstance(confidence, (int, float)):
        return max(0.0, min(1.0, float(confidence)))
    if isinstance(confidence, str):
        return _CONFIDENCE_STRINGS.get(confidence.lower(), 0.5)
    return 0.5


def derive_truth_score(verdict_label: Optional[str],
                        confidence,
                        category: Optional[str] = None) -> tuple[int, str]:
    """Per ADR 0005 §3, derive PolitiFact-style truth_score from the
    AVeriTeC verdict + confidence + category. Returns (score 1-6, label).

    Verdict ladder (ADR 0005):
      SUPPORTED + conf ≥ 0.85   → 6 TRUE
      SUPPORTED + conf ≥ 0.60   → 5 MOSTLY_TRUE
      CONFLICTING_EVIDENCE      → 4 HALF_TRUE
      REFUTED + conf < 0.70     → 3 MOSTLY_FALSE
      REFUTED + 0.70 ≤ conf < 0.95   → 2 FALSE
      REFUTED + conf ≥ 0.95 + category LIE → 1 PANTS_ON_FIRE

    NOT_ENOUGH_EVIDENCE → HALF_TRUE/4 (epistemically honest middle).
    """
    conf = _confidence_as_float(confidence)
    v = (verdict_label or "").strip().upper()
    cat = (category or "").strip().upper()
    cat = _COMPOUND_REWRITES.get(cat, cat)

    if v == "SUPPORTED":
        if conf >= 0.85:
            return 6, TRUTH_LABELS[6]
        if conf >= 0.60:
            return 5, TRUTH_LABELS[5]
        return 4, TRUTH_LABELS[4]      # supported but low confidence

    if v == "CONFLICTING_EVIDENCE":
        return 4, TRUTH_LABELS[4]

    if v == "REFUTED":
        if conf >= 0.95 and cat == "LIE":
            return 1, TRUTH_LABELS[1]
        if conf >= 0.70:
            return 2, TRUTH_LABELS[2]
        return 3, TRUTH_LABELS[3]

    # NOT_ENOUGH_EVIDENCE or unknown verdict → epistemic middle
    return 4, TRUTH_LABELS[4]


def derive_all(category: Optional[str], confidence) -> dict:
    """Single-call convenience: category + confidence → full triplet
    {verdict_label, truth_score, truth_score_label}. The shape stored
    on fact_checks."""
    vl = category_to_verdict_label(category)
    ts, tsl = derive_truth_score(vl, confidence, category)
    return {
        "verdict_label": vl,
        "truth_score": ts,
        "truth_score_label": tsl,
    }
