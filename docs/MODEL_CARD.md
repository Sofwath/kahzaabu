# Model Card — kahzaabu LLM-call stages

Following the Model Cards framework (Mitchell et al., FAccT 2019,
[arXiv 1810.03993](https://arxiv.org/abs/1810.03993)).

This card describes every LLM-call site in the kahzaabu pipeline.
Kahzaabu does not train models — it calls hosted APIs. The "model"
documented per stage is therefore the **(provider, model_id, prompt
version, decoding parameters)** combination as exercised by kahzaabu.

## Common card metadata

| Field | Value |
|---|---|
| Project | kahzaabu — Maldives Presidency fact-checking pipeline |
| Version | 0.1.0 (V2 — Slices 0–11 published) |
| Card revision | 2026-05-21 |
| License (this card + code) | Apache-2.0 |
| Maintainer | Sofwathullah Mohamed (`Sofwathullah.Mohamed@gmail.com`) |
| Repository | this directory |
| Intended use | Civic-tech: surfacing patterns in publicly-published government statements |
| Out of scope | Final journalistic verdicts, individual prosecution, social-media targeting, decisions affecting any person's legal status |
| Subject population | Statements published by the Government of Maldives (Office of the President) — a single public-figure source |

All stages share two operational defaults:

- **No PII collection**: corpus is public press releases only.
- **No training**: we use providers' API endpoints; no user data leaves
  the inference path; no fine-tuning. If the upstream provider's
  privacy policy permits opt-out of input retention, kahzaabu sets it.

---

## Stage 1 — Extractor (`kahzaabu/extractor.py`)

| Field | Value |
|---|---|
| Model | `claude-sonnet-4-6` (Anthropic) |
| Prompt | `extractor.SYSTEM` — extract checkable claims with `type`, `polarity`, `subject_normalized`, `is_checkable` fields |
| Temperature | 0.0 (deterministic decoding) |
| Max tokens | 4096 |
| Output schema | List of `{type, polarity, subject_normalized, is_checkable, quote, ...}` |
| Cost per article | ~$0.005–0.010 |

**Intended use.** Surface specific factual claims from press releases —
numeric promises ("40,000 housing units"), deadlines, comparative
boasts, denials, credits.

**Known limitations.**

- Tagging boundary cases are ambiguous. The `deadline_promise` taxonomy
  in particular conflates political deadlines (a leader's promise with
  a date) with event timetables (exhibition opening hours). See
  `tests/golden/extractor/03-article-32009.json` for the canonical
  unresolved example.
- The extractor sometimes splits one logical claim into two (the
  inauguration + the inauguration ceremony). Downstream `matcher.py`
  re-unifies these via canonical_claim_id, but the per-article claim
  count is inflated.
- Quote-extraction preserves the original English text only.
  Dhivehi-source claims live in a separate column and are not
  extracted by this stage (covered by `dv_compare.py`).

**Eval coverage.** `tests/golden/extractor/` — 4 fixtures, 3 verified
ground truth. Scoring: Jaccard F1 over (type, polarity, quote-prefix)
tuples. Current verified-subset F1: **1.000**.

---

## Stage 2 — Decomposer (`kahzaabu/decomposer.py`)

| Field | Value |
|---|---|
| Model | `claude-haiku-4-5-20251001` (Anthropic) |
| Prompt | `decomposer.SYSTEM` — break each claim into AVeriTeC-style sub-questions with `{question, answer_type, source_medium}` |
| Temperature | 0.0 |
| Batch size | 20 claims per API call |
| Cost per claim | ~$0.0014 (Haiku 4.5, ~$12.51 for full 8,954-claim backfill) |
| ADR | [0001](adr/0001-v2-architecture-overview.md) |

**Intended use.** Convert each claim into 3–5 verification questions
that an investigator (human or agent) would need to answer. Maps to
AVeriTeC's "ProofVer-style" decomposition (Schlichtkrull et al.,
EMNLP 2023).

**Known limitations.**

- Question quality is uneven. Some claims yield 4 questions that
  collapse to the same archive-lookup intent (Boolean over the same
  predicate); others yield genuinely diverse Q&A.
- `source_medium=archive` is over-applied. The decomposer tends to
  route lookup questions to the kahzaabu corpus even when external
  verification (e.g., a calendar fact) would be more appropriate.
- Multilingual claims (English with Dhivehi quotations) are decomposed
  in English only; the Dhivehi portion is treated as opaque text.

**Eval coverage.** `tests/golden/decomposer/` — 4 fixtures, all verified
ground truth. Scoring: Jaccard F1 over (answer_type, source_medium)
tuples. Current verified-subset F1: **1.000**.

---

## Stage 3 — Matcher (`kahzaabu/matcher.py` + `kahzaabu/embeddings.py`)

| Field | Value |
|---|---|
| Embedding provider | Pluggable per [ADR 0007](adr/0007-embedding-provider-abstraction.md): `local` (sentence-transformers, all-MiniLM-L6-v2, 384-dim, default $0) / `openai` (text-embedding-3-small, 1536-dim, $0.02/M tokens) / `voyage` (voyage-3, 1024-dim, $0.06/M tokens) |
| LLM tiebreaker | `claude-haiku-4-5-20251001` (~$0.0008/call) |
| Cosine threshold | 0.85 (paraphrase candidate) |
| Entity overlap | ≥ 0.6 Jaccard over extracted named entities |
| ADR | [0003](adr/0003-canonical-claim-matching.md), [0007](adr/0007-embedding-provider-abstraction.md) |

**Intended use.** Group claims that are paraphrases of the same
canonical statement (e.g., "60 years of diplomatic relations" appearing
in 7 different press releases). Used by the contradiction-finder to
restrict comparisons to claims on the same topic.

**Known limitations.**

- Sentence-transformers (the default) is English-only. Dhivehi claims
  are matched on their English translations.
- The 0.85 cosine threshold was tuned on this corpus; thresholds
  appropriate elsewhere may differ. ADR 0003 documents the empirical
  basis.
- Entity extraction uses regex-based capitalised-token heuristics, not
  a full NER model. Maldivian transliterated names ("Faafu Atoll",
  "Eydhafushi Island") are correctly captured; complex multi-word
  proper nouns may be split.

**Eval coverage.** `tests/golden/matcher/` — 6 fixtures (3 SAME + 3
DIFFERENT), all verified ground truth. Scoring: binary classification
F1. Current verified-subset macro-F1: **1.000**.

---

## Stage 4 — Contradiction finder (`kahzaabu/contradictions.py`)

| Field | Value |
|---|---|
| Model | `claude-sonnet-4-6` (Anthropic) |
| Prompt | `contradictions._CLASSIFIER_SYSTEM` — 4-way verdict + reasoning chain |
| Temperature | 0.0 |
| Verdict labels | `CONTRADICTION` / `EVOLVING_POSITION` / `CONTEXT_CHANGED` / `NOT_CONTRADICTORY` |
| Pre-filter | polarity-pair SQL shortlist + semantic-similarity bracket [0.55, 0.95] |
| Cost per candidate pair | ~$0.005 |
| ADR | [0004](adr/0004-contradiction-verdict-4way.md) |

**Intended use.** Identify pairs of statements that the same speaker
made at different times that contradict each other. The 4-way verdict
distinguishes intentional contradiction from legitimate evolution and
from changed external circumstances.

**Known limitations.**

- The model is asked to judge contradiction at the *statement* level;
  it doesn't have access to broader context (was a related law passed?
  did global market conditions change?). For high-stakes verdicts the
  reasoning_chain field should be reviewed by a human.
