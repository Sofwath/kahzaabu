# Kahzaabu — Architecture

> **STATUS: V2 nearing completion.** Slices 0-9 are shipped. Sections of this document reflect the current code in `kahzaabu/` and `hermes-plugin/`. Slices 10-12 (quality evals, OSS hygiene, reproducibility & observability) are the remaining work tracked in `docs/V2_BUILD_PLAN.md`.

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

Every V2 column documented inline. The schema is checked at test time by `tests/test_readme_schema_drift.py` — if a column listed below doesn't exist in the live DB, the test fails.

### articles
```
PK (id, language)               -- EN ↔ DV pairs share `id`, distinguished by `language`
title, category, body_text,     -- scraped from presidency.gov.mv
body_html, reference,           -- `reference` = source URL when available
published_date, scraped_at,
raw_page_html, paired_id        -- FK to the cross-language sibling
```

### claims  *(extended in Slice 1; V2 columns marked with †)*
```
id PK
article_id, language (FK)
extraction_run_id               -- which extraction pass produced this row
type                            -- numeric_promise | deadline_promise |
                                   numeric_update | credit_claim |
                                   policy_assertion | denial | boast |
                                   comparison_to_predecessor |
                                   no_specific_claims (sentinel)
subject, value, deadline,
actor_credited, quote
polarity †                      -- AFFIRM | DENY | PROMISE |
                                   DENIAL_OF_PROMISE | CLAIM_OF_FACT | NEUTRAL
subject_normalized †            -- entity-resolved canonical subject
is_checkable †                  -- 0 = opinion/ceremonial, 1 = verifiable
canonical_claim_id †            -- FK to itself; the FIRST occurrence is
                                   the canonical record (ADR 0003)
```

### claim_questions  *(Slice 2, AVeriTeC Q&A shape)*
```
id PK
claim_id (FK)
question                        -- decomposed sub-question
answer                          -- NULL until verifier fills it
answer_type                     -- Abstractive | Extractive | Boolean | Unanswerable
source_url, source_medium       -- source_medium ∈ {archive, web_search,
                                                     constitution, manifesto}
confidence
decomposition_run_id, answered_at, created_at
```

### claim_embeddings  *(Slice 3)*
```
claim_id PK
vector BLOB                     -- packed float32 vector
model                           -- e.g. sentence-transformers/all-MiniLM-L6-v2
dim                             -- 384 (Local) / 1536 (OpenAI) / 1024 (Voyage)
created_at
```

### contradiction_pairs  *(Slice 4, the headline)*
```
id PK
claim_a_id, claim_b_id (FKs, sorted at insert time)
subject                         -- subject_normalized bucket
verdict CHECK IN (              -- 4-way verdict, ADR 0004
  'CONTRADICTION',
  'EVOLVING_POSITION',
  'CONTEXT_CHANGED',
  'NOT_CONTRADICTORY')
confidence CHECK (0-1)
reasoning_chain                 -- JSON: [{question, answer, evidence}, ...]
published, reviewed_at, reviewed_by, detected_at
UNIQUE(claim_a_id, claim_b_id)  -- idempotent finder
```

### fact_checks  *(extended Slices 5 + 6)*
```
id PK
category                        -- kahzaabu analytical: LIE / MISLEADING /
                                   BROKEN DEADLINE / CREDIT THEFT /
                                   SHIFTING NUMBERS / CONTRADICTION (+ compounds)
claim_date, claim, what_actually_happened
type, topic, confidence ('auto' | 'reviewed' | 'rejected')
source_article_ids              -- JSON int array → articles.id
evidence_quotes                 -- JSON string array
source, fingerprint, created_at
published, public_summary, reviewed_at, reviewed_by

-- V2 Slice 5 additions:
verdict_label                   -- AVeriTeC: SUPPORTED | REFUTED |
                                   NOT_ENOUGH_EVIDENCE | CONFLICTING_EVIDENCE
truth_score                     -- PolitiFact 1-6 numeric
truth_score_label               -- TRUE | MOSTLY_TRUE | HALF_TRUE |
                                   MOSTLY_FALSE | FALSE | PANTS_ON_FIRE
reasoning_chain                 -- JSON, RAGAR Chain-of-RAG
contradiction_pair_id (FK)      -- when the fact-check came from a pair
speaker DEFAULT 'Mohamed Muizzu'
canonical_url                   -- nullable until public deploy

-- V2 Slice 6 addition:
claimreview_jsonld              -- pre-computed schema.org JSON-LD blob
```

