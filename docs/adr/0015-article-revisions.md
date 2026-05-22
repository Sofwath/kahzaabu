# ADR 0015 — Article-revision tracking

**Status**: Accepted (2026-05-22)

## Context

The kahzaabu archive scrapes `presidency.gov.mv` on a 12-hour
launchd cycle. Until this slice the upsert path (`db.insert_article`)
did `INSERT OR REPLACE` — if the press office quietly edited an
already-archived article (a "4 → 1" numeric fix, a photo swap, a
softened claim), the old content was overwritten silently.

That's a real integrity gap for a fact-checking archive. The
motivating scenario, reported by the maintainer: the press office
spokesperson said "4" something; a fact-check was issued against
that claim; the press office later edited the original article to
say something different; the kahzaabu archive's body_text quietly
moved to the new content, leaving the fact-check pointing at text
that no longer says what was fact-checked.

We need:

1. **Detection** — notice when an already-scraped article's
   content has changed.
2. **Preservation** — keep the prior version, not overwrite it.
3. **Auditability** — record WHAT changed and WHEN we noticed.
4. **Operator awareness** — surface the change so the operator
   can decide whether to re-extract claims or update verdicts.

## Decision

**Hash-and-archive on every scrape.**

### Schema (additive)

```sql
ALTER TABLE articles ADD COLUMN content_hash TEXT;

CREATE TABLE article_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL,
    language        TEXT    NOT NULL,
    content_hash    TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    body_text       TEXT,
    body_html       TEXT,
    image_urls      TEXT,
    reference       TEXT,
    published_date  TEXT,
    observed_at     TEXT    NOT NULL,   -- when WE first saw this version
    replaced_at     TEXT    NOT NULL,   -- when WE noticed the change
    diff_summary    TEXT
);
```

Both via `V2_SLICE15_*` in `kahzaabu/claims_db.py`, applied
idempotently. Existing rows get `content_hash = NULL` post-
migration; the scraper treats NULL as "first observation, can't
tell if it changed" — no false-positive on first scrape after
the upgrade.

### Hash function

`kahzaabu.revisions.compute_content_hash(title, body_text, reference, image_urls_json)`
returns SHA-256 over the editable fields. Normalisation:

- `None` → empty string (a missing field is not the literal string `"None"`)
- `image_urls_json` → parsed + sorted, so re-ordering image URLs without changing content doesn't trigger a fake revision

### Upsert flow (kahzaabu/db.py::insert_article)

1. Compute `new_hash` from the freshly-scraped fields.
2. SELECT the existing row (if any). If `content_hash IS NULL` →
   first scrape post-migration; just store, no archive.
3. If `existing.content_hash != new_hash` → call
   `archive_revision()` which:
     - generates a `diff_summary` (see below)
     - INSERTs the OLD row into `article_revisions` with
       `observed_at = old.scraped_at`, `replaced_at = now()`
4. INSERT OR REPLACE the articles row with the new content +
   `new_hash` + `scraped_at = now()`.

Steps 3 + 4 share `conn`'s implicit transaction; `conn.commit()`
seals them atomically.

### Diff-summary generator

Pure regex/length deltas — no LLM. Single-line semicolon-
separated digest designed to surface the cases a fact-checker
cares about:

- **Numeric shifts** (the "4 → 1" case) — token-level diff of
  numbers in the old vs new body
- **Length deltas** — body got materially longer/shorter (>20 chars)
- **Title changes** — flagged separately
- **Image count** — `images: 3 → 2`
- **Reference changes** — press-release number changed

Example output:
```
numbers: removed 4; added 1; body length 1842 → 1798 (-44)
```

### CLI surface

```
kahzaabu revisions list <article_id> [--language EN|DV]
kahzaabu revisions show <revision_id> [--no-body]
```

`list` prints all revisions oldest-first with the diff_summary.
`show` prints the full archived body so an operator can do a
real before/after read.

## Alternatives considered

- **LLM-generated diff summaries.** More readable ("the press
  office edited the housing-units claim from 4,000 to 3,000")
  but adds API cost per edit and adds a failure mode (LLM down →
  no diff). Regex is zero-ongoing-cost and catches the "4 → 1"
  case the maintainer specifically called out. A future
  `kahzaabu revisions explain <id>` LLM-powered command can layer
  on top if regex summaries prove too cryptic.
- **Soft-delete pattern** (`articles.deleted_at = now()` then
  insert new row with same id). Rejected because (id, language)
  is the schema PK and the codebase expects current article state
  at that key — every join would need to filter on `deleted_at IS
  NULL`. Separate revisions table is cleaner.
- **Trigger-based archival** in SQLite. Triggers would fire on
  every UPDATE including no-op ones, and would need to dynamically
  compute the diff_summary from inside SQL — awkward. The
  Python-side compare-then-archive is explicit, testable, and
  the right level of abstraction.
- **Hash the full raw_page_html** instead of editable fields.
  Rejected because raw HTML changes on every scrape (ad-tracker
  tokens, build IDs, comment timestamps) — every scrape would
  trigger a false-positive revision.

## Consequences

### Positive

- **Integrity**. Edits to source articles are no longer silent.
  `kahzaabu revisions list <id>` is the audit trail.
- **Zero ongoing cost**. Hashing is a few µs per article;
  diff_summary is regex on text already in memory.
- **Idempotent migration**. `content_hash` defaults NULL; no
  false-positives on the first scrape post-upgrade.
- **Single-source-of-truth upsert**. `db.insert_article` is the
  only writer of the articles table, so adding the compare-and-
  archive logic in one place catches all scrape paths
  (backfill, update, manual import).

### Negative — Storage

Each detected edit adds one row to `article_revisions` containing
the OLD title + body_text + body_html. Worst case: an article
that gets edited 5 times stores 5 old versions plus the current.
Bodies are typically <5 KB; 5 revisions on 100 articles is ~2.5
MB. Acceptable.

### Negative — Re-extraction is operator decision, not automatic

A detected edit does NOT automatically re-run the extractor on
the new content. Rationale: many edits are typo fixes or
formatting tweaks that don't change the factual content. Auto-re-
extraction would burn LLM budget on noise. Operators review the
revisions log + diff_summary, then manually run
`kahzaabu extract --article <id>` if the edit changes the
underlying claim. (Manual extract-by-article is a separate
follow-up; current `kahzaabu extract` runs on the whole queue.)

### Negative — No web UI yet

A "this article was edited N times — view history" badge on
`/article/{id}` is the natural follow-up. Deferred to a separate
slice so this one stays focused on the data model + scraper
behaviour. Until then operators use the CLI.

## Regression guards

`tests/test_revisions.py` covers:

- `compute_content_hash` is deterministic
- Same-content different-ordering of image URLs produces the
  same hash (order-insensitivity)
- Different fields produce different hashes
- `generate_diff_summary` correctly extracts numeric shifts
- `archive_revision` writes the expected row shape
- `db.insert_article`'s compare-then-archive:
   - First insert: no revision row, content_hash stored
   - Same content re-inserted: no revision row
   - Different content: ONE revision row with the old content
   - Multiple edits: chronological revision chain

## Superseding this ADR

Append-only — see `docs/adr/README.md` for the convention. If the
revision mechanism is replaced (e.g. moves to an external
event-log like Kafka), write ADR 00NN that references this one
and update this file's Status to "Superseded by ADR 00NN".