- A pair classified `EVOLVING_POSITION` may indicate either a defensible
  change of mind or an opportunistic reversal — the classifier does
  not distinguish these.
- Confidence calibration is approximate; we use the LLM's self-reported
  confidence as an input to the public-facing Truth-O-Meter mapping
  (see ADR 0005). A confidence ≥ 0.95 elevates a LIE to PANTS_ON_FIRE.

**Eval coverage.** `tests/golden/contradictions/` — 5 fixtures (2
CONTRADICTION + 3 NOT_CONTRADICTORY), all verified ground truth.
Scoring: 4-way macro-F1. Current verified-subset macro-F1: **1.000**.

**Real-world output (May 2026 corpus, 48 candidate pairs):**

| Verdict | Count |
|---|---|
| CONTRADICTION | 2 |
| NOT_CONTRADICTORY | 46 |

Both CONTRADICTIONs are independently verifiable through the linked
source press releases (judicial-interference Nov 2023 / May 2025;
external-debt Jan 2024 / Mar 2026).

---

## Stage 5 — Curator (`kahzaabu/curator.py`)

| Field | Value |
|---|---|
| Model | `claude-sonnet-4-6` (Anthropic) |
| Prompt | `curator.SYSTEM` — surface category + confidence per fact-check |
| Temperature | 0.0 |
| Output categories | `LIE` · `MISLEADING` · `BROKEN_DEADLINE` · `CREDIT_THEFT` · `SHIFTING_NUMBERS` · `CONTRADICTION` |
| Cost per topic | ~$0.05 |

