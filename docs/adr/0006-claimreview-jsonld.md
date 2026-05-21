# ADR 0006 — ClaimReview JSON-LD export

**Status**: Accepted (2026-05-21)

## Context

Google Fact Check Explorer, Bing's fact-check surfacing, and similar discovery tools index ClaimReview-marked-up pages from public publishers. Full Fact, PolitiFact, FactCheck.org, Africa Check, and every other major fact-checking organization emits `<script type="application/ld+json">{...}</script>` blocks with schema.org `ClaimReview` data per fact-check article. Kahzaabu emits nothing today; even when we deploy publicly, the site won't be discoverable as a fact-checking source.

ClaimReview itself is well-specified (schema.org/ClaimReview) and trivial to emit. The non-obvious choices are: which fields to populate (none are strictly required, but indexers expect a minimum set), how to compute `reviewRating`, what `itemReviewed.author` should be, and how to handle the disclaimer that kahzaabu's output is automated analysis.

## Decision

Every published fact-check generates a ClaimReview JSON-LD blob, stored in `fact_checks.claimreview_jsonld` (so it's cacheable) and served:

1. As `<script type="application/ld+json">{...}</script>` in the `<head>` of the per-fact-check page (`/factcheck/{id}`).
2. As a separate endpoint `GET /api/factchecks/{id}/jsonld` returning the bare JSON.
3. As an aggregate sitemap-style endpoint `GET /api/claimreviews/feed.json` for bulk indexing.

The blob populates these fields:

```json
{
  "@context": "https://schema.org",
  "@type": "ClaimReview",
  "datePublished": "2025-03-15",
  "url": "https://kahzaabu.example/factcheck/87",
  "claimReviewed": "{the human-readable claim being checked}",
  "author": {
    "@type": "Organization",
    "name": "Kahzaabu",
    "url": "https://kahzaabu.example",
    "sameAs": ["https://github.com/...", "..."]
  },
  "reviewRating": {
    "@type": "Rating",
    "ratingValue": <truth_score 1-6>,
    "bestRating": 6,
    "worstRating": 1,
    "alternateName": "<truth_score_label>",
    "ratingExplanation": "<one-line explanation>"
  },
  "itemReviewed": {
    "@type": "Claim",
    "datePublished": "<claim_date>",
    "author": {
      "@type": "Person",
      "name": "Mohamed Muizzu",
      "jobTitle": "President of the Maldives"
    },
    "appearance": [
      {"@type": "CreativeWork", "url": "<presidency.gov.mv URL #1>"},
      ...
    ]
  },
  "disclaimer": "This fact-check is the output of an automated analysis pipeline. The categorical verdict and 1-6 truth score are derived deterministically from extracted evidence; the underlying claim is verified against the official press release archive and (where applicable) Anthropic's web_search tool. Constitutional citations use the 2008 Dheena Hussain functional translation; the legally binding text is the Dhivehi original."
}
```

The `disclaimer` field is non-standard (schema.org doesn't define it) but Google Fact Check Tools accepts arbitrary extra fields. The disclaimer is mandatory per ADR 0001's "defensible at every step" requirement — readers must know this is automated.

## Alternatives considered

- **Inline JSON-LD only (no separate endpoint).** Rejected — researchers and downstream automation want JSON, not HTML scraping.
- **Emit ClaimReview for ALL fact-checks, including unpublished.** Rejected — unpublished items are under review; emitting them would imply editorial publication. Only `published=1` items get JSON-LD.
- **Skip `itemReviewed.author` (treat the claim as authorless).** Rejected — every claim has a speaker, and search indexers use the speaker to disambiguate. We declare Muizzu (or whoever made the claim) explicitly.
- **Use `Claim` `@type` vs `CreativeWork`.** schema.org defines `Claim` as a subtype of `CreativeWork`. We use `Claim` to be explicit, which is what AVeriTeC's reference systems do.

## Consequences

**Positive.**

- Once deployed, kahzaabu is indexable by Google Fact Check Explorer and surfaces in search results.
- The JSON-LD is a machine-readable export of the entire fact-check, suitable for academic citation and downstream tools.
- The `claimreview_jsonld` column is regenerated whenever a fact-check is updated (via a trigger or an explicit `regenerate-jsonld` CLI command).

**Negative.**

- We have to commit to a stable URL structure (`/factcheck/{id}`) that won't break when indexed.
- Speaker attribution requires the corpus to know each speaker. Currently all 218 fact-checks point to Muizzu (or implicitly to "the government"); we add an explicit `fact_checks.speaker` column.
- The disclaimer text becomes a kahzaabu-wide stable string. Editing it changes the JSON-LD for every fact-check; we treat the disclaimer as an ADR-worthy decision.

## Schema additions

```sql
ALTER TABLE fact_checks ADD COLUMN speaker TEXT DEFAULT 'Mohamed Muizzu';
ALTER TABLE fact_checks ADD COLUMN claimreview_jsonld TEXT;
ALTER TABLE fact_checks ADD COLUMN canonical_url TEXT;
    -- stable URL once a public site exists; nullable until then.
```
