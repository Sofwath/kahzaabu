# ADR 0001 — V2 architecture overview

**Status**: Accepted (2026-05-21)

## Context

Kahzaabu V1 shipped a working fact-check pipeline (scrape → extract → curate → verify → dv-compare) and an agentic Q&A loop that has produced 218 published fact-checks across 3,099 articles. It works, but it is **not** a reference project: contradictions are a side-effect of curation rather than first-class records, claims have no polarity / subject-resolution / Q&A decomposition, fact-checks lack a numeric truth score and the public ClaimReview JSON-LD that lets Google Fact Check Explorer surface them, and the data model has no equivalent of FEVER/AVeriTeC's evidence-per-question structure.

We surveyed the canonical references:

- **ClaimReview** (schema.org) — the discoverability format every public fact-check publisher uses.
- **Full Fact's AI workflow** — claim detection → claim matching → live monitoring → human verification → ClaimReview publication. Key insight: *one canonical record per claim, not N*.
- **FEVER** (NAACL 2018) — `SUPPORTS / REFUTES / NOT ENOUGH INFO` verdicts with sentence-level evidence.
- **AVeriTeC** (EMNLP 2023) — the modern real-world fact-checking benchmark. Adds `CONFLICTING_EVIDENCE/CHERRY-PICKING` and structures evidence as `(question, answer, source_url, source_medium, answer_type)`. **Best fit for our political-claim domain.**
- **RAGAR** (arXiv 2404.12065) — Retrieval-Augmented Generation Augmented Reasoning. Chain-of-RAG decomposes claims into sub-questions, retrieves per question, and chains answers into a verdict. Hits F1=0.85 on political fact-checking.
- **PolitiFact** Truth-O-Meter — 6 public-facing rungs (`TRUE` → `PANTS ON FIRE`) for human readability.

## Decision

V2 adopts a hybrid drawing the strongest piece from each:

1. **AVeriTeC** structure for evidence — Q&A decomposition becomes a first-class table.
2. **RAGAR** Chain-of-RAG flow for the verify stage — each claim_question is answered, then answers chain into a verdict.
3. **AVeriTeC** verdict labels (`SUPPORTED / REFUTED / NOT_ENOUGH_EVIDENCE / CONFLICTING_EVIDENCE`).
4. **PolitiFact** Truth-O-Meter as the public-facing label, derived deterministically from category + AVeriTeC verdict + evidence count.
5. **Full Fact** claim matching — `canonical_claim_id` clusters duplicates across articles.
6. **ClaimReview JSON-LD** export per published fact-check (ADR 0006).
7. **First-class contradictions** — `contradiction_pairs` table with 4-way verdict (ADR 0004). This is the user's headline priority and not directly inherited from any prior system; it's where kahzaabu becomes more than a port.

These changes are landed as ten ordered slices, each complete + tested before the next. See `docs/V2_BUILD_PLAN.md`.

## Alternatives considered

- **Stay with V1 + ClaimReview export only.** Discoverability win without architectural debt; rejected because the user explicitly wanted a reference project, not a polish pass.
- **Adopt FEVER labels (3-way)** instead of AVeriTeC (4-way). Rejected — `NOT_ENOUGH_INFO` collapses two distinct evidential states (truly absent vs. genuinely conflicting), and political claims hit "cherry-picking" often.
- **Skip claim-matching (`canonical_claim_id`)** to ship faster. Rejected — Full Fact's data on this is unambiguous: without canonical records, a politician repeating the same claim 50 times generates 50 fact-checks, and downstream analytics lie.

## Consequences

**Positive.**

- Direct comparability with the academic SOTA benchmark (AVeriTeC), with the kahzaabu corpus becoming a citable extension of the literature.
- Public surfaceability via Google Fact Check Explorer once deployed.
- Machine-checkable contradiction records — every contradiction has two claim_ids and a reasoning chain.
- Public-readable truth labels for non-technical audiences.

**Negative.**

- Schema migration touches `claims` and `fact_checks`. The DB has 9,000+ claims; backfilling polarity, subject normalization, and Q&A decomposition costs ~$200 of LLM spend.
- Pipeline gains 3 new stages (decompose, match, find_contradictions). Cycle time per article extends by ~10s for new claims, ~2s for already-processed ones.
- The dual-labeling system (AVeriTeC + PolitiFact) means two derivable fields per fact-check; reviewers must understand both layers.

## Links

- ADR 0002 — Polarity taxonomy
- ADR 0003 — Canonical claim matching
- ADR 0004 — Contradiction verdict — 4-way
- ADR 0005 — Dual labeling — AVeriTeC + PolitiFact
- ADR 0006 — ClaimReview JSON-LD export
- `docs/V2_BUILD_PLAN.md` — implementation tracker
