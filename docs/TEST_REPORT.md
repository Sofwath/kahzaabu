# kahzaabu — full test report

Generated: 2026-05-21 (V2 slices 0–12 done; password-based admin auth removed).

This document captures the result of running the full test stack
end-to-end against the working tree. Reproduce with:

```bash
.venv/bin/python -m unittest discover tests/    # unit suite
./scripts/test.sh                                # local CI parity
./scripts/ci-dry-run.sh                          # validate workflow in fresh worktree
.venv/bin/kahzaabu eval                          # golden-set quality eval (ADR 0008)
# JS/UI verification of vendored libs:
cd scripts/js-verify && npm run verify
```

---

## 1. Unit suite — 327 tests across 20 modules, all green

| Module | Tests | Domain |
|---|---|---|
| `test_constitution_api.py` | 32 | Constitution browser + per-fact-check page + cache headers + Laws link-out + JS-shadow guards + page-data-load smokes + no-CDN-script guard |
| `test_slice12.py` | 30 | Reproducibility manifest + audit + transparency + metrics decorator + schema init full |
| `test_truth_score.py` | 27 | Deterministic AVeriTeC + Truth-O-Meter mapping (ADR 0005) |
| `test_eval.py` | 26 | Golden-set framework — Jaccard F1, classification metrics, verified-vs-pinned fixtures, per-stage runners |
| `test_matcher.py` | 21 | Slice 3: vector pack/unpack, cosine, entity extraction, jaccard, find_match flows |
| `test_registry.py` | 21 | ADR 0011: registry shape, URL matching, YAML↔JSON parity, schema migration |
| `test_hermes_plugin.py` | 19 | Plugin manifest↔code parity, 9-tool surface, error contract, pipeline gate, discovery |
| `test_claims_enrichment.py` | 18 | Slice 1: schema, polarity validation, is_checkable coercion |
| `test_contradictions.py` | 17 | Slice 4: schema, polarity-pair shortlist, 4-way verdict enum guards |
| `test_pricing.py` | 17 | Centralised pricing: frozen Model dataclass, registry parity, cost helper, no-stage-redeclares guard |
| `test_claimreview.py` | 16 | Slice 6: schema.org JSON-LD shape, Truth-O-Meter rendering, disclaimer always present |
| `test_embedding_providers.py` | 16 | ADR 0007: 3-provider abstraction, selection priority, env-var override |
| `test_decomposer.py` | 14 | Slice 2: AVeriTeC enums, idempotent discovery, NULL-answer state |
| `test_constitution_parser.py` | 14 | Parser contract (301 articles), lookup, BM25 quality, FTS5/weights alignment |
| `test_fact_check_enricher.py` | 12 | Slice 5: V2-label backfill, reasoning_chain assembly, contradiction-pair promotion |
| `test_secrets_hygiene.py` | 8 | Credential/PII guards + no-auth-surface posture (modules absent, deps absent, no /login or /admin routes) |
| `test_contradictions_api.py` | 8 | Slice 7: GET /api/contradictions list + detail |
| `test_host_llm_branch.py` | 4 | `ctx.llm` branch in narrative-tricks guarantee-pass |
| `test_json1_fallback.py` | 4 | SQLite JSON1 happy-path + LIKE fallback parity |
| `test_readme_schema_drift.py` | 3 | Doc↔schema drift guard with silent-pass invariant |

Runtime: ~2.6s total.

---

## 2. CI workflow — passes in fresh worktree

`./scripts/ci-dry-run.sh` clones HEAD into a temporary worktree, creates a
clean venv, installs editable, bootstraps the DB, runs all 327 tests,
and checks for stale references to the old end-to-end-test filename.
**All steps pass.**

`.github/workflows/test.yml` runs the suite on every push and PR;
`.github/workflows/external-links.yml` probes mvlaw.gov.mv tile URLs
weekly (Mondays 02:00 UTC). The `js-verify` job runs Node 20 against
`scripts/js-verify/` and exercises the vendored Chart.js + marked
libraries against kahzaabu's actual call sites.

---

## 3. Live DB integrity — V2 schema fully populated, zero orphans

| Table | Rows |
|---|---|
| articles (EN) | 14,124 |
| articles (DV) | 6,686 |
| claims | 9,873 |
| ┕ with polarity | 8,954 (100% of checkable) |
| ┕ with subject_normalized | 8,954 |
| ┕ with canonical_claim_id | 8,954 |
| claim_questions | 35,648 |
| claim_embeddings | 8,954 |
| fact_checks (published) | 218 |
| fact_checks (V2-enriched) | 220 / 220 with `verdict_label`, `truth_score`, `truth_score_label` |
| fact_checks (with ClaimReview JSON-LD cached) | 218 |
| fact_check_evidence | 304 |
| evidence authoritative (ADR 0011) | 48 |
| contradiction_pairs | 48 (**2 CONTRADICTION**, 46 NOT_CONTRADICTORY) |
| manifesto_promises | 717 |
| constitution_articles | 301 |
| web_users | 0 (table preserved for backwards compat; auth removed) |

