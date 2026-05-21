# ADR 0003 — Canonical claim matching

**Status**: Accepted (2026-05-21)

## Context

Politicians repeat the same claim across speeches, press releases, and tweets. Today, kahzaabu treats each occurrence as an independent `claims` row, so a single talking point repeated 20 times produces 20 claim rows (and, after curation, potentially 20 fact-checks). This makes the corpus look noisier than it is, breaks per-claim analytics, and inflates LLM cost on the verify stage.

Full Fact's automation explicitly cites claim-matching as their pipeline's biggest leverage: dedup speeches → one canonical claim → one fact-check → cited many times. RAGAR sidesteps the issue (single-claim benchmark); AVeriTeC sidesteps it (each claim is annotated independently). For a longitudinal corpus like kahzaabu's, claim matching is essential.

## Decision

Add `canonical_claim_id` to the `claims` table: a self-foreign-key pointing to the FIRST occurrence of a semantically equivalent claim. The first occurrence has `canonical_claim_id = id` (or NULL — see consequences); subsequent occurrences point back.

Matching is a hybrid of two signals:

1. **Sentence embedding similarity.** Compute a vector embedding for each claim (`text-embedding-3-small` or equivalent) and compare against all prior claims in the same `subject_normalized` bucket. Cosine similarity ≥ 0.85 is a candidate.
2. **Entity overlap.** Extract named entities (people, places, projects, numbers) and require ≥ 60% overlap with the candidate.

If both signals match, the new claim links to the canonical record. If embedding matches but entities don't, the LLM is asked to confirm (handles paraphrase with novel entities; e.g., "promised housing in Malé" vs. "promised housing in Hulhumalé" — same shape, different specific).

Fact-checks reference `canonical_claim_id`, not the per-occurrence claim id, so a fact-check has one row but many supporting articles.

## Alternatives considered

- **LLM-only matching.** Too expensive at scale (9,000 × 9,000 pairs even with shortlisting). Rejected — embedding pre-filter cuts the search space by ~95% before any LLM call.
- **Embedding-only.** Suffers on paraphrase that involves swapping key entities. Catches "we will build 5,000 flats" vs "the government is constructing 5,000 flats" but also conflates "5,000 flats in Malé" with "5,000 flats in Hulhumalé". Entity overlap fixes this.
- **Fingerprint-only (current V1 approach).** Word-level hashing on lowercased text. Misses paraphrase entirely. Today this means the corpus is full of near-duplicate claims.

## Consequences

**Positive.**

- One fact-check per claim, with N supporting `articles.id` values via the existing JSON column. Massively cleaner public output.
- Frequency-of-claim becomes a queryable field: "Muizzu repeated this promise 14 times across 9 speeches in 2025" — exactly the kind of pattern PolitiFact and Full Fact surface.
- Verify stage runs ONCE per canonical claim, not N times. The 304 web_evidence rows we have today probably collapse to ~70 distinct canonical claims; the rest are duplicate work.

**Negative.**

- Adds an embedding step to the pipeline. We use `text-embedding-3-small` at $0.02 per 1M tokens (~free at our scale).
- The first-occurrence convention means `canonical_claim_id` always points back, never forward. If we discover later that an earlier claim was actually paraphrasing a yet-earlier one, we need a migration. We accept this — first-occurrence is the canonical disambiguator that audit trails expect.
- Embedding vectors must be stored. We add a `claim_embeddings(claim_id, vector BLOB)` side table. Or we use SQLite extension `sqlite-vec` if available. Default to a side table with `BLOB` to avoid adding a dep.

## Specific schema

```sql
ALTER TABLE claims ADD COLUMN canonical_claim_id INTEGER REFERENCES claims(id);

CREATE TABLE claim_embeddings (
    claim_id INTEGER PRIMARY KEY REFERENCES claims(id),
    vector BLOB NOT NULL,            -- float32 packed
    model TEXT NOT NULL,             -- e.g. "text-embedding-3-small"
    created_at TEXT NOT NULL
);
CREATE INDEX idx_claims_canonical ON claims(canonical_claim_id);
```
