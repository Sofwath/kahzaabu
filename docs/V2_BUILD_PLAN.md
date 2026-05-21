# Kahzaabu V2 — build plan

State-of-the-art rebuild of the fact-checking pipeline, combining AVeriTeC (verdict structure), RAGAR (Chain-of-RAG reasoning), Full Fact (claim matching), PolitiFact (Truth-O-Meter), schema.org ClaimReview (discoverability), and first-class contradiction records as the headline feature.

Architecture is documented in `docs/adr/0001-v2-architecture-overview.md`. Six decisions are pre-committed (ADRs 0001–0006).

## Slice tracker

Each slice is complete + tested before the next starts. Discipline:
- ✅ Tests pass via `python -m unittest discover tests/`
- ✅ `./scripts/test.sh` passes (unit + stale-name)
- ✅ `./scripts/ci-dry-run.sh` passes (fresh worktree validation)
- ✅ Schema drift test passes (`test_readme_schema_drift.py`)
- ✅ ADR written for any non-obvious choice
- ✅ Commit message references the slice + ADR(s)

| Slice | Title | Status | ADR(s) | Notes |
|---|---|---|---|---|
| 0 | Bootstrap: ADRs + build plan + ARCHITECTURE skeleton | ✅ done (95fd8c3) | 0001-0006 | docs-only |
| 1 | Claims enrichment (polarity / subject / is_checkable) | ✅ done (2e8c81d) | 0002 | DB + extractor + tests |
| 2 | Q&A decomposition + backfill | ✅ done (749fa6a + backfill) | 0001 | 8,954 / 8,954 claims; 35,648 questions; avg 3.98/claim; $12.51 total spend (vs $200 ADR projection — Haiku 4.5 outperformed expectations) |
| 3 | Claim matching (canonical_claim_id, embeddings + entity) | ✅ done | 0003, 0007 | 8,954/8,954 embedded; 151 paraphrase-grouped (1.7%); 53 LLM tiebreakers; **$0 spend** (local sentence-transformers); provider abstraction via ADR 0007 supports openai/voyage too |
| 4 | Contradiction finder (the headline feature) | ✅ done | 0004 | enrichment backfill: 8,954 claims @ $3; finder: 48 candidates → **2 CONTRADICTION verdicts** (judicial interference + external debt), 46 NOT_CONTRADICTORY, @ $0.41. Sonnet 4.6 verifier; semantic-similarity prefilter (cosine 0.55-0.95) keeps shortlist tractable. Total slice cost ~$3.50. |
| 5 | AVeriTeC verdict + Truth-O-Meter + RAGAR reasoning_chain | ✅ done | 0005 | 220/220 enriched (218 existing + 2 promoted from contradictions); 172 with reasoning_chain; deterministic derivation in kahzaabu/truth_score.py — $0 LLM cost; distribution: REFUTED 179, CONFLICTING_EVIDENCE 41 |
| 6 | ClaimReview JSON-LD export | ✅ done | 0006 | 218 published fact-checks have cached JSON-LD; 2 API endpoints (/api/factchecks/{id}/jsonld + /api/claimreviews/feed.json); Google Rich Results checklist passes; KAHZAABU_PUBLIC_BASE_URL env var; $0 cost |
| 7 | Web UI: Truth-O-Meter card + Q&A trace + /contradictions | ⚪ pending | — | UX |
| 8 | `kahzaabu-fact-check` agentskills.io skill | ⚪ pending | — | hermes skill for external use |
| 9 | docs/ARCHITECTURE.md fill-in + citation block | ⚪ pending | — | reference-project polish |
| 10 | Quality evals + prompt regression tests | ⚪ pending | 0008 | golden set per stage, `kahzaabu eval` CLI, F1 metrics, hash-based prompt drift detection |
| 11 | OSS readiness + model/data cards + backup | ⚪ pending | 0009 | LICENSE, CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, docs/MODEL_CARD.md, docs/DATA_CARD.md, scripts/backup.sh + restore.sh, arXiv-style methodology paper draft |
| 12 | Reproducibility manifest + observability + audit CLIs | ⚪ pending | 0010 | /api/reproducibility.json endpoint, prometheus_client, Grafana JSON, `kahzaabu audit` (bias/fairness), `kahzaabu transparency-report`, Dockerfile for one-command reproduction |

