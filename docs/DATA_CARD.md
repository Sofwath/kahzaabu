# Data Card — kahzaabu corpus

> **Reference implementation.** Kahzaabu is a sample Hermes Agent
> plugin and fact-checking pipeline built for educational and research
> purposes. The corpus described below is a research dataset
> *derived from* the public press release archive at
> `presidency.gov.mv`; LLM-derived annotations (claims, verdicts,
> Truth-O-Meter scores) are automated analysis, not authoritative
> facts. Researchers may use the corpus for replication, methodology
> study, and adaptation to other small-state political corpora; the
> annotations are not citable as findings of fact. Full terms in
> [`../DISCLAIMER.md`](../DISCLAIMER.md).

Following the Data Cards framework (Pushkarna, Zaldivar, Kjartansson —
FAccT 2022, [arXiv 2204.01075](https://arxiv.org/abs/2204.01075)).

## Summary

The kahzaabu corpus is an automated, append-only archive of press
releases published on `presidency.gov.mv` (Office of the President of
the Republic of Maldives), enriched with LLM-derived structured
annotations (claims, decompositions, embeddings, contradiction pairs,
fact-checks, ClaimReview JSON-LD).

| Item | Value |
|---|---|
| Card revision | 2026-05-21 |
| Schema version | V2 (Slices 0–10 land) |
| License (this card + the LLM-derived annotations) | Apache-2.0 |
| Source articles license | Public domain — Government of Maldives press releases |
| Sample size (English) | 14,124 articles |
| Sample size (Dhivehi) | 6,686 articles |
| Time coverage | 2008-12 onwards (broader corpus); Muizzu administration claims focus: **2023-11-17 → present** |
| Update cadence | Every 12h via launchd (`scripts/com.kahzaabu.pipeline.plist`) |
| Total uncompressed size | ~900 MB SQLite database |

## Collection methodology

**Source.** All articles are scraped from `presidency.gov.mv/news/`
categories `press_release`, `speech`, `vp_speech`. The scraper
(`kahzaabu/scraper.py`) is incremental and idempotent — re-runs do not
duplicate.

**Language pairing.** EN and DV articles share a `paired_id` when a
press release was published in both languages on the same day. 2,648
pairs in the current corpus support EN/DV translation-consistency
checks (`dv_en_inconsistencies` table).

**Pipeline-derived data.** Most fields in the database are derivative
of the source HTML, produced by deterministic code or LLM call sites
described in [`docs/MODEL_CARD.md`](MODEL_CARD.md). The pipeline writes
audit-log rows (`scrape_runs`, `extraction_runs`, `decomposition_runs`,
`matching_runs`, `contradiction_finder_runs`) so every derived field
is traceable to a run, a model version, and a cost.

## Known gaps

- **Pre-2023-11-17 Muizzu era**: he was a private citizen / municipal
  mayor; press releases from his presidency only begin at inauguration.
- **Articles published before December 2008**: the upstream site does
  not publish that far back.
- **Removed/edited articles**: when the upstream site edits or removes
  a press release, kahzaabu may have a stale snapshot. We attempt no
  re-fetch on the principle that a published statement should remain
  inspectable even after retraction; the scraper logs (`scrape_runs`)
  record any 404s.
- **Speeches, formal interviews**: covered. **Off-the-cuff remarks at
  press conferences, rallies, Twitter/X posts**: not in scope.
- **Opposition or third-party statements**: not in scope. The corpus
  is government-output-only.
- **Dhivehi-source-only content**: scraped (DV pages exist) but
  LLM-extraction is English-only at present. The `dv_compare.py` stage
  surfaces translation discrepancies but does not extract DV-only
  claims into the structured tables.

## Sensitive content

- **Subject is a sitting head of state.** Outputs are constrained to
  patterns from publicly-published statements; we do not infer
  intent, motive, or psychology. All fact-checks include a disclaimer
  pointing to the original article.
- **No PII**: the corpus contains officials' names (already public)
  but no private citizens' personal data beyond what the source site
  itself publishes.
- **No image data**: HTML and text only. Photos linked from press
  releases are not scraped.

## External-reference registry (ADR 0011)

The corpus includes an explicit registry of authoritative external
references — the Maldives public-sector entities whose domains count
as primary sources when cited as evidence.

- Source of truth: `data/registry/maldives_public_sector.yaml` (human-
  editable, the format used for community contribution).
- Machine twin: `data/registry/maldives_public_sector.json` (loaded by
  `kahzaabu/registry.py`; YAML↔JSON parity-tested).
- Coverage: 25 entities — the President's Office, 3 ministries, the
  legislature (Majlis), the judiciary, 4 independent commissions
  (ACC, Elections, CSC, HRCM), 2 regulators (HPA, MFDA), 5 utilities
  (STELCO, Fenaka, MWSC, HDC, MTCC), 1 airport operator (MACL), 1
  SOE (STO), plus revenue/customs/immigration/police agencies and
  oneGov/eFaas digital-service portals.
- Used by: `verifier.py` auto-tags
  `fact_check_evidence.authoritative_entity_id` on insert; future
  Slice 12 transparency-report aggregates by entity_type.
- Extension policy: contributors edit the YAML; the JSON twin must be
  regenerated and the parity test must pass.

## Schema

The canonical schema is in `kahzaabu/claims_db.py` and rendered in
the README's "Data model" section (drift-tested by
`tests/test_readme_schema_drift.py`).

V2 additions over the V1 baseline (per ADRs 0002–0006, 0011):

- `claims` gains `polarity`, `subject_normalized`, `is_checkable`,
  `canonical_claim_id`.
- New tables: `claim_questions`, `decomposition_runs`,
  `claim_embeddings`, `matching_runs`, `contradiction_pairs`,
  `contradiction_finder_runs`.
- `fact_checks` gains `verdict_label`, `truth_score`,
  `truth_score_label`, `reasoning_chain`, `contradiction_pair_id`,
  `speaker`, `canonical_url`, `claimreview_jsonld`.
- `fact_check_evidence` gains `authoritative_entity_id` (nullable
  pointer to registry `entity_id` — ADR 0011).

Migrations are idempotent ALTER-COLUMN style. WAL mode is on. The
schema is single-tenant; no multi-customer separation.

## Provenance & reproducibility

- Each article carries `reference` (canonical URL on
  `presidency.gov.mv`) and `scraped_at`. The full `raw_page_html`
  is retained for re-extraction.
- Each LLM-derived row carries the originating run-id (e.g.
  `extraction_run_id`, `decomposition_run_id`,
  `inspection_run_id`). The run-id table records the model, prompt
  version, started/finished timestamps, total cost.
- Re-running any pipeline stage is idempotent: existing rows are
  skipped or replaced, never duplicated. See ADR 0010 (in flight)
  for the planned `/api/reproducibility.json` endpoint that exposes
  this provenance over HTTP.

## Quality

See [`docs/EVAL_RESULTS.md`](EVAL_RESULTS.md) (auto-generated by
`kahzaabu eval`). Each LLM-call stage has a golden set under
`tests/golden/<stage>/`. Per [ADR 0008](adr/0008-quality-evaluation.md),
golden fixtures are tagged `verified` (hand-confirmed ground truth)
or pinned (drift detector for prompt changes). Current verified-subset
counts:

| Stage | Verified fixtures | Verified-subset metric |
|---|---|---|
| truth_score | 6 / 6 | exact-match 1.000 |
| matcher | 6 / 6 | macro-F1 1.000 |
| contradictions | 5 / 5 | macro-F1 1.000 |
| decomposer | 4 / 4 | F1 1.000 |
| extractor | 3 / 4 | F1 1.000 |

Growing the verified subset is an ongoing data-labelling task open to
contributors (see [`CONTRIBUTING.md`](../CONTRIBUTING.md)).

## Retention & deletion

- The DB is backed up nightly via `scripts/backup.sh` (gzipped
  `sqlite3 .dump` to `data/backups/`). Default retention: 30 days
  local. Off-machine sync is the operator's responsibility.
- The corpus has no individual-deletion API; takedowns of any
  Maldives-published article should be requested upstream at
  `presidency.gov.mv`. Once removed there, the next scrape cycle will
  record the 404 but kahzaabu does not auto-purge — the principle is
  that published government statements remain inspectable.
- Contributors with a justified deletion request (e.g. wrong personal
  attribution in an LLM-derived row) can email
  `Sofwathullah.Mohamed@gmail.com`.

## Recommended uses

- Civic-tech research on government communication patterns.
- Academic study of automated fact-checking on a small-state corpus.
- Building / testing claim-matching, decomposition, or
  contradiction-detection methods against a real (non-Wikipedia)
  corpus.
- Comparing AVeriTeC / RAGAR / Full Fact methodologies on a non-US,
  non-EU-centric dataset.

## Not recommended

- Single-claim journalistic verdicts without human verification.
- Decisions about individuals (employment, prosecution, eligibility).
- Training language models. The corpus is small, partisan-skewed,
  and not balanced enough for training; it may bias an LLM's
  political voice.

## Maintainer

Sofwathullah Mohamed (`Sofwathullah.Mohamed@gmail.com`).

For corrections: open an issue using the bug-report template, or use
the public web UI's "Report a correction" form.