| Referential integrity probe | Orphans |
|---|---|
| claims → articles | 0 |
| claim_questions → claims | 0 |
| claim_embeddings → claims | 0 |
| fact_check_evidence → fact_checks | 0 |
| contradiction_pairs.claim_a / claim_b → claims | 0 |
| fact_checks.contradiction_pair_id → contradiction_pairs | 0 |

V2 columns on `claims`: `polarity`, `subject_normalized`, `is_checkable`,
`canonical_claim_id` — all present.

V2 columns on `fact_checks`: `verdict_label`, `truth_score`,
`truth_score_label`, `reasoning_chain`, `contradiction_pair_id`,
`speaker`, `canonical_url`, `claimreview_jsonld`,
`git_sha_at_publication` — all present.

V2 columns on `fact_check_evidence`: `authoritative_entity_id` (ADR 0011)
— present, indexed.

V2 tables: `claim_questions`, `decomposition_runs`, `claim_embeddings`,
`matching_runs`, `contradiction_pairs`, `contradiction_finder_runs` —
all present.

---

## 4. Web stack — all read-only public routes return 200

```
  200  /
  200  /browse
  200  /lies
  200  /factcheck/{id}            ← Truth-O-Meter centerpiece + provenance
  200  /constitution              ← 301-article BM25 browser
  200  /laws                      ← link-out to mvlaw.gov.mv (ADR 0012)
  200  /contradictions
  200  /compare
  200  /manifesto
  200  /ask
  200  /methodology
  200  /corrections
  200  /api/stats
  200  /api/factchecks?limit=N    ← always published=1
  200  /api/articles?limit=N
  200  /api/contradictions
  200  /api/contradictions/{id}
  200  /api/constitution/articles
  200  /api/constitution/search?q=…
  200  /api/constitution/{n}
  200  /api/viz/truth-score-ladder
  200  /api/reproducibility/{id}.json
  200  /api/factchecks/{id}/jsonld
  200  /api/claimreviews/feed.json
  200  /metrics                   ← Prometheus exposition
```

**Removed routes — all return 404 by design** (no in-app auth):
`/login`, `/admin`, `/admin/queue`, `/admin/run`, `/api/login`,
`/api/admin/*`, `/api/me`. The web UI is read-only public;
operator actions run from the shell via the `kahzaabu` CLI.

---

## 5. Hermes plugin — 9 tools registered, plugin tests cover the surface

`hermes kahzaabu doctor` returns all-green. `hermes plugins list` shows
kahzaabu v0.2.0 as enabled, bundled. The plugin's own test file
(`tests/test_hermes_plugin.py`) verifies manifest↔code parity, error-
envelope consistency, the legacy/new pipeline-gate env-var
backwards-compat, and discovery via `kahzaabu_home()`.

Tools registered (9): `kahzaabu_stats`, `kahzaabu_ask`,
`kahzaabu_list_lies`, `kahzaabu_get_factcheck`, `kahzaabu_manifesto`,
`kahzaabu_get_article`, `kahzaabu_recent_activity`,
`kahzaabu_constitution_lookup`, `kahzaabu_pipeline_run`.

Plus the `/kahzaabu` slash command (works in any hermes chat session
including messaging gateway) and the `hermes kahzaabu` CLI subcommand
(setup, status, doctor, web, update, ask).

The `kahzaabu-fact-check` agentskills.io skill (Slice 8) is installable
via `scripts/install-hermes-skills.sh` and produces a structured
verdict + Truth-O-Meter + reasoning chain + sources for arbitrary
claim strings.

---

## 6. CLI surface — V1 + V2 + audit/transparency, no user-management

