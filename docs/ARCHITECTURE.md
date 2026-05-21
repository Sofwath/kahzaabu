# Kahzaabu — Architecture

> **STATUS: V2 in progress.** This document describes the V2 design that is currently being built. Sections marked `⚪ pending` reflect work-in-progress; sections marked `✅ shipped` are live in the codebase. See `docs/V2_BUILD_PLAN.md` for slice-level progress.

Kahzaabu is an automated political fact-checking pipeline for the Maldives. It combines published academic and industry best practices into a single end-to-end system: claim extraction (Full Fact), Q&A decomposition (AVeriTeC), Chain-of-RAG verification (RAGAR), dual-axis verdicts (AVeriTeC + PolitiFact), schema.org discoverability (ClaimReview), and machine-checkable contradiction records (kahzaabu-original).

This document is the **reference map** for the project. Every architectural choice points to an ADR (Architecture Decision Record) under `docs/adr/`. Every component points to a code module under `kahzaabu/`. Every published method points to a citation.

---

## 1. Citations & references

This project draws on:

- **Schema.org ClaimReview** — <https://schema.org/ClaimReview>. The structured-data format for discoverable fact-checks.
- **Full Fact AI workflow** — Babakar et al., Full Fact, <https://fullfact.org/about/automated/>. Claim detection, claim matching, monitoring, ClaimReview publication.
- **FEVER** — Thorne et al., NAACL 2018, "FEVER: a Large-scale Dataset for Fact Extraction and VERification". Sentence-level evidence, 3-way verdicts.
- **AVeriTeC** — Schlichtkrull et al., EMNLP 2023, "AVeriTeC: A Dataset for Real-world Claim Verification with Evidence from the Web". Q&A-structured evidence, 4-way verdicts. <https://fever.ai/dataset/averitec.html>
- **RAGAR** — Khaliq et al., arXiv 2404.12065, "RAGAR, Your Falsehood Radar: RAG-Augmented Reasoning for Political Fact-Checking using Multimodal Large Language Models". Chain-of-RAG and Tree-of-RAG.
- **PolitiFact** — <https://www.politifact.com/article/2018/feb/12/principles-truth-o-meter-politifacts-methodology-i/>. The Truth-O-Meter and claim-selection methodology.

**To cite kahzaabu** (placeholder until paper/DOI exists):
```
Kahzaabu: Automated Political Fact-Checking for the Maldives Presidency, 2025.
https://github.com/<TBD>
```

---

## 2. System overview

```
                      [presidency.gov.mv: EN + DV press releases]
                                       │
                                       ▼  scrape (Slice 0 of pipeline)
              ┌────────────────────────┴────────────────────────────┐
              │                  SQLite (data/kahzaabu.db)           │
              │                                                      │
              │   articles ── claims ── claim_questions              │
              │       │         │           (AVeriTeC Q&A)           │
              │       │         ├── claim_embeddings (canonical)     │
              │       │         │                                    │
              │       │         └── contradiction_pairs              │
              │       │            (kahzaabu original; 4-way)        │
              │       │                                              │
              │       ├── fact_checks ── fact_check_evidence         │
              │       │   (verdict_label + truth_score + JSON-LD)    │
              │       │                                              │
              │       ├── article_fact_cards                         │
              │       ├── dv_en_inconsistencies                      │
              │       ├── manifesto_promises                         │
              │       └── constitution_articles + _fts5              │
              └──────────────────────────────────────────────────────┘
                                       │
              ┌────────────────────────┼──────────────────────────┐
              ▼                        ▼                          ▼
      CLI / TUI                   Web UI                  Hermes plugin
   (kahzaabu …)            (FastAPI :8765)         (in-process tools +
                                                    /kahzaabu slash +
                                                    kahzaabu-fact-check skill)
```

---

## 3. Pipeline stages

V2 pipeline has 9 stages in sequence (V1 had 6). Each stage is idempotent and budget-gated.

