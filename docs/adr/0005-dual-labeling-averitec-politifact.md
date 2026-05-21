# ADR 0005 — Dual labeling: AVeriTeC verdict + PolitiFact Truth-O-Meter

**Status**: Accepted (2026-05-21)

## Context

Today's `fact_checks.category` field uses analytical labels (`LIE`, `MISLEADING`, `BROKEN DEADLINE`, `CREDIT THEFT`, `SHIFTING NUMBERS`, `CONTRADICTION`) that are useful for internal analysis but unfamiliar to public readers. PolitiFact's 6-rung Truth-O-Meter (`TRUE` → `PANTS ON FIRE`) is the format civic-tech audiences recognize. AVeriTeC's 4-way verdict (`SUPPORTED` / `REFUTED` / `NOT_ENOUGH_EVIDENCE` / `CONFLICTING_EVIDENCE`) is the academic benchmark format.

We need to decide whether to replace categories, replace them with rungs, keep all three, or derive one from another.

## Decision

Keep ALL THREE label layers, derived in this order:

```
category  →  verdict_label  →  truth_score / truth_score_label
(domain     (AVeriTeC          (PolitiFact 1-6 rung)
analytical)  benchmark)
```

1. **`fact_checks.category`** stays as-is. Analytical, kahzaabu-specific. Drives internal queries and the curator stage.
2. **`fact_checks.verdict_label`** (NEW) is the AVeriTeC verdict, derived from category + evidence:

   | Category | Default verdict_label | Override condition |
   |---|---|---|
   | `LIE` | `REFUTED` | — |
   | `CONTRADICTION` | `REFUTED` | — |
   | `MISLEADING` | `CONFLICTING_EVIDENCE` | — |
   | `BROKEN DEADLINE` | `REFUTED` | If deadline genuinely renegotiated: `CONFLICTING_EVIDENCE` |
   | `SHIFTING NUMBERS` | `CONFLICTING_EVIDENCE` | — |
   | `CREDIT THEFT` | `REFUTED` (credit claim is false) | If credit ambiguous: `CONFLICTING_EVIDENCE` |
   | (no curation yet, only verify done) | `NOT_ENOUGH_EVIDENCE` | If verifier found strong evidence: `SUPPORTED` |

3. **`fact_checks.truth_score` (1-6)** and `truth_score_label`, derived from verdict_label + confidence + evidence_count via a deterministic function:

   | truth_score | label | Derivation |
   |---|---|---|
   | 6 | `TRUE` | `SUPPORTED` + confidence ≥ 0.85 |
   | 5 | `MOSTLY_TRUE` | `SUPPORTED` + 0.6 ≤ confidence < 0.85 |
   | 4 | `HALF_TRUE` | `CONFLICTING_EVIDENCE` |
   | 3 | `MOSTLY_FALSE` | `REFUTED` + confidence < 0.7 |
   | 2 | `FALSE` | `REFUTED` + 0.7 ≤ confidence < 0.95 |
   | 1 | `PANTS_ON_FIRE` | `REFUTED` + confidence ≥ 0.95 + category in {`LIE`} |

The mapping function lives in `kahzaabu/truth_score.py` with its own unit tests. Changes to the mapping are ADR-worthy events.

## Alternatives considered

- **Replace categories with PolitiFact rungs.** Rejected — we lose 218 fact-checks worth of analytical signal. The rungs collapse `CREDIT THEFT`, `SHIFTING NUMBERS`, and `BROKEN DEADLINE` into "false-ish", erasing the diagnostic value.
- **Replace categories with AVeriTeC labels.** Rejected — `CONFLICTING_EVIDENCE` is too coarse for the kahzaabu corpus where most issues are specifically deadline / credit / numbers.
- **Derive only one of {verdict_label, truth_score}.** Rejected — they serve different audiences (researchers vs. public) and the cost of carrying both is ~10 bytes per fact-check.

## Consequences

**Positive.**

- ClaimReview JSON-LD can emit `reviewRating.ratingValue = truth_score` and `reviewRating.alternateName = truth_score_label` — exactly what Google Fact Check Explorer indexes.
- AVeriTeC-labeled output is directly comparable to the academic benchmark; kahzaabu becomes a citable corpus for political fact-checking research.
- Public web UI shows the Truth-O-Meter badge; admin / researcher views can drill into category + verdict_label.

**Negative.**

- Three labels per fact-check means three places to maintain. The derivation function plus its tests is the contract: any change must go through `truth_score.py` and update the test fixture.
- The 218 existing fact-checks need backfilling for `verdict_label` + `truth_score`. Deterministic from category + confidence — no LLM cost — but is a schema migration.
- The mapping table above is a judgment call. Different reasonable people would draw the lines differently. ADRs let us version the mapping; if we change it, the old mapping survives in git history.
