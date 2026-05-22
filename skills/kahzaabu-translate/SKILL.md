---
name: kahzaabu-translate
description: "Translate text between English and Dhivehi in the Maldives Presidency Office's distinctive style. Uses a paired-corpus few-shot prompt (top-3 topically-similar EN↔DV press releases) + a precomputed terminology glossary mined from the archive. NOT a generic translation tool — purpose-built for fidelity to the PO's formal political register."
version: 1.0.0
license: Apache-2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [translation, dhivehi, thaana, maldives, kahzaabu, civic-tech]
    category: language
    related_skills: [kahzaabu-fact-check]
prerequisites:
  hermes_plugins: [kahzaabu]
---

# kahzaabu-translate

## When to use this skill

Translate text between English and Dhivehi when **style fidelity to
the Maldives Presidency Office's press releases matters**. Examples:

- Drafting an English summary of a Dhivehi statement that should
  sound like the PO's own English releases
- Reproducing the formal register of a press release in the
  opposite language
- Preserving specific institutional terminology that has a
  canonical PO rendering (e.g. "Judicial Service Commission" ↔
  "ޝަރުޢީ ޚިދުމަތާ ބެހޭ ކޮމިޝަން")

If you just need a generic translation — a Google-translate-quality
output — this skill is overkill. Use the host model directly.
This skill exists because raw LLM output does NOT match the PO's
register; their distinctive markers like "ރައީސުލްޖުމްހޫރިއްޔާ"
(not "ޕްރެޒިޑެންޓް") for "President" are the kind of detail this
tool preserves and a generic translator drops.

## How to invoke

```
/kahzaabu-translate <text>
```

Or, in agent code, call the tool directly:

```python
kahzaabu_translate({
    "text": "The President met with the Cabinet today.",
    "target_language": "auto",   # or "EN" / "DV"
})
```

`auto` (default) detects the source language from the input (>50%
Thaana characters → source is Dhivehi) and translates to the
other.

## Terminology fidelity over literal accuracy

**The single most important rule** when reviewing translation
output: the PO has a preferred phrasing for many recurring
concepts, and the translator must defer to that phrasing — not
produce a literal word-for-word translation of the input.

**Worked example** (the real-world case that motivated this
rule):

| Input | Literal translation | PO's actual usage |
|---|---|---|
| "undocumented foreign nationals" | "undocumented foreign nationals" | "undocumented expatriate workers" |
| Frequency in corpus: | "foreign nationals" — 14 articles | "expatriate workers" — **35 articles** |

A literal translation would produce the first column; the PO's
press releases use the third. The few-shot exemplars carry the
canonical phrasing, and the system prompt explicitly instructs
the LLM to adopt the exemplar's wording when the same concept
appears.

**Verifier workflow for any translation you propagate further:**

1. Re-read the input — identify any phrase that has an
   institutional / political flavour (proper nouns, government
   programmes, labour categories, legal terms).
2. For each such phrase, search recent articles (last 365 days)
   via `kahzaabu_search_articles` — does the PO use that exact
   phrasing, or a different one?
3. If the PO uses a different phrasing, the translation's
   rendering of that phrase should match the PO's — not the
   input's literal wording.

This rule applies to the EN→DV direction (where the user might
input casual EN that needs the PO's formal Thaana) AND the DV→EN
direction (where Thaana terms have specific EN renderings the PO
prefers — "ބިދޭސީން" → "expatriate workers", not "foreigners" or
"foreign nationals").

## The PO's distinctive register

Markers the tool tries to preserve, in order of how-load-bearing
they are:

1. **Institutional names use full forms, not abbreviations.**
   "ރައީސުލްޖުމްހޫރިއްޔާ" (President of the Republic) is preferred
   over the shorter "ރައީސް" (President). "ދައުލަތުގެ ވަޒީރުންގެ
   މަޖިލިސް" (Cabinet of State Ministers) over generic "ކެބިނެޓް".

2. **Classical Thaana political vocabulary**, not colloquial. The
   register is closer to official gazette language than to spoken
   Dhivehi.

3. **No transliterations** of English political terms unless the
   term has no native Dhivehi equivalent. "Constitution" becomes
   "ޤާނޫނުއަސާސީ", not "ކޮންސްޓިޓިއުޝަން".

4. **Numbers and dates** match the PO's conventions (Dhivehi
   numerals are NOT used in the PO's press releases despite being
   available; they use Western 1-2-3 inside Thaana text).

5. **Speaker attribution conventions**, e.g. "ވިދާޅުވިއެވެ" (formal
   "[the President] said") at sentence end.

## How the tool produces this style

Three layers feed every translation prompt:

1. **System prompt** — a hand-written description of the PO's
   register with explicit examples of preferred renderings.
2. **Glossary subset** — relevant rows from
   `translation_glossary`, a precomputed dictionary mined from the
   paired corpus via a one-shot LLM extraction job. Provides
   institutional-name pairs sorted by frequency in the corpus.
3. **Few-shot exemplars** — 3 paired EN↔DV press releases from
   the last 90 days that are topic-similar to the input (via BM25
   over `articles_fts`). Recency matters because the PO's
   terminology can drift over years; the most recent corpus
   reflects current style.

The LLM (Claude Sonnet at temperature 0.3 for consistency)
synthesises the translation from these three layers. Each
invocation persists to `translation_runs` — both for the audit
trail and as an LRU cache (same input within 1h returns the
cached translation without a fresh LLM call).

## Verification

LLM-generated Dhivehi can be **grammatically valid but factually
wrong** — especially for proper nouns, numbers, dates, and
institutional names that don't appear in the glossary. The skill
treats every output as a starting draft, not a final translation.

Verification checklist for any translation you propagate further:

- [ ] Numbers and dates carried over exactly
- [ ] Proper nouns (people, places, institutions) rendered using
      the PO's canonical form (check `kahzaabu translate
      glossary-stats` to see registered terms)
- [ ] No transliterated English political terms that have a native
      equivalent
- [ ] Register matches the input's register (formal-formal,
      casual-casual — though casual input is rare in this corpus)
- [ ] Cite the source. Per kahzaabu's reference-implementation
      framing (ADR 0013, DISCLAIMER.md), automated translations
      must not be treated as authoritative. Link back to the
      original press release on presidency.gov.mv.

## Limitations

- **Single-block translation only.** Very long inputs (>4000
  chars) are rejected. For multi-paragraph documents,
  translate paragraph-by-paragraph and stitch.
- **No back-translation pass.** A round-trip EN → DV → EN does
  not guarantee semantic preservation; we don't add an automatic
  verification step. (Future slice could.)
- **Glossary coverage is bounded by the corpus.** Concepts that
  haven't appeared in 2024-2026 press releases won't have a
  glossary entry. The few-shot exemplars partially compensate by
  pulling topically-similar examples.
- **Output is automated analysis.** Per ADR 0013 (the no-auth /
  reference-implementation posture) and DISCLAIMER.md, kahzaabu's
  output is not an authoritative source — including translations.

## Related

- **`kahzaabu-fact-check`** — for verifying claims against the
  archive. Translation is upstream of fact-checking when the
  claim source is Dhivehi.
- **Source ADR** — `docs/adr/0016-style-faithful-translation.md`
  in the kahzaabu repo. Documents the design + alternatives
  considered (Google Translate, embedding-based retrieval over
  sentences, fine-tuning) + consequences.
- **Glossary builder** — `kahzaabu translate build-glossary` is
  the one-shot CLI that populates `translation_glossary`.
  Re-running it after a major corpus update refreshes the
  terminology.
