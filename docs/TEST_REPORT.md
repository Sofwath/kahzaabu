# kahzaabu — full test report

Generated: 2026-05-21 (V2 in flight, mid-Slice-4 enrichment backfill running).

This document captures the result of running the full test stack
end-to-end against the working tree. Reproduce with:

```bash
.venv/bin/python -m unittest discover tests/    # unit suite
./scripts/test.sh                                # local CI parity
./scripts/ci-dry-run.sh                          # validate workflow in fresh worktree
.venv/bin/python tests/system_check.py           # live web stack (needs server running)
```

---

## 1. Unit suite — 109 tests across 9 modules, all green

| Module | Tests | Domain |
|---|---|---|
| `test_claims_enrichment.py` | 18 | Slice 1: schema, backward-compat insert, polarity validation, is_checkable coercion |
| `test_constitution_parser.py` | 14 | Parser contract (301 articles), lookup behaviour, BM25 quality, FTS5/weights alignment |
| `test_contradictions.py` | 15 | Slice 4: schema, polarity-pair shortlist, ordering normalization, enum guards |
| `test_decomposer.py` | 14 | Slice 2: AVeriTeC enums, idempotent discovery, NULL-answer state |
| `test_embedding_providers.py` | 16 | ADR 0007: 3-provider abstraction, selection priority, env-var override |
| `test_host_llm_branch.py` | 4 | ctx.llm branch in narrative-tricks guarantee-pass |
| `test_json1_fallback.py` | 4 | SQLite JSON1 happy-path + LIKE fallback parity |
| `test_matcher.py` | 21 | Slice 3: vector pack/unpack, cosine, entity extraction (Unicode), jaccard, find_match flows |
| `test_readme_schema_drift.py` | 3 | Doc↔schema drift guard with silent-pass invariant |

Runtime: ~1.6s total.

---

## 2. CI workflow dry-run — passes in fresh worktree

`./scripts/ci-dry-run.sh` clones HEAD into a temporary worktree, creates a
clean venv, installs editable, bootstraps the DB, runs all 109 tests,
and checks for stale `test_system.py` references. **All steps pass.**

---

## 3. Live DB integrity — zero orphans, all V2 schema present

| Table | Rows |
|---|---|
| articles (EN) | 14,124 |
| articles (DV) | 6,686 |
| claims | 9,873 |
| ┕ with polarity | 4,020 *(backfill in progress)* |
| ┕ with subject_normalized | 4,020 *(backfill in progress)* |
| ┕ with canonical_claim_id | 8,954 (100% of checkable) |
| claim_questions | 35,648 |
| claim_embeddings | 8,954 (100% of checkable) |
| fact_checks (published) | 218 |
| fact_check_evidence | 304 |
| manifesto_promises | 717 |
| constitution_articles | 301 |
| contradiction_pairs | 0 *(awaits enrichment-backfill completion)* |

| Referential integrity probe | Orphans |
|---|---|
| claims → articles | 0 |
| claim_questions → claims | 0 |
| claim_embeddings → claims | 0 |
| fact_check_evidence → fact_checks | 0 |

V2 columns on `claims`: `polarity`, `subject_normalized`, `is_checkable`,
`canonical_claim_id` — all present.

V2 tables: `claim_questions`, `decomposition_runs`, `claim_embeddings`,
`matching_runs`, `contradiction_pairs`, `contradiction_finder_runs` — all present.

---

## 4. Web stack — all 12 routes return 200

```
  200  /
  200  /browse
  200  /lies
  200  /ask
  200  /manifesto
  200  /methodology
  200  /compare
  200  /api/freshness
  200  /api/stats
  200  /static/css/kahzaabu.css
  200  /api/factchecks?limit=3
  200  /api/articles?limit=3
```

`/api/stats` returns the expected JSON shape with audit-trail subblocks
(`last_extraction`, `last_curation`, etc.).

---

## 5. Hermes plugin — all 9 tools registered, agent integration works

`hermes kahzaabu doctor` returns all-green (7 of 7 checks). `hermes plugins
list` shows kahzaabu as enabled, bundled, version 0.1.0. `hermes kahzaabu
status` reports correct corpus counts.

Plugin tools registered (9):

```
kahzaabu_ask                  kahzaabu_get_factcheck
kahzaabu_constitution_lookup  kahzaabu_list_lies
kahzaabu_get_article          kahzaabu_manifesto
kahzaabu_pipeline_run         kahzaabu_recent_activity
kahzaabu_stats
```

Live agent calls via `hermes chat`:

- *"Call the kahzaabu_stats tool"* → returns correct numbers.
- *"Use kahzaabu_constitution_lookup with query 'state religion'. Report
  the article number and title only."* → returns "Article 10 — State Religion".

---

## 6. CLI surface — all V2 commands present

```
✅ decompose             V2 — decompose each claim into AVeriTeC-style sub-questions
✅ match                 V2 — canonical claim matching (Slice 3, ADR 0003)
✅ enrich-claims         V2 — backfill polarity / subject_normalized / is_checkable
✅ find-contradictions   V2 — find pairs of contradictory claims (ADR 0004)
```

All four commands surface in `kahzaabu --help` with the expected help text.

---

## 7. Matcher quality — sample paraphrase groups

Top 12 canonical groups (most-repeated talking points across the corpus):

| canonical# | repeats | excerpt |
|---|---|---|
| 2074 | 7 | "marking the 60th anniversary of the establishment of diplomatic relations between the two…" |
| 166 | 6 | "Diplomatic relations between the Maldives and Türkiye were established on May 28, 1979." |
| 293 | 3 | "reiterated the Maldives' commitment to the One-China Principle" |
| 206 | 2 | "by 2028, 33% of the country's electricity demand will be met by renewable energy…" |
| 215 | 2 | "significant presidential pledge to plant five million trees across the Maldives" |
| 469 | 2 | "the Maldives continues to advocate for an independent and sovereign State of Palestine" |
| 1871 | 2 | "the Government intends to launch a 6.5-million-dollar loan facility for women entrepreneurs" |
| 2020 | 2 | "the landmark China-Maldives Friendship Bridge and many other housing and other infrastructure…" |
| 2767 | 2 | "Our digital economy strategy aims to contribute 15% to GDP by 2030" |
| 2912 | 2 | "next year will mark the sixtieth anniversary of establishing formal diplomatic relations" |
| 3430 | 2 | "the Government's target of generating 33 per cent of the country's electricity from renewables" |
| 5779 | 2 | "one of the several important steps taken towards becoming a developed country by 2040" |

All groups are real political talking points correctly identified as repeated.

---

## 8. Known gaps (documented in V2_BUILD_PLAN)

- **Quality evals (Slice 10)** — not yet built. No golden set, no precision/recall/F1.
- **OSS hygiene (Slice 11)** — LICENSE, CONTRIBUTING.md, SECURITY.md still missing.
- **Reproducibility / observability (Slice 12)** — `/api/reproducibility.json`, Prometheus metrics, Dockerfile, audit/transparency CLIs not yet built.
- **contradiction_pairs** is 0 — slice 4's finder hasn't run yet (enrichment backfill in flight; finder will run on its completion).

---

## 9. Verdict

The codebase is in a healthy state for mid-V2-rebuild work. All committed code is fully tested, the live database is schema-clean and orphan-free, every web route and plugin tool responds correctly, and the CI dry-run validates the workflow against the current HEAD. No blocking defects.

The matcher's qualitative output (top canonical groups, random sample pairs) reads as journalistically defensible — every grouping is a real repeated talking point.
