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
| 7 | Web UI: Truth-O-Meter card + Q&A trace + /contradictions | ✅ done | — | Truth-O-Meter badges on /lies (6-rung color gradient); existing Q&A trace toggle preserved; new /contradictions page with 4-way verdict legend + filter + lazy reasoning-chain expand; /api/contradictions list + detail endpoints; /api/factchecks now returns V2 fields (verdict_label, truth_score, contradiction_pair_id, speaker) |
| 8 | `kahzaabu-fact-check` agentskills.io skill | ✅ done | — | skills/kahzaabu-fact-check/SKILL.md installable via scripts/install-hermes-skills.sh; live agent invocation produces structured verdict + Truth-O-Meter + reasoning chain + sources; loads + executes via `--skills kahzaabu-fact-check` |
| 9 | docs/ARCHITECTURE.md fill-in + citation block | ✅ done | — | All 11 sections complete (430 lines): full data model, 5-stage contradiction-detector flow with live numbers, 3-layer verdict/label derivation, ClaimReview JSON-LD anatomy, agent/skill surface with install instructions, reproducibility map linking code modules to source papers, citation block |
| 10 | Quality evals + prompt regression tests | ✅ done | 0008 | 23 golden fixtures across 5 stages; `kahzaabu eval` CLI with --small (CI) + per-stage filter; macro-F1 + per-class metrics; docs/EVAL_RESULTS.md auto-generated; data/eval_history.jsonl append-only audit |
| 11 | OSS readiness + model/data cards + backup | ✅ done | 0009 | LICENSE (Apache-2.0), CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md (Contributor Covenant 2.1), docs/MODEL_CARD.md (per-stage), docs/DATA_CARD.md (corpus), docs/METHODOLOGY.md (public methodology + citations), scripts/backup.sh + restore.sh (tested: 900MB DB → 160MB compressed), GitHub issue/PR templates, SPDX-License-Identifier header on all 68 .py files. Contact email: Sofwathullah.Mohamed@gmail.com. |
| 11.5 | Public-sector entity registry — external-reference trust anchor | ✅ done | 0011 | 25 Maldives entities (presidency, ministries, regulators, commissions, utilities, SOEs); YAML source of truth + JSON twin (parity-tested); `kahzaabu/registry.py` with `entity_for_url`/`is_authoritative`; additive `fact_check_evidence.authoritative_entity_id` column; verifier auto-tags on insert; backfill tagged 48/300 existing evidence rows (presidency=43, foreign=3, elections=1, finance=1) |
| 12 | Reproducibility manifest + observability + audit CLIs | ✅ done | 0010 | `kahzaabu reproducibility <id>` CLI + `/api/reproducibility/{id}.json` endpoint (joins curation run, claims, decomposition Qs, evidence, contradiction pair, ClaimReview, git SHA); `prometheus_client` /metrics endpoint + middleware + helpers for pipeline LLM calls; docs/observability/grafana-dashboard.json (6 panels: req rate, P95 latency, LLM cost, fact-check throughput, stage runs, error rate); `kahzaabu audit` (chi-squared category×year/topic + verdict + Truth-O-Meter + speaker + authoritative-source coverage); `kahzaabu transparency-report --since` (window fact-checks, corrections, LLM spend, methodology git-log); Dockerfile (python:3.11-slim, EMBED_EXTRA build arg); `fact_checks.git_sha_at_publication` column; 22 new tests |

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
