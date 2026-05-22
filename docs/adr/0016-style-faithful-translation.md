# ADR 0016 — Press-office-style EN ↔ DV translation

**Status**: Accepted (2026-05-22)

## Context

The kahzaabu archive holds 2,648 EN-DV paired press releases
published by the Maldives Presidency Office (2024-2026). Each
pair describes the same content in both languages.

The Presidency Office writes Dhivehi in a distinctive formal
register: *"ރައީސުލްޖުމްހޫރިއްޔާ"* (the full constitutional title)
rather than the shorter *"ރައީސް"* for "President"; *"ދައުލަތުގެ
ވަޒީރުންގެ މަޖިލިސް"* for "Cabinet"; specific Thaana renderings
for institutional names ("Judicial Service Commission" →
"ޝަރުޢީ ޚިދުމަތާ ބެހޭ ކޮމިޝަން"). Generic translation tools (Google
Translate, raw Claude/GPT) don't reproduce this register —
they default to transliterations or shorter colloquial forms.

The user's framing (in the original feature request):

> "I need a function to translate given english or dhivehi
> based on existing patterns and style and language used in PO.
> We need to build skills for this and not just use default
> LLM to translate."

The paired corpus is the natural ground truth. The question is
how to use it.

## Decision

**Three-layer prompt at translation time:**

1. **Hand-written system prompt** documenting the PO's register
   with explicit examples of preferred renderings (the
   "ރައީސުލްޖުމްހޫރިއްޔާ not ޕްރެޒިޑެންޓް" canon).

2. **Precomputed glossary** (`translation_glossary` table). One-
   shot batch LLM extraction job (`kahzaabu translate
   build-glossary`) mines paired articles, sending each to Sonnet
   with a JSON-output schema asking for 5-12 EN↔DV term pairs.
   Frequencies aggregate across the sample; top-N rows persist
   to the table. At translation time, we inject only the rows
   whose source-language term substrings appear in the input
   text (cap 20-25 terms — context budget).

3. **Hybrid few-shot exemplars** (3 paired articles). Topic-
   similarity wins, falling back to recency. Topic similarity
   is BM25 over a new `articles_fts` virtual table (parallel to
   the existing `fact_checks_fts` from Slice 13); recency
   restricts to the last 90 days because the PO's preferred
   terminology drifts and recent corpus reflects current style.

The composed prompt → Sonnet at temperature 0.3 (deterministic-ish
for consistency). Each translation persists to `translation_runs`,
which doubles as an LRU cache: same input + target within 1h
returns the cached translation without a fresh LLM call.

## Surfaces

- **CLI**: `kahzaabu translate {text, build-glossary, glossary-stats}`
- **Hermes plugin tool**: `kahzaabu_translate(text, target_language)`
- **Slash command**: `/kahzaabu-translate <text>` (auto-detects
  source language)
- **Web UI**: `/translate` (textarea + provenance panel)
- **Hermes skill**: `skills/kahzaabu-translate/SKILL.md`
  (auto-installed via `scripts/install-hermes-skills.sh` — symlinks
  pick up new skill directories with zero config change)

## Alternatives considered

- **Google Translate / DeepL API.** Rejected because they don't
  know the PO's terminology canon. "President" → "ރައީސް" not
  "ރައީސުލްޖުމްހޫރިއްޔާ"; "Cabinet" → transliteration not the full
  Thaana form. The whole point of this slice is producing the
  PO's idiomatic style, which off-the-shelf translation
  explicitly doesn't.
- **Raw LLM call (no few-shot, no glossary).** Same problem —
  the model defaults to its training-distribution Dhivehi, not
  the PO's register.
- **Embedding-based retrieval over individual sentences instead
  of full paired articles.** Too granular. The PO's style is a
  paragraph-level property (sentence order, attribution patterns,
  closing conventions); pulling N best-matching sentences
  fragments that signal. Full-article exemplars preserve
  paragraph-level structure.
- **Fine-tune a model on the paired corpus.** ~2.6k pairs is on
  the low end for fine-tuning; cost + maintenance burden + the
  need to re-tune as corpus grows. Few-shot + glossary is the
  right MVP; revisit if quality plateaus.
- **No glossary, few-shot only.** Tried in design — works for
  general phrasing but term-pair consistency suffers for low-
  frequency institutional names that don't appear in the
  randomly-selected exemplars. The glossary covers the long tail.

## Consequences

### Positive

- **Style fidelity.** Translation output uses the PO's preferred
  terminology and register. Verifiable via spot-checks against
  the corpus and the audit log in `translation_runs`.
- **Reuse of existing infrastructure.** `articles.paired_id` (V1)
  + Sonnet via `kahzaabu.pricing` (Slice 10) + slowapi rate
  limiting + daily-spend cap (Slice 11) all already exist.
- **No new external dependencies.** Pure SQLite + Anthropic SDK.
- **Provenance** is part of every output: `exemplar_ids`,
  `glossary_terms_used`, `cost_usd`, `cache_hit` all return to
  the caller. The web UI's expandable provenance panel makes the
  retrieval visible to humans.

### Negative — Cost

- **Glossary build**: ~$5-10 one-shot (sample 200 paired articles
  × ~$0.05 each), budget-capped by `--budget`. Re-runs are
  idempotent w.r.t. the extracting model alias (clears prior
  rows from same model, preserves manual edits).
- **Per-translation**: ~$0.02-0.04 (3 exemplars + ~20 glossary
  terms in context + Sonnet at temperature 0.3). Daily web cap
  defaults to half the `/api/ask` cap (overridable via
  `KAHZAABU_TRANSLATE_DAILY_CAP_USD`).

### Negative — Quality limits

- **LLM-generated Dhivehi can be grammatically valid but factually
  wrong** — especially for proper nouns, numbers, and dates. The
  skill's SKILL.md states this explicitly; the web UI surfaces a
  warning band; every output carries the standard reference-
  implementation disclaimer per ADR 0013 (no in-app auth) and
  `DISCLAIMER.md`.
- **No back-translation verification.** A round-trip translation
  doesn't guarantee semantic preservation; we don't run an
  automatic check. Operators verify manually.
- **Single-block input** (max 4000 chars). Multi-paragraph
  documents need to be translated paragraph-by-paragraph and
  stitched by the caller. Defer paragraph-stitching to a future
  slice if usage justifies it.
- **Bounded by corpus coverage.** Concepts that haven't appeared
  in 2024-2026 PO press releases won't have glossary entries
  and won't influence the few-shot exemplar selection. The
  hand-written system prompt's distinctive markers are the
  baseline for these cases.

## Regression guards

`tests/test_translator.py` (Slice 16):

- Language detection: pure Latin → EN, pure Thaana → DV,
  mixed-dominant → correct dominant, empty → EN default
- Few-shot selection: topic-similar wins inside the recency
  window; falls back to most-recent when no FTS5 hits; respects
  `k` cap; only returns pairs with non-empty bodies on both
  sides
- Glossary subset: only terms whose source appears in the input
  are returned; sorted by freq DESC; respects `max_terms`
- `translate()` with mocked LLM: prompt assembly correct (system
  + glossary + exemplars + input); writes `translation_runs` row;
  returns expected shape
- Cache hit: second call within TTL reads from `translation_runs`
  (no LLM call); cache misses on different target_lang
- API endpoint shape + daily-cap (mirrors `test_articles_api.py`
  pattern)

## Superseding this ADR

Append-only — see `docs/adr/README.md`. If translation moves to a
fine-tuned model or an embedding-based retrieval, write a new ADR
that references this one and update Status here to "Superseded
by ADR 00NN".
