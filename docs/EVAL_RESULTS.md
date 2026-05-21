# Kahzaabu — quality evaluation results

Generated: 2026-05-21T06:50:19.781612+00:00

Per-stage metrics against the hand-labeled golden set under `tests/golden/`. Methodology: ADR 0008.

## truth_score

- Fixtures: **6**
- Verdict accuracy: **1.000**
- Verdict macro-F1: **1.000**

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| CONFLICTING_EVIDENCE | 1.000 | 1.000 | 1.000 | 1 |
| NOT_ENOUGH_EVIDENCE | 1.000 | 1.000 | 1.000 | 1 |
| REFUTED | 1.000 | 1.000 | 1.000 | 4 |
- Truth-score exact-match: **1.000**

## extractor

- Fixtures: **4**
- Precision: **1.000**
- Recall:    **1.000**
- F1:        **1.000**

## decomposer

- Fixtures: **4**
- Precision: **1.000**
- Recall:    **1.000**
- F1:        **1.000**

## matcher

- Fixtures: **6**
- Accuracy: **1.000**
- Macro-F1: **1.000**

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| DIFFERENT | 1.000 | 1.000 | 1.000 | 3 |
| SAME | 1.000 | 1.000 | 1.000 | 3 |

## contradictions

- Fixtures: **5**
- Accuracy: **1.000**
- Macro-F1: **1.000**

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| CONTRADICTION | 1.000 | 1.000 | 1.000 | 2 |
| NOT_CONTRADICTORY | 1.000 | 1.000 | 1.000 | 3 |

---

**How to grow the golden set**: see ADR 0008. Add new `tests/golden/<stage>/<id>.json` files with shape `{id, input, expected, notes}`. Re-run `kahzaabu eval` to refresh this report.