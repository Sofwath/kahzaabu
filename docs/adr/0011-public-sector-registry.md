# ADR 0011 — Public-sector entity registry as external-reference trust anchor

**Status**: Accepted (2026-05-21)

## Context

Kahzaabu's fact-check pipeline ingests two kinds of external references:

1. **Source articles** — press releases on `presidency.gov.mv`. This is
   the primary corpus; all claims originate here. Already a known surface.
2. **Verifier evidence** — `kahzaabu/verifier.py` does Anthropic web
   search and stores hit URLs in `fact_check_evidence.url`. These are
   currently a flat list — a result from `mira.gov.mv` (official tax
   authority) and a result from a random Twitter aggregator carry the
   same weight at the schema level. Downstream consumers (the web UI's
   "Sources" panel, the ClaimReview JSON-LD `itemReviewed.author`
   block, the agent's narrative) cannot distinguish primary-source
   evidence from secondary-source evidence without re-classifying
   every URL.

The user supplied a starter registry of 25 Maldivian public-sector
entities (`presidency`, ministries, regulators, independent
commissions, utilities, SOEs) with their official `entity_id`,
`official_name`, `domain`, `entity_type`. This is the natural trust
anchor for "evidence is on an authoritative .gov.mv (or similar)
domain."

A reference fact-checking project needs to make this distinction
**visible in the schema**, not buried in heuristics.

## Decision

Adopt the supplied registry as a first-class artefact of the project:

- **Canonical source of truth**:
  `data/registry/maldives_public_sector.yaml` (human-editable, the
  format the user supplied).
- **Machine-loaded twin**:
  `data/registry/maldives_public_sector.json` (loaded by code; no
  pyyaml dependency required). A test
  (`tests/test_registry.py::TestRegistryParity`) asserts the YAML and
  JSON stay in sync.
- **Lookup module**: `kahzaabu/registry.py` provides
  `load_registry()`, `entity_for_url(url)`, `is_authoritative(url)`,
  `entity_by_id(eid)`, `all_entities()`. All pure-Python, stdlib only.
- **Schema integration**: additive nullable column
  `fact_check_evidence.authoritative_entity_id TEXT` (foreign-key-free
  pointer to `entities[].entity_id`). Auto-populated by
  `claims_db.insert_evidence()` when the URL hostname matches a
  registered domain. Indexed.
- **Match rule**: hostname equality OR subdomain ancestry. `www.`
  stripped. Case-insensitive. Examples:
  - `https://presidency.gov.mv/news/123` → `presidency`
  - `news.presidency.gov.mv/x` → `presidency` (subdomain)
  - `presidency.gov.mv` (bare) → `presidency`
  - `WWW.Presidency.gov.mv` → `presidency` (case + www)
  - `presidency-fake.gov.mv` → no match (must be the exact registered
    domain or a strict subdomain)
- **Behaviour**: additive trust signal, **not** a filter. Non-registered
  URLs are still ingested as evidence; they're just stored with
  `authoritative_entity_id = NULL`.
- **Entity-type taxonomy**: 17 entity types from the supplied YAML —
  `constitutional_executive`, `digital_service_portal`,
  `identity_authentication`, `ministry`, `agency`, `law_enforcement`,
  `customs`, `tax_authority`, `legislature`, `judiciary`,
  `independent_commission`, `regulator`, `state_owned_enterprise`,
  `utility`, `airport_operator`, `infrastructure_corporation`,
  `development_corporation`. Validated on load; unknown types fail
  loud.

## Alternatives considered

- **Treat all .gov.mv as authoritative.** Rejected — too coarse. Some
  registered entities (HRCM, MFDA) use non-`.gov.mv` domains
  (`hrcm.org.mv`, etc.). Some `.gov.mv` subdomains may also belong to
  deprecated or shadow agencies. An explicit registry is more
  defensible and more reviewable.
- **Pyyaml dependency.** Rejected for now — registry is small enough
  to ship a JSON twin. Avoids a runtime dep that 99% of users don't
  need. If the registry grows beyond ~200 entities or starts using
  YAML features (anchors, aliases), revisit.
- **Trust-tier numeric weight** (e.g. 0.0–1.0 per entity). Rejected as
  scope creep — boolean "registered or not" plus the `entity_type`
  enum is enough downstream signal. Weighting is the next iteration's
  problem when we have data to tune against.
- **Cross-reference at query time, not store-time.** Rejected — the
  store-time tagging is idempotent, cached at insert, and the
  schema becomes self-documenting (`SELECT entity_type, COUNT(*) FROM
  fact_check_evidence ...`).
- **Hardcode entities in a Python module.** Rejected — contributors
  (especially non-Python ones) should be able to PR a new entity by
  editing the YAML alone. Code change scales worse than data change.

## Consequences

**Positive.**

- Every fact-check now distinguishes primary-source evidence from
  secondary. The web UI can render a trust badge. The
  ClaimReview JSON-LD can populate `itemReviewed.author` with a real
  publisher when the source is authoritative.
- Contributors can extend the registry without touching Python code.
  The YAML is the diff.
- Provides a natural integration point for Slice 12's
  `kahzaabu transparency-report` — surface "how many of our
  fact-checks rely on primary-source evidence?"
- Forces an explicit decision about which entities count as
  authoritative for this project. Documentation is the artefact.

**Negative.**

- The registry must be kept current. When a Maldivian agency renames
  or merges, the YAML must be updated within the next eval cycle.
  Mitigated by the contribution process documented in `CONTRIBUTING.md`.
- The match rule is hostname-based. A site that quotes a press release
  but lives on its own domain (e.g. a news aggregator's republish of
  a presidency.gov.mv announcement) is **not** authoritative under
  this rule — even though the underlying content is. We accept this;
  the rule is about *publisher*, not *content origin*. Provenance
  beyond the domain is out of scope.
- `entity_for_url()` is a linear scan over ~25 entities; under
  microbenchmarks it's ~5 µs. If the registry grows beyond a few
  thousand entries, swap to a hash-suffix tree. Not a current concern.