**Intended use.** Read all claims on the same topic across time and
identify factual problems (V1 categories). Output feeds the
deterministic V2 verdict mapping (ADR 0005).

**Known limitations.**

- The 6-category taxonomy was V1-era and predates the AVeriTeC verdict
  layer. The category is **internal**; the public-facing label is the
  AVeriTeC verdict + Truth-O-Meter derived in `truth_score.py`.
  See ADR 0005 for the rationale.
- Confidence is the model's self-report. Low-confidence categorisations
  flow through to `NOT_ENOUGH_EVIDENCE` or `CONFLICTING_EVIDENCE`
  AVeriTeC verdicts, but the calibration is approximate.

**Eval coverage.** Indirect — covered by `tests/golden/truth_score/`
(6 fixtures, all verified) which validates the deterministic mapping
from `(category, confidence)` to `(verdict_label, truth_score,
truth_score_label)`.

---

## Stage 6 — Verifier (`kahzaabu/verifier.py`)

| Field | Value |
|---|---|
| Model | `claude-haiku-4-5-20251001` (Anthropic) |
| Server tool | `web_search_20250305` (Anthropic-hosted) |
| Prompt | `verifier.SYSTEM` — classify each search result as `confirms` / `contradicts` / `context` / `unclear` / `not_found` |
| Temperature | 0.0 |
| Cost per fact-check | ~$0.03 + $0.01/search |

**Intended use.** Provide external corroboration / contradiction for
high-severity fact-checks. Output cached in `fact_check_evidence` table
with the search snippet, relevance label, and a short rationale.

**Trust tier auto-tagging** ([ADR 0011](adr/0011-public-sector-registry.md)).
Every evidence row's URL is matched against the public-sector entity
registry (`data/registry/maldives_public_sector.yaml`). Hostnames on a
registered domain (presidency.gov.mv, foreign.gov.mv, mira.gov.mv, …)
get tagged with the registry's `entity_id` in
`fact_check_evidence.authoritative_entity_id`. Backfill of pre-Slice-11.5
rows tagged 48 of 300 existing evidence rows as primary-source.

**Known limitations.**

- Web search respects publisher `robots.txt` (Anthropic's server tool
  policy), which can exclude some authoritative sources.
- The verifier is not a fact-checking oracle — it classifies what the
  search engine returned, which may itself be biased or outdated.
- No paid-source access (Maldives Independent paywall, etc.). Public
  sources only.
- The trust tier is binary (authoritative / not). A weighted score is
  out of scope for V2; deferred until eval data supports calibration.

