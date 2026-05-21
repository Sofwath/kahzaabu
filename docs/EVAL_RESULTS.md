# Kahzaabu — quality evaluation results

Generated: 2026-05-21T13:32:24.679269+00:00

Per-stage metrics against the golden set under `tests/golden/`. Methodology: ADR 0008.

## Reading the numbers

Fixtures are tagged `verified: true|false`. **Verified** means the `expected` value is hand-confirmed ground truth (e.g. a deterministic mapping, a structurally obvious case). **Pinned** (`verified: false`) means `expected` was seeded from current pipeline output to act as a *drift detector* — a non-1.000 score after a prompt edit means the LLM diverged from its previous behavior, but says nothing about which version is "correct."

Each stage reports **Verified-subset** metrics (real quality) and **All-fixture** metrics (drift detector) separately.

## truth_score

- Fixtures: **6** (verified: **6**, pinned: **0**)

### Verified-subset (ground truth)

- Accuracy: **1.000**
- Macro-F1: **1.000**

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| CONFLICTING_EVIDENCE | 1.000 | 1.000 | 1.000 | 1 |
| NOT_ENOUGH_EVIDENCE | 1.000 | 1.000 | 1.000 | 1 |
| REFUTED | 1.000 | 1.000 | 1.000 | 4 |
- Truth-score exact-match: **1.000**

### All fixtures (drift detector)

- Accuracy: **1.000**
- Macro-F1: **1.000**
- Truth-score exact-match: **1.000**

## extractor

- Fixtures: **4** (verified: **3**, pinned: **1**)

### Verified-subset (ground truth)

- Precision: **1.000**
- Recall:    **1.000**
- F1:        **1.000**

### All fixtures (drift detector)

- Precision: **1.000**
- Recall:    **1.000**
- F1:        **1.000**

## decomposer

- Fixtures: **4** (verified: **4**, pinned: **0**)

### Verified-subset (ground truth)

- Precision: **1.000**
- Recall:    **1.000**
- F1:        **1.000**

### All fixtures (drift detector)

- Precision: **1.000**
- Recall:    **1.000**
- F1:        **1.000**

## matcher

- Fixtures: **6** (verified: **6**, pinned: **0**)

### Verified-subset (ground truth)

- Accuracy: **1.000**
- Macro-F1: **1.000**

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| DIFFERENT | 1.000 | 1.000 | 1.000 | 3 |
| SAME | 1.000 | 1.000 | 1.000 | 3 |

### All fixtures (drift detector)

- Accuracy: **1.000**
- Macro-F1: **1.000**

## contradictions

- Fixtures: **5** (verified: **5**, pinned: **0**)

### Verified-subset (ground truth)

- Accuracy: **1.000**
- Macro-F1: **1.000**

| Class | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| CONTRADICTION | 1.000 | 1.000 | 1.000 | 2 |
| NOT_CONTRADICTORY | 1.000 | 1.000 | 1.000 | 3 |

### All fixtures (drift detector)

- Accuracy: **1.000**
- Macro-F1: **1.000**

## verifier

- Fixtures: **8** (verified: **0**, pinned: **8**)

### All fixtures (drift detector)

- Precision: **1.000**
- Recall:    **1.000**
- F1:        **1.000**

---

**How to grow the verified subset**: review a pinned fixture, confirm the `expected` field is correct, set `verified: true`. The verified-subset count grows; the system's real quality measurement grows with it. See ADR 0008.