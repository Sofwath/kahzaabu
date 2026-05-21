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
| 0 | Bootstrap: ADRs + build plan + ARCHITECTURE skeleton | 🟢 in progress | 0001-0006 | docs-only |
| 1 | Claims enrichment (polarity / subject / is_checkable) | ⚪ pending | 0002 | DB + extractor + tests |
| 2 | Q&A decomposition + backfill | ⚪ pending | 0001 | new claim_questions table; ~$200 backfill |
| 3 | Claim matching (canonical_claim_id, embeddings + entity) | ⚪ pending | 0003 | new claim_embeddings table |
| 4 | Contradiction finder (the headline feature) | ⚪ pending | 0004 | new contradiction_pairs table + pipeline stage |
| 5 | AVeriTeC verdict + Truth-O-Meter + RAGAR reasoning_chain | ⚪ pending | 0005 | enrich fact_checks; derivation function |
| 6 | ClaimReview JSON-LD export | ⚪ pending | 0006 | per-factcheck + feed endpoint |
| 7 | Web UI: Truth-O-Meter card + Q&A trace + /contradictions | ⚪ pending | — | UX |
| 8 | `kahzaabu-fact-check` agentskills.io skill | ⚪ pending | — | hermes skill for external use |
| 9 | docs/ARCHITECTURE.md fill-in + citation block | ⚪ pending | — | reference-project polish |

## Definition of done (the whole V2)

When Slice 9 lands, the project is V2 if:

1. Every claim has `polarity`, `subject_normalized`, `is_checkable`, `canonical_claim_id`.
2. Every checkable claim has at least one row in `claim_questions`.
3. Every published fact-check has `verdict_label`, `truth_score`, `truth_score_label`, `reasoning_chain`, `claimreview_jsonld`, `speaker`.
4. `/contradictions` exists in the web UI and lists `contradiction_pairs` with the 4-way verdict.
5. Per-fact-check page emits valid ClaimReview JSON-LD that passes the Google Rich Results Test.
6. The `kahzaabu-fact-check` skill is installable in hermes and produces a complete fact-check trace for an arbitrary claim string.
7. `docs/ARCHITECTURE.md` has a citation block, a flow diagram, and links to ADRs 0001–0006.
8. All 25+ existing tests still pass; new slices add their own tests.
9. `./scripts/ci-dry-run.sh` passes against the final commit.

## Costs

| Item | Spend | Recoverable? |
|---|---|---|
| Q&A decomposition backfill (Slice 2) | ~$200 (9,000 claims × ~$0.02) | One-shot, irreversible |
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