**Eval coverage.** `tests/golden/verifier/` — 8 fixtures pulled from
live `fact_check_evidence` rows, spanning all 5 relevance labels
(`confirms`/`contradicts`/`context`/`unclear`/`no_relevant_info`).
Scoring: Jaccard F1 over (url_prefix, relevance) tuples per
fact-check. All fixtures currently PINNED (`verified: false`)
because relevance classifications on third-party news are subjective
— promoting to verified requires hand-review. The drift detector
catches prompt edits that shift the relevance distribution; the
verified subset is the path to a real quality metric as fixtures
get hand-reviewed.

---

## Stage 7 — Agentic Q&A (`kahzaabu/qna_agentic.py`)

| Field | Value |
|---|---|
| Model (loop) | `claude-sonnet-4-6` (Anthropic) |
| Model (narrative-tricks pass) | `claude-haiku-4-5-20251001` or hermes `ctx.llm` provider |
| Decoding | Tool-use mode; temperature 0.0 for tool decisions |
| Cost per answer | ~$0.02–0.05 |

**Intended use.** Answer natural-language questions about the
Presidency corpus via an internal tool loop (archive_stats /
search_articles / get_article / search_factchecks / ... / web_search).

**Known limitations.**

- Like all agentic loops, error states can cascade. We bound the
  iteration count and emit a structured fallback if no tool resolves
  the question.
- The narrative-tricks layer (the "🎭 Narrative tricks observed"
  section) is a separate pass and may surface framing critiques even
  when the underlying claim is factually accurate. Treat it as
  analysis, not verdict.
- Web-search responses can lag corpus updates by hours-to-days.

**Eval coverage.** No golden fixtures (open-ended generation is hard
to pin); covered indirectly by `test_host_llm_branch.py` for the
narrative-tricks fallback invariant.

---

## Stage 8 — DV/EN consistency checker (`kahzaabu/dv_compare.py`)

| Field | Value |
|---|---|
| Model | `claude-sonnet-4-6` (Anthropic, multilingual) |
| Prompt | `dv_compare.SYSTEM` — flag numeric / omission / softening / embellishment differences |
| Temperature | 0.0 |
| Cost per pair | ~$0.08 |

**Intended use.** Read paired EN+DV press releases (2,648 pairs in the
corpus) and flag factual differences across translations. Stored in
`dv_en_inconsistencies`.

**Known limitations.**

- The model treats English as primary; differences expressed only in
  Dhivehi nuance may not surface.
- The "softening / embellishment" categories are subjective; the
  flagged items are starting points for human review, not verdicts.

**Eval coverage.** No golden fixtures (translation differences are
inherently subjective). Manual review is the gate before any flag
becomes a publishable fact-check.

---

## Bias considerations (whole pipeline)

The corpus is **one source**: the Maldives Office of the President's
press output. By construction:

- The dataset is **partisan-skewed**: there is no opposition
  perspective.
- The dataset is **English-translation-skewed**: the originals are
  often Dhivehi, and the published English versions are a
  re-presentation chosen by the press office.
- "Contradictions" are detected within this single source's output.
  Contradictions between government and other sources require manual
  cross-referencing.
- The Truth-O-Meter ladder (TRUE → PANTS_ON_FIRE) is a US-political
  taxonomy adapted to the Maldives context. Subtle differences in
  political-speech norms across cultures may make some thresholds
  too strict or too lax.

Mitigations:

- Every output links to the original press release URL on
  `presidency.gov.mv`. Readers must verify against the source.
- The web UI carries a permanent disclaimer
  (`KAHZAABU_DISCLAIMER` in `claimreview.py`) on every public-facing
  fact-check.
- `kahzaabu eval` includes a verified-subset metric so anyone re-running
  the pipeline can see whether prompt changes regressed against the
  documented baseline.

## Citation

If you cite this card or any stage's methodology, cite the project:

```bibtex
@software{kahzaabu,
  title  = {Kahzaabu — automated fact-checking archive for the Maldives Presidency},
  author = {Mohamed, Sofwathullah and contributors},
  year   = {2026},
  url    = {https://github.com/<repo>/kahzaabu},
  license = {Apache-2.0}
}
```

And the upstream methodology papers — see
[`docs/METHODOLOGY.md`](METHODOLOGY.md) for the full citation list.