| # | Stage | ADR | Code (V2 target) | Best-practice source |
|---|---|---|---|---|
| 1 | `scrape` | — | `kahzaabu/scraper.py` | — (in-house) |
| 2 | `extract` (now also: polarity, subject, is_checkable) | 0002 | `kahzaabu/extractor.py` | Full Fact claim detection (BERT classifier; we use Sonnet instead) |
| 3 | `decompose` (new) | 0001 | `kahzaabu/decomposer.py` ⚪ | AVeriTeC Q&A structure; RAGAR Chain-of-RAG |
| 4 | `match` (new — canonical_claim_id) | 0003 | `kahzaabu/matcher.py` ⚪ | Full Fact claim matching |
| 5 | `find_contradictions` (new — the headline feature) | 0004 | `kahzaabu/contradictions.py` ⚪ | kahzaabu original |
| 6 | `inspect` (per-article fact card) | — | `kahzaabu/inspector.py` | — (in-house) |
| 7 | `curate` (writes fact_checks with V2 columns) | 0005 | `kahzaabu/curator.py` (refactored) | RAGAR synthesis |
| 8 | `verify` (Q&A-driven web search) | — | `kahzaabu/verifier.py` (refactored) | AVeriTeC evidence model |
| 9 | `export_jsonld` (new) | 0006 | `kahzaabu/claimreview.py` ⚪ | schema.org ClaimReview |
| 10 | `dv-compare` (EN/DV diff) | — | `kahzaabu/dv_compare.py` | — (in-house) |
| 11 | `constitution_check` (live in qna_agentic) | — | `kahzaabu/constitution.py` | — (in-house) |

> ⚪ = code does not yet exist; will be added in the relevant V2 slice.

---

## 4. Data model

⚪ Pending (will be filled in Slice 9 — the comprehensive schema reference once all V2 columns and tables are live). See `docs/V2_BUILD_PLAN.md` for column-level changes per slice; see `README.md` "Data model" section for the V1 baseline.

---

## 5. The contradiction detector (V2 headline feature)

⚪ Pending detailed write-up (Slice 9). High-level: claims tagged with polarity at extraction time → polarity-pair SQL shortlist on `subject_normalized` clusters → LLM verifier classifies each pair as `CONTRADICTION` / `EVOLVING_POSITION` / `CONTEXT_CHANGED` / `NOT_CONTRADICTORY` (ADR 0004) → records persisted with reasoning chain → only `CONTRADICTION` produces a published fact-check.

This is the piece that goes beyond porting existing systems. Every contradiction is machine-checkable: two `claim_id` foreign keys, a JSON reasoning chain, a 4-way verdict, and a confidence score.

---

## 6. Verdict & label system

⚪ Pending (will be filled in Slice 5). Triple-layer labeling per ADR 0005: `category` (kahzaabu analytical) → `verdict_label` (AVeriTeC academic) → `truth_score` + `truth_score_label` (PolitiFact public). Derivation function in `kahzaabu/truth_score.py`.

---

## 7. ClaimReview JSON-LD export

⚪ Pending (will be filled in Slice 6). Per ADR 0006: every published fact-check generates and stores a `claimreview_jsonld` blob. Served as inline `<script>` on the per-fact-check web page AND as a `/api/factchecks/{id}/jsonld` endpoint AND as a `/api/claimreviews/feed.json` aggregate.

---

## 8. Agent / skill surface

⚪ Pending (will be filled in Slices 7 & 8).

V2 adds:
- 4 new agent tools in the hermes plugin: `kahzaabu_decompose_claim`, `kahzaabu_contradictions_about`, `kahzaabu_truth_score`, `kahzaabu_claimreview_jsonld`.
- 1 new agentskills.io-format skill `kahzaabu-fact-check` that runs the full pipeline against an arbitrary input claim.

---

## 9. What's NOT in V2

See `docs/V2_BUILD_PLAN.md` § Out of scope. Briefly: public VPS deploy, multimodal verification, native-Dhivehi LLM, real-time monitoring.
