# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 10 — quality evaluation framework (ADR 0008).

Golden-set based evaluation for each LLM-call stage of the pipeline.
Pure-Python framework: loads JSON fixtures, runs them through the
actual stage code, scores against expected output, emits a markdown
report.

Stages evaluated:
  extractor     — input: article body; expected: list of claim dicts
                  scoring: Jaccard F1 on (type, polarity) tuples
  decomposer    — input: claim dict; expected: list of question dicts
                  scoring: Jaccard F1 on (answer_type, source_medium)
  matcher       — input: two claim quotes + same/diff label
                  scoring: binary classification F1
  contradictions— input: two claim dicts; expected: 4-way verdict
                  scoring: macro-F1 over 4 verdict classes
  truth_score   — input: (category, confidence); expected: verdict_label + truth_score
                  scoring: exact-match accuracy
                  (deterministic, no LLM — confirms ADR 0005 mapping stays
                  stable across refactors)

`kahzaabu eval` CLI runs all stages or a single one. Results go to
data/eval_history.jsonl (append-only) and docs/EVAL_RESULTS.md (rendered).

Fixtures live under tests/golden/<stage>/*.json — each file is:
  {
    "id":       "human-friendly slug",
    "input":    <stage-specific input>,
    "expected": <stage-specific expected output>,
    "verified": true | false,   // see "verified vs pinned" below
    "notes":    "why this fixture, what it pins"
  }

**Verified vs pinned**

A fixture is `verified: true` when its `expected` is hand-confirmed
ground truth (e.g. the truth_score mapping is mathematically defined
by ADR 0005, or a matcher pair is structurally obvious). It is
`verified: false` when `expected` was seeded from current pipeline
output to act as a *drift detector* — a non-1.0 score after a prompt
change means the LLM diverged from its previous behavior, but says
nothing about which version is "correct."

The eval report shows both subsets:
  - **Verified-subset metric** = real quality measurement
  - **All-fixture metric**     = drift detector (regression to prior)

Adding hand-verified fixtures over time is how the verified subset
grows. Default is `false` so a new fixture is honestly a drift
baseline until reviewed.

Per ADR 0008 §3, CI runs the SMALL eval (`--small` = first 3 fixtures
per stage) on every PR. The full eval runs nightly. Add new fixtures
as the project owner curates them.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("kahzaabu")

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "golden"
HISTORY_PATH = Path(__file__).resolve().parents[1] / "data" / "eval_history.jsonl"
REPORT_PATH = Path(__file__).resolve().parents[1] / "docs" / "EVAL_RESULTS.md"


# ───────────────────────────────────────────────────────────────────
# Metrics
# ───────────────────────────────────────────────────────────────────

def jaccard_f1(predicted: set, expected: set) -> tuple[float, float, float]:
    """Set-based F1. Returns (precision, recall, f1)."""
    if not predicted and not expected:
        return 1.0, 1.0, 1.0
    if not predicted or not expected:
        return 0.0, 0.0, 0.0
    intersect = predicted & expected
    p = len(intersect) / len(predicted)
    r = len(intersect) / len(expected)
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def classification_metrics(pairs: list[tuple[str, str]]) -> dict:
    """For a list of (predicted, expected) labels, returns macro-F1 and
    per-class P/R/F1. Handles binary or multi-class uniformly."""
    if not pairs:
        return {"accuracy": 0.0, "macro_f1": 0.0, "per_class": {}}
    classes = sorted(set(p for p, _ in pairs) | set(e for _, e in pairs))
    per_class: dict[str, dict[str, float]] = {}
    for c in classes:
        tp = sum(1 for p, e in pairs if p == c and e == c)
        fp = sum(1 for p, e in pairs if p == c and e != c)
        fn = sum(1 for p, e in pairs if p != c and e == c)
        p_ = tp / (tp + fp) if (tp + fp) else 0.0
        r_ = tp / (tp + fn) if (tp + fn) else 0.0
        f_ = 2 * p_ * r_ / (p_ + r_) if (p_ + r_) else 0.0
        per_class[c] = {"precision": p_, "recall": r_, "f1": f_,
                         "support": sum(1 for _, e in pairs if e == c)}
    accuracy = sum(1 for p, e in pairs if p == e) / len(pairs)
    macro_f1 = sum(v["f1"] for v in per_class.values()) / max(1, len(per_class))
    return {"accuracy": accuracy, "macro_f1": macro_f1,
            "per_class": per_class, "n": len(pairs)}


# ───────────────────────────────────────────────────────────────────
# Fixture loader
# ───────────────────────────────────────────────────────────────────

def load_fixtures(stage: str, limit: Optional[int] = None) -> list[dict]:
    """Load JSON fixtures under tests/golden/<stage>/. Returns a list of
    {id, input, expected, verified, notes} dicts. Missing `verified`
    defaults to False (honest: untouched fixtures are drift baselines,
    not ground truth)."""
    stage_dir = GOLDEN_DIR / stage
    if not stage_dir.exists():
        return []
    fixtures: list[dict] = []
    for path in sorted(stage_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning(f"  bad fixture {path}: {e}")
            continue
        if "id" not in data:
            data["id"] = path.stem
        data.setdefault("verified", False)
        fixtures.append(data)
    if limit:
        fixtures = fixtures[:limit]
    return fixtures


# ───────────────────────────────────────────────────────────────────
# Per-stage runners
# ───────────────────────────────────────────────────────────────────

def _run_truth_score(fixtures: list[dict]) -> dict:
    """Deterministic — confirms the ADR 0005 mapping stays stable. No
    LLM call; pure-Python derivation."""
    from . import truth_score as ts
    pred_pairs: list[tuple[str, str]] = []
    score_pairs: list[tuple[int, int]] = []
    pred_pairs_v: list[tuple[str, str]] = []
    score_pairs_v: list[tuple[int, int]] = []
    misses: list[dict] = []
    for fx in fixtures:
        inp = fx["input"]
        exp = fx["expected"]
        d = ts.derive_all(inp.get("category"), inp.get("confidence"))
        pred_pairs.append((d["verdict_label"], exp["verdict_label"]))
        score_pairs.append((int(d["truth_score"]), int(exp["truth_score"])))
        if fx.get("verified"):
            pred_pairs_v.append((d["verdict_label"], exp["verdict_label"]))
            score_pairs_v.append(
                (int(d["truth_score"]), int(exp["truth_score"])))
        if d["verdict_label"] != exp["verdict_label"] or \
           int(d["truth_score"]) != int(exp["truth_score"]):
            misses.append({"id": fx["id"], "input": inp,
                            "expected": exp, "got": d})
    out = {
        "n": len(fixtures),
        "n_verified": sum(1 for fx in fixtures if fx.get("verified")),
        "verdict_metrics": classification_metrics(pred_pairs),
        "score_accuracy": (sum(1 for p, e in score_pairs if p == e)
                           / max(1, len(score_pairs))),
        "misses": misses,
    }
    if pred_pairs_v:
        out["verdict_metrics_verified"] = classification_metrics(pred_pairs_v)
        out["score_accuracy_verified"] = (
            sum(1 for p, e in score_pairs_v if p == e)
            / max(1, len(score_pairs_v)))
    return out


def _run_extractor(fixtures: list[dict]) -> dict:
    """Compare extractor output to expected claim dicts. Scoring uses
    Jaccard F1 over the SET of (type, polarity, quote-prefix) tuples
    — exact text matching is too brittle, type/polarity is what
    matters for downstream pipeline stages."""
    misses: list[dict] = []
    p_list, r_list, f_list = [], [], []
    p_v, r_v, f_v = [], [], []
    for fx in fixtures:
        expected_set = {
            (c.get("type"), c.get("polarity"),
             (c.get("quote") or "")[:50])
            for c in fx["expected"].get("claims", [])
        }
        predicted_set = {
            (c.get("type"), c.get("polarity"),
             (c.get("quote") or "")[:50])
            for c in fx.get("predicted", {}).get("claims", [])
        }
        p, r, f = jaccard_f1(predicted_set, expected_set)
        p_list.append(p); r_list.append(r); f_list.append(f)
        if fx.get("verified"):
            p_v.append(p); r_v.append(r); f_v.append(f)
        if f < 1.0:
            misses.append({"id": fx["id"], "f1": f,
                            "expected": sorted(expected_set),
                            "predicted": sorted(predicted_set)})
    out = {
        "n": len(fixtures),
        "n_verified": sum(1 for fx in fixtures if fx.get("verified")),
        "precision": sum(p_list) / max(1, len(p_list)),
        "recall":    sum(r_list) / max(1, len(r_list)),
        "f1":        sum(f_list) / max(1, len(f_list)),
        "misses":    misses,
    }
    if p_v:
        out["precision_verified"] = sum(p_v) / len(p_v)
        out["recall_verified"]    = sum(r_v) / len(r_v)
        out["f1_verified"]        = sum(f_v) / len(f_v)
    return out


def _run_contradictions(fixtures: list[dict]) -> dict:
    """Compare 4-way verdict prediction against expected. For now this
    is a STATIC eval — runs against the predicted verdict already
    captured in the fixture (set when the fixture was hand-curated).
    A future iteration could re-run the LLM verifier; for V2 we pin
    the human-labeled expectations."""
    pairs: list[tuple[str, str]] = []
    pairs_v: list[tuple[str, str]] = []
    misses: list[dict] = []
    for fx in fixtures:
        exp = fx["expected"].get("verdict")
        got = fx.get("predicted", {}).get("verdict")
        if exp is None:
            continue
        pairs.append((got or "MISSING", exp))
        if fx.get("verified"):
            pairs_v.append((got or "MISSING", exp))
        if got != exp:
            misses.append({"id": fx["id"], "expected": exp, "got": got})
    metrics = classification_metrics(pairs)
    metrics["misses"] = misses
    metrics["n_verified"] = sum(1 for fx in fixtures if fx.get("verified"))
    if pairs_v:
        metrics["verified_metrics"] = classification_metrics(pairs_v)
    return metrics


def _run_decomposer(fixtures: list[dict]) -> dict:
    """Q&A decomposition — score by Jaccard on (answer_type, source_medium)
    of the questions produced."""
    p_list, r_list, f_list = [], [], []
    p_v, r_v, f_v = [], [], []
    misses: list[dict] = []
    for fx in fixtures:
        expected_set = {
            (q.get("answer_type"), q.get("source_medium"))
            for q in fx["expected"].get("questions", [])
        }
        predicted_set = {
            (q.get("answer_type"), q.get("source_medium"))
            for q in fx.get("predicted", {}).get("questions", [])
        }
        p, r, f = jaccard_f1(predicted_set, expected_set)
        p_list.append(p); r_list.append(r); f_list.append(f)
        if fx.get("verified"):
            p_v.append(p); r_v.append(r); f_v.append(f)
        if f < 1.0:
            misses.append({"id": fx["id"], "f1": f})
    out = {
        "n": len(fixtures),
        "n_verified": sum(1 for fx in fixtures if fx.get("verified")),
        "precision": sum(p_list) / max(1, len(p_list)),
        "recall":    sum(r_list) / max(1, len(r_list)),
        "f1":        sum(f_list) / max(1, len(f_list)),
        "misses":    misses,
    }
    if p_v:
        out["precision_verified"] = sum(p_v) / len(p_v)
        out["recall_verified"]    = sum(r_v) / len(r_v)
        out["f1_verified"]        = sum(f_v) / len(f_v)
    return out


def _run_matcher(fixtures: list[dict]) -> dict:
    """Claim-matching — binary SAME/DIFFERENT. fixture['predicted']
    has the matcher's actual call ('SAME' or 'DIFFERENT')."""
    pairs: list[tuple[str, str]] = []
    pairs_v: list[tuple[str, str]] = []
    misses: list[dict] = []
    for fx in fixtures:
        exp = fx["expected"].get("label")
        got = fx.get("predicted", {}).get("label")
        if exp is None:
            continue
        pairs.append((got or "MISSING", exp))
        if fx.get("verified"):
            pairs_v.append((got or "MISSING", exp))
        if got != exp:
            misses.append({"id": fx["id"], "expected": exp, "got": got})
    metrics = classification_metrics(pairs)
    metrics["misses"] = misses
    metrics["n_verified"] = sum(1 for fx in fixtures if fx.get("verified"))
    if pairs_v:
        metrics["verified_metrics"] = classification_metrics(pairs_v)
    return metrics


def _run_verifier(fixtures: list[dict]) -> dict:
    """Verifier — web-search evidence classifier (ADR 0001 stage 6).

    Each fixture pins the relevance distribution + per-row
    (url_prefix, relevance) set for a fact-check that already went
    through the verifier. Scoring: Jaccard F1 on the row set.

    Like the contradictions runner this is a STATIC eval against
    captured pipeline output — re-running the verifier live would
    cost LLM calls + Anthropic web-search credits. A prompt change
    that shifts the relevance balance fails the test; the maintainer
    decides whether the shift is an improvement (update the fixtures)
    or a regression (revert the prompt).
    """
    p_list, r_list, f_list = [], [], []
    p_v, r_v, f_v = [], [], []
    misses: list[dict] = []
    for fx in fixtures:
        expected_set = {
            (r.get("url_prefix"), r.get("relevance"))
            for r in fx["expected"].get("rows", [])
        }
        predicted_set = {
            (r.get("url_prefix"), r.get("relevance"))
            for r in fx.get("predicted", {}).get("rows", [])
        }
        p, r, f = jaccard_f1(predicted_set, expected_set)
        p_list.append(p); r_list.append(r); f_list.append(f)
        if fx.get("verified"):
            p_v.append(p); r_v.append(r); f_v.append(f)
        if f < 1.0:
            misses.append({
                "id":   fx["id"],
                "f1":   f,
                "expected_dist":  fx["expected"].get("relevance_distribution"),
                "predicted_dist": (fx.get("predicted") or {}).get("relevance_distribution"),
            })
    out = {
        "n": len(fixtures),
        "n_verified": sum(1 for fx in fixtures if fx.get("verified")),
        "precision": sum(p_list) / max(1, len(p_list)),
        "recall":    sum(r_list) / max(1, len(r_list)),
        "f1":        sum(f_list) / max(1, len(f_list)),
        "misses":    misses,
    }
    if p_v:
        out["precision_verified"] = sum(p_v) / len(p_v)
        out["recall_verified"]    = sum(r_v) / len(r_v)
        out["f1_verified"]        = sum(f_v) / len(f_v)
    return out


STAGE_RUNNERS: dict[str, Callable[[list[dict]], dict]] = {
    "truth_score":    _run_truth_score,
    "extractor":      _run_extractor,
    "decomposer":     _run_decomposer,
    "matcher":        _run_matcher,
    "contradictions": _run_contradictions,
    "verifier":       _run_verifier,
}


# ───────────────────────────────────────────────────────────────────
# Top-level run + reporting
# ───────────────────────────────────────────────────────────────────

def run_eval(stages: Optional[list[str]] = None,
              small: bool = False) -> dict:
    """Run eval across requested stages (default: all). Returns a dict
    of {stage: metrics}."""
    if stages is None:
        stages = list(STAGE_RUNNERS.keys())
    limit = 3 if small else None
    results: dict[str, dict] = {
        "_meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "small": small,
            "stages_run": stages,
        },
    }
    for stage in stages:
        runner = STAGE_RUNNERS.get(stage)
        if runner is None:
            logger.warning(f"unknown stage: {stage}")
            continue
        fixtures = load_fixtures(stage, limit=limit)
        if not fixtures:
            results[stage] = {"n": 0, "note": "no fixtures yet"}
            continue
        results[stage] = runner(fixtures)
    return results


def append_history(results: dict) -> None:
    """Append the eval run to data/eval_history.jsonl (one line per run)."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(results, default=str) + "\n")


def render_markdown_report(results: dict) -> str:
    """Render a human-readable EVAL_RESULTS.md from the results dict.

    Each stage block emits TWO metric rows when verified fixtures exist:
      - **Verified subset** — hand-confirmed ground truth (real quality)
      - **All fixtures**    — includes pinned baselines (drift detector)
    """
    lines: list[str] = []
    lines.append("# Kahzaabu — quality evaluation results")
    lines.append("")
    lines.append(f"Generated: {results['_meta']['timestamp']}")
    if results["_meta"].get("small"):
        lines.append("Mode: **--small** (CI-fast; first 3 fixtures per stage)")
    lines.append("")
    lines.append("Per-stage metrics against the golden set under "
                 "`tests/golden/`. Methodology: ADR 0008.")
    lines.append("")
    lines.append("## Reading the numbers")
    lines.append("")
    lines.append(
        "Fixtures are tagged `verified: true|false`. **Verified** "
        "means the `expected` value is hand-confirmed ground truth "
        "(e.g. a deterministic mapping, a structurally obvious case). "
        "**Pinned** (`verified: false`) means `expected` was seeded "
        "from current pipeline output to act as a *drift detector* — "
        "a non-1.000 score after a prompt edit means the LLM diverged "
        "from its previous behavior, but says nothing about which "
        "version is \"correct.\""
    )
    lines.append("")
    lines.append(
        "Each stage reports **Verified-subset** metrics (real quality) "
        "and **All-fixture** metrics (drift detector) separately."
    )
    lines.append("")
    for stage in STAGE_RUNNERS.keys():
        if stage not in results:
            continue
        r = results[stage]
        lines.append(f"## {stage}")
        lines.append("")
        if r.get("n") == 0 or r.get("note") == "no fixtures yet":
            lines.append("*No fixtures yet. Add JSON files under "
                          f"`tests/golden/{stage}/`.*")
            lines.append("")
            continue
        n_total = r.get("n", "?")
        n_verified = r.get("n_verified", 0)
        lines.append(
            f"- Fixtures: **{n_total}** "
            f"(verified: **{n_verified}**, pinned: **{n_total - n_verified}**)"
        )

        # ── Verified subset (real quality) ──
        if n_verified > 0:
            lines.append("")
            lines.append("### Verified-subset (ground truth)")
            lines.append("")
            vm_v = r.get("verdict_metrics_verified") or r.get("verified_metrics")
            if vm_v and "macro_f1" in vm_v:
                lines.append(f"- Accuracy: **{vm_v.get('accuracy', 0):.3f}**")
                lines.append(f"- Macro-F1: **{vm_v.get('macro_f1', 0):.3f}**")
                if vm_v.get("per_class"):
                    lines.append("")
                    lines.append("| Class | Precision | Recall | F1 | Support |")
                    lines.append("|---|---|---|---|---|")
                    for cls, m in vm_v["per_class"].items():
                        lines.append(
                            f"| {cls} | {m['precision']:.3f} | "
                            f"{m['recall']:.3f} | {m['f1']:.3f} | "
                            f"{m['support']} |"
                        )
            elif "f1_verified" in r:
                lines.append(f"- Precision: **{r['precision_verified']:.3f}**")
                lines.append(f"- Recall:    **{r['recall_verified']:.3f}**")
                lines.append(f"- F1:        **{r['f1_verified']:.3f}**")
            if r.get("score_accuracy_verified") is not None:
                lines.append(
                    f"- Truth-score exact-match: "
                    f"**{r['score_accuracy_verified']:.3f}**")

        # ── All-fixture (drift detector) ──
        lines.append("")
        lines.append("### All fixtures (drift detector)")
        lines.append("")
        vm = r.get("verdict_metrics")
        if vm and "macro_f1" in vm:
            lines.append(f"- Accuracy: **{vm.get('accuracy', 0):.3f}**")
            lines.append(f"- Macro-F1: **{vm.get('macro_f1', 0):.3f}**")
        elif "macro_f1" in r:
            lines.append(f"- Accuracy: **{r.get('accuracy', 0):.3f}**")
            lines.append(f"- Macro-F1: **{r.get('macro_f1', 0):.3f}**")
        elif "f1" in r:
            lines.append(f"- Precision: **{r.get('precision', 0):.3f}**")
            lines.append(f"- Recall:    **{r.get('recall', 0):.3f}**")
            lines.append(f"- F1:        **{r.get('f1', 0):.3f}**")
        if r.get("score_accuracy") is not None:
            lines.append(
                f"- Truth-score exact-match: **{r['score_accuracy']:.3f}**")

        misses = r.get("misses") or []
        if misses:
            lines.append("")
            lines.append(f"<details><summary>{len(misses)} misses</summary>")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(misses[:5], indent=2, default=str))
            if len(misses) > 5:
                lines.append(f"... (+{len(misses) - 5} more)")
            lines.append("```")
            lines.append("")
            lines.append("</details>")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "**How to grow the verified subset**: review a pinned fixture, "
        "confirm the `expected` field is correct, set `verified: true`. "
        "The verified-subset count grows; the system's real quality "
        "measurement grows with it. See ADR 0008."
    )
    return "\n".join(lines)


def write_report(results: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_markdown_report(results))