```
✅ pipeline               V1 — orchestrates scrape → extract → … → dv-compare
✅ extract                V1 — per-article claim extraction
✅ inspect                V1 — per-article fact card
✅ curate                 V1 — cross-time contradiction detector
✅ verify                 V1 — web-search verification
✅ dv-compare             V1 — EN/DV translation diff
✅ ask                    V1 — agentic Q&A loop
✅ tui                    V1 — interactive TUI
✅ web                    V1 — FastAPI server

✅ decompose              V2 — AVeriTeC Q&A decomposition (Slice 2)
✅ match                  V2 — canonical claim matching (Slice 3, ADR 0003)
✅ enrich-claims          V2 — backfill polarity / subject_normalized / is_checkable
✅ find-contradictions    V2 — 4-way verdict classifier (Slice 4, ADR 0004)
✅ enrich-factchecks      V2 — V2-label backfill (Slice 5, ADR 0005)
✅ export-claimreview     V2 — schema.org JSON-LD generation (Slice 6, ADR 0006)
✅ eval                   V2 — golden-set quality eval (Slice 10, ADR 0008)
✅ reproducibility <id>   V2 — provenance manifest JSON (Slice 12, ADR 0010)
✅ audit                  V2 — bias/fairness chi-squared report
✅ transparency-report    V2 — window-scoped public report

✅ publish <id>           Toggle fact_checks.published — operator-only
                          (this is now the SOLE publishing path; the
                           login-gated web admin queue was removed)
```

`create-user` and `set-password` no longer exist. There are no
in-app credentials anywhere.

---

## 7. Quality evaluation — `kahzaabu eval`

Auto-generated report: [`docs/EVAL_RESULTS.md`](EVAL_RESULTS.md).

Per-stage metrics against the golden set under `tests/golden/`.

| Stage | Fixtures | Verified | Verified-subset | Drift detector |
|---|---|---|---|---|
| truth_score | 6 | 6/6 | acc=1.000 macro_f1=1.000 | acc=1.000 |
| extractor | 4 | 3/4 | P=1.000 R=1.000 F1=1.000 | F1=1.000 |
| decomposer | 4 | 4/4 | P=1.000 R=1.000 F1=1.000 | F1=1.000 |
| matcher | 6 | 6/6 | acc=1.000 macro_f1=1.000 | acc=1.000 |
| contradictions | 5 | 5/5 | acc=1.000 macro_f1=1.000 | acc=1.000 |
| verifier | 8 | 0/8 | (pinned baselines) | F1=1.000 |

33 fixtures total; 24 verified ground truth; 9 pinned (8 verifier +
1 extractor) — promotion is operator-review work.

---

## 8. Matcher quality — sample paraphrase groups

Top canonical groups (most-repeated talking points across the corpus):

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

All groups are real political talking points correctly identified as repeated.

---

## 9. Contradictions found — Slice 4 headline output

Live polarity-pair shortlist + semantic-similarity filter [0.55, 0.95] +
Sonnet 4.6 classifier produced **48 candidate pairs**, of which **2 were
classified CONTRADICTION** (both promoted to `fact_checks`):

1. **Judicial interference** — Nov 2023: "President's Office interfering
   in the judiciary ends here right now" vs May 2025: "neither he nor
   the President has interfered with the judiciary's work."
2. **External debt** — Jan 2024: "high debt burdens... challenge to
   refinance our debt obligations" vs Mar 2026: "the Maldives faces no
   concerns regarding the repayment of its external debt."

46 candidates were classified NOT_CONTRADICTORY (compatible stances on
different topics, or evolving positions with explicit context). Verdict
distribution validated via `tests/golden/contradictions/`.

---

## 10. Security posture

- **Zero in-app credentials.** No passwords, no sessions, no admin users,
  no `/login` or `/admin` routes. The web UI is read-only public.
  Operator actions go through the `kahzaabu` CLI on the operator's
  shell, gated by OS-level permissions.
- **No live-shape credentials in any tracked file.** Verified by
  `test_secrets_hygiene.py::SecretShapeGuardTests` against 7 credential
  patterns (sk-ant-, sk-, pa-, AKIA, AIza, ghp_, xox[abprs]-).
- **No hardcoded developer-machine paths in active code.** Verified by
  `DeveloperPathGuardTests` against `/Users/<name>/...` and
  `/home/<name>/...` patterns.
- **`data/kahzaabu.db` correctly gitignored.** Never committed.
- **No CDN dependencies at runtime.** Chart.js + marked vendored under
  `kahzaabu/web/static/js/` with `NOTICE.md` attribution + lockfile-
  pinned verifier.

---

## 11. Verdict

The codebase is in publication-ready state. All 327 tests green,
live DB schema-clean + orphan-free, every read-only web route
returns 200, every removed auth/admin route returns 404. The
maintenance triangle (drift detection / upgrade recipe / call-site
verification / reproducibility / link-rot / discoverability /
cadence) is closed across the project, and CI enforces the most
important gates on every PR.

Open follow-ups are documented in `docs/V2_BUILD_PLAN.md` and the
README TODO table — primarily public VPS deploy + growing the
verified golden-fixture subset for the verifier stage.