### Side tables (audit, evidence, multilingual, prior)
```
fact_check_evidence       -- web-search rows backing each fact-check
                          -- cols: url, title, snippet, relevance, summary, retrieved_at
article_fact_cards        -- per-article inspector output (V1)
dv_en_inconsistencies     -- EN/DV translation diffs (V1)
manifesto_promises        -- 2023 campaign promises
constitution_articles     -- parsed 2008 Constitution (+ FTS5)
qna_sessions              -- agentic-ask multi-turn memory

-- audit/runs tables (per pipeline stage):
extraction_runs, decomposition_runs, matching_runs,
contradiction_finder_runs, inspection_runs, curation_runs,
verification_runs, dv_compare_runs, manifesto_runs, scrape_runs
```

**Editor protocol**: when editing this section, derive columns from `sqlite3 data/kahzaabu.db ".schema"` and re-run `tests/test_readme_schema_drift.py` (the test treats this file's parallel block in README as authoritative). The README block uses a stricter `-- cols: a, b, c` format because the parser is dumb; this ARCHITECTURE.md block is for human reading.

---

## 5. The contradiction detector (V2 headline feature)

The piece that takes kahzaabu beyond porting existing systems. Every contradiction is machine-checkable: two `claim_id` foreign keys, a JSON reasoning chain, a 4-way verdict (ADR 0004), and a confidence score.

```
┌────────────────────────────────────────────────────────────────────┐
│  STAGE 1 — POLARITY ENRICHMENT  (kahzaabu/claims_enricher.py,      │
│                                  Slice 1 + Slice 4 backfill)       │
│   Haiku 4.5 labels every claim with polarity ∈                     │
│   { AFFIRM, DENY, PROMISE, DENIAL_OF_PROMISE,                      │
│     CLAIM_OF_FACT, NEUTRAL }  +  subject_normalized                │
│   Cost: ~$3 for the full ~9,000-claim corpus.                      │
└────────────────────────────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│  STAGE 2 — POLARITY-PAIR SHORTLIST  (cheap SQL, no LLM)            │
│   For each (subject_normalized) bucket, JOIN claim pairs of        │
│   opposite polarity. Excludes:                                     │
│     - same-day claims (MIN_DAYS_APART = 1)                         │
│     - already-classified pairs (UNIQUE constraint)                 │
│     - claims with is_checkable = 0                                 │
│     - NEUTRAL polarity (never pairs)                               │
│   Live corpus: 96,284 pairs before next stage filter.              │
└────────────────────────────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│  STAGE 3 — SEMANTIC-SIMILARITY FILTER  (uses Slice 3 embeddings)   │
│   Drop pairs whose cosine similarity falls outside                 │
│   [MIN_SIMILARITY=0.55, MAX_SIMILARITY=0.95]:                      │
│     < 0.55  different topics (false positive of polarity shortlist)│
│     > 0.95  paraphrases (Slice 3's canonical_claim_id handled them)│
│   Live corpus: 96,284 → 48 candidates.                             │
└────────────────────────────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│  STAGE 4 — LLM VERDICT  (Sonnet 4.6, ~$0.02/pair)                  │
│   For each candidate, classify into one of 4 verdicts:             │
│     CONTRADICTION       no plausible explanation                   │
│     EVOLVING_POSITION   honest revision, acknowledged              │
│     CONTEXT_CHANGED     external facts shifted (defensible)        │
│     NOT_CONTRADICTORY   polarity-pair false positive               │
│   Returns a JSON reasoning_chain — 2-4 {question, answer,          │
│   evidence} objects with verbatim quotes. Defensible to third      │
│   parties.                                                         │
└────────────────────────────────────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│  STAGE 5 — PERSIST + PROMOTE  (kahzaabu/contradictions.py)         │
│   INSERT OR IGNORE INTO contradiction_pairs ... UNIQUE(a, b).      │
│   Only verdict='CONTRADICTION' rows get promoted to fact_checks    │
│   (Slice 5's promote_contradictions_to_factchecks).                │
│   Other 3 verdicts persist for transparency; queryable at          │
│   /api/contradictions and on the /contradictions web page.         │
└────────────────────────────────────────────────────────────────────┘
```

Live result: **2 CONTRADICTION verdicts on the corpus today** — judicial-interference inconsistency (2023-11-17 ↔ 2025-05-12) and external-debt inconsistency (2024-01-20 ↔ 2026-03-02), both with confidence 0.78 and 4-step reasoning chains. 46 NOT_CONTRADICTORY (polarity-pair false positives the LLM correctly identified).

Conservative bias is the design: bad-faith contradiction inflation undermines the project's credibility. The 4-way verdict distinguishes "I changed my mind after the cyclone" from "I lied" — that distinction is what makes kahzaabu citable.

---

## 6. Verdict & label system

Three labels per fact-check, derived left-to-right (ADR 0005). The derivation lives in `kahzaabu/truth_score.py` — pure, deterministic, **zero LLM cost**.

```
category                  →   verdict_label                →   truth_score + truth_score_label
(kahzaabu domain-analytic)    (AVeriTeC benchmark format)      (PolitiFact public 1-6 rung)

LIE                      →   REFUTED                       →   1 PANTS_ON_FIRE  (if confidence ≥ 0.95 + category = LIE)
CONTRADICTION            →   REFUTED                       →   2 FALSE          (if 0.70 ≤ confidence < 0.95)
BROKEN DEADLINE          →   REFUTED                       →   3 MOSTLY_FALSE   (if confidence < 0.70)
CREDIT THEFT             →   REFUTED                       →
MISLEADING               →   CONFLICTING_EVIDENCE          →   4 HALF_TRUE
SHIFTING NUMBERS         →   CONFLICTING_EVIDENCE          →
(SUPPORTED, confidence ≥ 0.85)                             →   6 TRUE
(SUPPORTED, 0.60 ≤ conf < 0.85)                            →   5 MOSTLY_TRUE
```

Compound categories (`LIE / MISLEADING`, `MISLEADING / CREDIT THEFT`) resolve to the stronger half. `confidence='auto'` → 0.65, `'reviewed'` → 0.90, `'rejected'` → 0.30.

**Why three layers**: each audience needs a different format. PolitiFact-style rungs are recognised by chat readers (Slice 7's Truth-O-Meter badges use them). AVeriTeC verdicts are what researchers comparing to the benchmark expect. `category` retains the analytic granularity used in fact-check curation — `LIE` vs `BROKEN DEADLINE` is more useful than just `REFUTED` when the analyst is choosing remediation language.

The mapping function has 28 unit tests in `tests/test_truth_score.py` covering every category × confidence combination.

---

## 7. ClaimReview JSON-LD export

Every published fact-check carries a pre-computed schema.org `ClaimReview` blob in `fact_checks.claimreview_jsonld`. Served three ways:

1. **`GET /api/factchecks/{id}/jsonld`** — single fact-check, `Content-Type: application/ld+json`, 1-hour public cache.
2. **`GET /api/claimreviews/feed.json`** — aggregate `ItemList` with pagination and `?since=ISO` filter.
3. **Inline `<script type="application/ld+json">`** — landing in Slice 7's per-fact-check page when that ships.

Each blob carries:

```json
{
  "@context": "https://schema.org",
  "@type": "ClaimReview",
  "datePublished": "<created_at>",
  "url": "<canonical_url or KAHZAABU_PUBLIC_BASE_URL/factcheck/{id}>",
  "claimReviewed": "<public_summary or first 700 chars of claim>",
  "author": {
    "@type": "Organization",
    "name": "Kahzaabu",
    "url": "<KAHZAABU_ORG_URL or base>",
    "sameAs": ["<KAHZAABU_ORG_SAMEAS comma-list>"]
  },
  "reviewRating": {
    "@type": "Rating",
    "ratingValue": <truth_score 1-6>,
    "bestRating": 6,
    "worstRating": 1,
    "alternateName": "<humanized truth_score_label e.g. 'Pants On Fire'>",
    "ratingExplanation": "<category> — <verdict_label>"
  },
  "itemReviewed": {
    "@type": "Claim",
    "datePublished": "<claim_date>",
    "author": { "@type": "Person", "name": "Mohamed Muizzu",
                "jobTitle": "President of the Maldives" },
    "appearance": [
      { "@type": "CreativeWork", "url": "<presidency.gov.mv URL or local /article/{id}>" }
    ]
  },
  "disclaimer": "<the automated-analysis disclaimer; ADR 0006 mandate>"
}
```

The `disclaimer` field is non-standard but Google Fact Check Tools accepts arbitrary extra fields. **Every blob carries it; stripping it is a material accuracy violation** — `tests/test_claimreview.py::test_disclaimer_always_present` regression-guards this.

Configuration: `KAHZAABU_PUBLIC_BASE_URL`, `KAHZAABU_ORG_URL`, `KAHZAABU_ORG_SAMEAS` env vars. Defaults work for local development; production deploys override.

Validation: a hand-rolled Google Rich Results checklist (14 invariants) is asserted in `tests/test_claimreview.py`. Once deployed, Google's official Rich Results Test (https://search.google.com/test/rich-results) should pass.

---

## 8. Agent / skill surface

V2 ships three layers of agent integration:

### Plugin tools (`hermes-plugin/tools.py`)

Nine tools registered in the `kahzaabu` toolset; all 9 invocable from `hermes chat` once `./scripts/install-hermes-plugin.sh` runs:

| Tool | Returns |
|---|---|
| `kahzaabu_stats` | Counts + freshness (call first for "recent" questions) |
| `kahzaabu_ask` | **The agentic loop** — 9 internal sub-tools, narrative-tricks layer, citation discipline |
| `kahzaabu_list_lies` | Fact-checks filtered by category / topic / date |
| `kahzaabu_get_factcheck` | Full fact-check incl. evidence + source articles |
| `kahzaabu_manifesto` | 2023 promises by delivery status |
| `kahzaabu_get_article` | Single press release + claims + linked fact-checks |
| `kahzaabu_recent_activity` | Last N days of articles |
| `kahzaabu_constitution_lookup` | FTS5 BM25 search over the parsed Constitution |
| `kahzaabu_pipeline_run` | Trigger a fresh scrape (env-gated) |

### Slash command (`/kahzaabu <question>`)

Available in any hermes chat session including the messaging gateway (Telegram, WhatsApp, Slack, Discord). Auto-continues the most-recent session within 24h. Implemented in `hermes-plugin/__init__.py::_slash_kahzaabu`.

### Installable skill (`skills/kahzaabu-fact-check/SKILL.md`)

agentskills.io-format skill that any external hermes user can `./scripts/install-hermes-skills.sh` to gain a structured fact-check workflow. The skill tells the calling agent to invoke `kahzaabu_ask` and emit a fixed Markdown shape with Truth-O-Meter rating + reasoning steps + sources + the mandatory disclaimer.

```bash
# external user installs everything
git clone <kahzaabu>
./scripts/install-hermes-plugin.sh        # ← the 9 tools + CLI
./scripts/install-hermes-skills.sh        # ← the fact-check skill
hermes skills list | grep kahzaabu         # ← confirms 2 skills enabled

# now any agent can use it:
hermes chat -q "Use kahzaabu-fact-check to verify: <claim>" \
            --skills kahzaabu-fact-check --yolo
```

---

## 8.5. Authoritative external-reference registry (ADR 0011)

The pipeline distinguishes **primary-source** evidence from
**secondary** evidence via an explicit registry of Maldivian
public-sector entities.

Source of truth: `data/registry/maldives_public_sector.yaml` — 25
entities covering the executive (Presidency), 3 ministries, the
legislature (Majlis), the judiciary, 4 independent commissions
(ACC, Elections, CSC, HRCM), 2 regulators (HPA, MFDA), 5 utilities,
1 airport operator (MACL), 1 SOE (STO), plus revenue/customs/
immigration/police and the oneGov/eFaas digital portals.

Lookup module: `kahzaabu/registry.py` provides `entity_for_url()`,
`is_authoritative()`, `entity_by_id()`. Pure stdlib (no pyyaml at
runtime; a JSON twin is shipped alongside the YAML and parity-tested).

Integration: when `claims_db.insert_evidence()` writes a row to
`fact_check_evidence`, it auto-populates the new
`authoritative_entity_id` column if the URL's hostname matches a
registered domain (exact or strict-subdomain match, case-insensitive,
`www.` stripped). Non-registered URLs are still ingested; they simply
remain `NULL` for that column.

Backfill (May 2026): 48 of 300 existing evidence rows tagged.
Breakdown — presidency: 43, foreign: 3, elections: 1, finance: 1.

The registry is an **additive trust signal, not a filter**. Future
Slice 12 transparency-report will aggregate by `entity_type` to
quantify how grounded each fact-check is in primary-source evidence.

---

## 9. What's NOT in V2

See `docs/V2_BUILD_PLAN.md` § Out of scope. Briefly: public VPS deploy, multimodal verification (RAGAR's image extension), native-Dhivehi LLM verification, real-time monitoring.

---

## 10. Reproducibility map

A researcher trying to reproduce kahzaabu's numbers needs to know which paper / standard each piece of the system implements:

| Component | Code module | Paper / standard |
|---|---|---|
| Claim detection | `kahzaabu/extractor.py` | Full Fact AI (BERT classifier; we use Sonnet) |
| Claim matching → canonical_claim_id | `kahzaabu/matcher.py` + `kahzaabu/embeddings.py` | Full Fact AI claim matching |
| Embedding provider abstraction | `kahzaabu/embeddings.py` | (kahzaabu-original; ADR 0007) |
| Q&A decomposition | `kahzaabu/decomposer.py` | AVeriTeC (Schlichtkrull et al. EMNLP 2023) |
| Contradiction detector | `kahzaabu/contradictions.py` | (kahzaabu-original — closest analog: RAGAR Chain-of-RAG flow applied to two opposing claims) |
| Verdict labels | `kahzaabu/truth_score.py::category_to_verdict_label` | AVeriTeC verdicts |
| Truth-O-Meter | `kahzaabu/truth_score.py::derive_truth_score` | PolitiFact methodology |
| Reasoning chain | `kahzaabu/fact_check_enricher.py::_assemble_reasoning_chain` | RAGAR (Khaliq et al. arXiv 2404.12065) Chain-of-RAG |
| ClaimReview JSON-LD | `kahzaabu/claimreview.py` | schema.org/ClaimReview |
| Narrative-tricks layer | `kahzaabu/qna_agentic.py` SYSTEM_PROMPT | (kahzaabu-original) |
| Constitution cross-check | `kahzaabu/constitution.py` + FTS5 | (kahzaabu-original) |
| Public-sector entity registry | `kahzaabu/registry.py` + `data/registry/` | (kahzaabu-original; ADR 0011) |
| Reproducibility manifest | `kahzaabu/reproducibility.py` + `/api/reproducibility/{id}.json` | (kahzaabu-original; ADR 0010) |
| Bias / fairness audit | `kahzaabu/audit.py` + `kahzaabu audit` CLI | (kahzaabu-original; ADR 0010) |
| Transparency report | `kahzaabu/transparency.py` + `kahzaabu transparency-report` CLI | (kahzaabu-original; ADR 0010) |
| Observability metrics | `kahzaabu/web/metrics.py` + `/metrics` | prometheus_client; ADR 0010 |
| One-command reproduction | `Dockerfile` | (kahzaabu-original; ADR 0010) |

ADRs 0001-0011 in `docs/adr/` document every architectural choice with explicit context and alternatives considered. Quality numbers per stage live in `docs/EVAL_RESULTS.md`, auto-generated by `kahzaabu eval`.

---

## 11. Citation

Until a paper / DOI exists:

```bibtex
@software{kahzaabu_2025,
  title = {Kahzaabu: Automated Political Fact-Checking for the Maldives Presidency},
  author = {Chopey, S.},
  year = {2025},
  url = {https://github.com/<TBD>},
  note = {Open-source civic-tech project. Apache-2.0 licensed.}
}
```

The architecture is portable: same pipeline could fact-check any executive office's press release archive. Only the corpus (`data/kahzaabu.db`) and the constitution (`data/constitution/`) are Maldives-specific.