## Definition of done (the whole V2)

When Slices 0–12 land, the project is V2 / reference-project-grade if:

1. Every claim has `polarity`, `subject_normalized`, `is_checkable`, `canonical_claim_id`.
2. Every checkable claim has at least one row in `claim_questions`.
3. Every published fact-check has `verdict_label`, `truth_score`, `truth_score_label`, `reasoning_chain`, `claimreview_jsonld`, `speaker`.
4. `/contradictions` exists in the web UI and lists `contradiction_pairs` with the 4-way verdict.
5. Per-fact-check page emits valid ClaimReview JSON-LD that passes the Google Rich Results Test.
6. The `kahzaabu-fact-check` skill is installable in hermes and produces a complete fact-check trace for an arbitrary claim string.
7. `docs/ARCHITECTURE.md` has a citation block, a flow diagram, and links to ADRs 0001–0006.
8. All 25+ existing tests still pass; new slices add their own tests.
9. `./scripts/ci-dry-run.sh` passes against the final commit.
10. **Quality**: `kahzaabu eval` produces precision/recall/F1 per pipeline stage against a hand-labeled golden set; numbers reported in `docs/EVAL_RESULTS.md`; prompt regression detection in CI.
11. **OSS-ready**: LICENSE (Apache-2.0), CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, docs/MODEL_CARD.md (per stage), docs/DATA_CARD.md, scripts/backup.sh; cloneable + buildable from scratch with documented one-command setup.
12. **Reproducibility**: every published fact-check has a complete provenance trace queryable via `/api/reproducibility.json`; Dockerfile builds the stack; `kahzaabu audit` produces a bias/fairness summary; `kahzaabu transparency-report` generates a public-facing markdown report.

## Costs

| Item | Spend | Recoverable? |
|---|---|---|
| Q&A decomposition backfill (Slice 2) | **actual: $12.51** for 8,954 claims (Haiku 4.5, vs $200 Sonnet projection) | One-shot, irreversible |
| Embedding generation (Slice 3) | ~$1 (9,000 claims via text-embedding-3-small) | One-shot |
| Contradiction-finder LLM calls (Slice 4) | ~$5 (estimate ~100 pairs × $0.05) | Ongoing ~$1.50/month |
| Verdict + Truth-O-Meter derivation (Slice 5) | $0 (deterministic from existing data) | — |
| JSON-LD generation (Slice 6) | $0 (pure templating) | — |
| **Total one-shot V2 spend** | **~$206** | |
| **Ongoing additional cost vs V1** | **~$2/month** | |

## Rollback

Each slice is one commit; rolling back is `git revert <sha>`. The DB schema changes are additive (new columns / new tables); no existing columns are dropped or renamed. The dual-labeling system means we never lose the `category` field; if V2 doesn't pan out, the system reverts cleanly to V1 behaviour by ignoring the new columns.

## Out of scope

Decisions explicitly NOT in V2:

- **Public VPS deploy.** Tracked separately (`README.md` TODOs); JSON-LD is generated locally and validated, but indexing waits.
- **Multimodal claim verification** (RAGAR's image extension). Kahzaabu's corpus is text-only press releases; multimodal adds complexity for no immediate gain.
- **Dhivehi-source fact-checking.** V1 already pairs EN/DV via `dv_en_inconsistencies`; V2 builds atop this. Native-Dhivehi LLM verification is a separate large effort.
- **Real-time monitoring.** V2 keeps the 12h pipeline cycle. Live monitoring is a separate channel.
