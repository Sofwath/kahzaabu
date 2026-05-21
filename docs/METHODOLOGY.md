# Methodology — kahzaabu

This document is the public-facing extended methodology of the
kahzaabu fact-checking pipeline. It explains, in plain English with
citations, how the project goes from "scraped press release" to
"published fact-check with AVeriTeC verdict and Truth-O-Meter label."

A shorter web-facing version is published at `/methodology` in the
web UI. The technical-implementation map lives in
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md); the per-decision rationale
lives in [`docs/adr/`](adr/).

## TL;DR

We chain six methodological ideas from the academic and journalistic
fact-checking literature into a single open pipeline:

1. **Claim extraction** — surface specific, checkable factual claims
   from political speech (extractor stage).
2. **Q&A decomposition** — convert each claim into the sub-questions
   you'd need to verify it (decomposer stage, AVeriTeC-style).
3. **Canonical claim matching** — group paraphrases of the same
   underlying assertion across time (matcher stage, Full Fact-style).
4. **Contradiction detection** — find pairs of statements the same
   speaker made that cannot both be true (contradiction finder stage).
5. **AVeriTeC verdict labeling** — classify each fact-check into the
   four AVeriTeC verdict categories.
6. **Truth-O-Meter publishing** — render each fact-check with the
   6-rung public ladder PolitiFact has trained the public to read,
   wrapped in schema.org ClaimReview JSON-LD for Google
   Fact-Check-Explorer indexing.

Each step is implemented as an idempotent CLI command, audited by a
run table, and evaluable against a held-out golden set
([ADR 0008](adr/0008-quality-evaluation.md)).

## 1. Claim extraction

**Why.** Press releases are dense prose. Most sentences are
narrative; only a small fraction make a specific checkable claim
("we will build 40,000 housing units by 2028"). We need to surface
those.

**How.** A Sonnet 4.6 call per article returns a list of claim records
with structured fields (`type`, `polarity`, `subject_normalized`,
`is_checkable`, `quote`). The polarity taxonomy follows
[ADR 0002](adr/0002-polarity-taxonomy.md): `AFFIRM`, `DENY`,
`PROMISE`, `DENIAL_OF_PROMISE`, `CLAIM_OF_FACT`, `NEUTRAL`. This
explicit polarity is critical for the later contradiction-finder,
which pairs polarity-opposites.

**Comparable systems.** ClaimBuster (Hassan et al., KDD 2017,
[doi:10.1145/3097983.3098131](https://doi.org/10.1145/3097983.3098131))
introduced the "check-worthiness" classifier for political speech;
modern LLM-based extractors (including ours) have largely subsumed it.

## 2. Q&A decomposition (AVeriTeC)

**Why.** A claim like "we have provided IGMH with permissions to
recruit 411 additional employees" hides several sub-claims:
*was permission granted?* / *what is the exact number?* / *has this
been done before with different numbers?* / *what is the current
baseline?*. Without explicit sub-questions, verification is
ad-hoc.

**How.** A Haiku 4.5 call per claim returns 3–5 questions, each
tagged with an `answer_type` (`Boolean` / `Extractive` /
`Abstractive`) and a `source_medium` (`archive` for the kahzaabu
corpus, `web_search` for external).

**Reference paper.** Schlichtkrull, Guo, Vlachos — *AVeriTeC: A
Dataset for Real-world Claim Verification with Evidence from the
Web* — EMNLP 2023
([arXiv 2305.13117](https://arxiv.org/abs/2305.13117)). The
question-decomposition pattern, the four-way verdict label (used
later in Stage 5), and the structured evidence format are taken
from this paper.

Backfill cost: $12.51 for 8,954 claims (35,648 questions). Haiku 4.5
substantially out-performed our Sonnet-based projection.

## 3. Canonical claim matching (Full Fact)

**Why.** The same political talking point shows up dozens of times
across press releases ("60 years of diplomatic relations with the
United Kingdom," "33% renewable by 2028," etc.). To detect a *change*
in stance, we first need to recognise that two phrasings refer to the
same underlying claim.

**How.** Two-phase pipeline ([ADR 0003](adr/0003-canonical-claim-matching.md),
[ADR 0007](adr/0007-embedding-provider-abstraction.md)):

1. **Embed every claim** with a pluggable provider (local
   sentence-transformers default; OpenAI or Voyage as alternatives).
2. **Match candidates** above cosine 0.85, then filter by named-entity
   Jaccard ≥ 0.6, then resolve ties with a Haiku LLM call.

**Reference work.** Full Fact's
*[An automated approach to detecting claims in political discourse](https://fullfact.org/about/automated/)*
(2018-onwards) pioneered claim-matching for verified fact-checks at
scale. They use sentence-similarity + reviewer-tuned thresholds; we
adopt the same shape with explicit provider abstraction so anyone
building on kahzaabu can pick a different embedding stack.

**Real-world output.** 8,954 claims grouped into 151 canonical
paraphrase groups (~1.7% repetition rate — meaningful but not noisy).

## 4. Contradiction detection

**Why.** The headline feature. Political accountability is sharpest
when a speaker has said two things that cannot both be true.

**How.** Three filters narrow the candidate pool before any LLM is
called ([ADR 0004](adr/0004-contradiction-verdict-4way.md)):

1. **Polarity-pair SQL shortlist.** Only consider pairs whose
   polarities form a contradictory pair (AFFIRM vs DENY, PROMISE vs
   DENIAL_OF_PROMISE, etc.).
2. **Semantic-similarity filter.** Cosine in [0.55, 0.95]:
   high enough to be on the same topic, low enough to not be a
   paraphrase.
3. **Sonnet 4.6 4-way classifier.** For each surviving candidate, the
   model returns one of:
   - `CONTRADICTION` — direct logical contradiction
   - `EVOLVING_POSITION` — defensibly changed mind
   - `CONTEXT_CHANGED` — earlier statement no longer applies
   - `NOT_CONTRADICTORY` — only superficially in tension

Each classification carries a `reasoning_chain` — a short structured
trace explaining the verdict, in the spirit of
**RAGAR** (Khaliq et al., 2024,
[arXiv 2404.12065](https://arxiv.org/abs/2404.12065))'s Chain-of-RAG
approach.

**Why four labels, not two.** Reducing to a CONTRADICTION/NO
binary loses signal — and gives the speaker no air for legitimate
position changes. Four labels is a hard but defensible classification
problem.

**Real-world output (May 2026 corpus).** 96,284 polarity-pair raw
candidates → 48 after semantic-similarity filter → **2 CONTRADICTION**
+ 46 NOT_CONTRADICTORY. Cost: $0.41 for the LLM step. The two
contradictions (judicial interference, external debt repayment) are
independently verifiable through the linked source press releases.

## 5. AVeriTeC verdict + PolitiFact Truth-O-Meter

**Why.** Internal categories (LIE, MISLEADING, etc.) are for the
curator. The public-facing surface needs (a) a vocabulary that the
fact-checking research community uses and (b) a scale that the
general public has been trained to read.

**How.** A pure-deterministic three-layer derivation
([ADR 0005](adr/0005-dual-labeling-averitec-politifact.md)):

1. The curator's V1 `category` + `confidence` →
2. AVeriTeC `verdict_label` (`SUPPORTED` / `REFUTED` /
   `NOT_ENOUGH_EVIDENCE` / `CONFLICTING_EVIDENCE`) →
3. PolitiFact 6-rung `truth_score` (1 = PANTS_ON_FIRE through
   6 = TRUE).

No second LLM call. The mapping is documented per-rule in
`kahzaabu/truth_score.py` and unit-tested as ADR 0005 ground truth
(`tests/golden/truth_score/`).

**References.**

- Schlichtkrull et al. for AVeriTeC verdicts (cited above).
- PolitiFact's
  *[Truth-O-Meter and explanations](https://www.politifact.com/article/2018/feb/12/principles-truth-o-meter-politifacts-methodology-i/)*
  for the 6-rung ladder.

## 6. ClaimReview JSON-LD publishing

**Why.** A research project that never reaches a reader is
incomplete. Google's Fact Check Explorer surfaces fact-checks
worldwide; eligibility requires schema.org `ClaimReview` JSON-LD on
each published fact-check page.

**How.** Per [ADR 0006](adr/0006-claimreview-jsonld.md),
`kahzaabu/claimreview.py` builds the JSON-LD payload at fact-check
publish time, includes a mandatory disclaimer
("Automated analysis; verify against the original press release"),
and caches it in the `fact_checks.claimreview_jsonld` column. The
web UI's `/api/factchecks/{id}/jsonld` and
`/api/claimreviews/feed.json` endpoints publish it.

**Reference.** schema.org
*[ClaimReview](https://schema.org/ClaimReview)* and Google's
*[Fact Check Markup Tool guidelines](https://developers.google.com/search/docs/appearance/structured-data/factcheck)*.

## Cross-cutting: authoritative external-reference registry

Verifier evidence carries one of two trust tiers:

- **Primary-source** — the evidence URL is on a domain in
  `data/registry/maldives_public_sector.yaml` (25 entities: the
  Presidency, ministries, regulators, independent commissions,
  utilities, SOEs). Auto-tagged with the registry's `entity_id` on
  `fact_check_evidence.authoritative_entity_id`.
- **Secondary** — any other URL. Still ingested as evidence; just not
  tagged.

The registry is the project's explicit answer to "which sources count
as authoritative for a Maldives Presidency fact-check?" Documented in
[ADR 0011](adr/0011-public-sector-registry.md). Contributors can extend
the registry by editing the YAML (the JSON twin is kept in sync via
the `tests/test_registry.py::TestRegistryParity` test).

The match rule is hostname-based: exact match or strict subdomain,
case-insensitive, `www.` stripped. The registry is a **trust signal,
not a filter** — non-registered URLs continue to flow through the
pipeline and the web UI.

## Cross-cutting: quality evaluation

Every LLM-call stage has a held-out golden set
under `tests/golden/<stage>/` per
[ADR 0008](adr/0008-quality-evaluation.md). Each fixture is tagged
**verified** (hand-confirmed ground truth) or **pinned** (drift
detector for prompt changes). `kahzaabu eval` produces a verified-
subset metric (real quality) and an all-fixture metric (drift) for
each stage; see [`docs/EVAL_RESULTS.md`](EVAL_RESULTS.md).

The closest external comparators are:

| System | Domain | Best published F1 |
|---|---|---|
| **FEVER** (Thorne et al., NAACL 2018) | Wikipedia | ~0.80 (current SOTA) |
| **AVeriTeC** (Schlichtkrull et al., EMNLP 2023) | Real-world claims | ~0.50 macro-F1 (SOTA) |
| **RAGAR** (Khaliq et al., 2024) | Political fact-checking | F1=0.85 |

Kahzaabu is **not directly comparable** to these because its corpus
is a single source (the Maldives presidency) — but the verified-subset
metric is meant to make it apples-to-apples once the verified set
grows.

## Cross-cutting: reproducibility

[ADR 0010](adr/0010-reproducibility-and-observability.md) sets the
target: every published fact-check should be reproducible end-to-end
from `raw_page_html` + the recorded run rows. Slice 12 will implement
`/api/reproducibility.json` to expose this provenance over HTTP plus
a `kahzaabu audit` CLI for bias/fairness summaries.

## Limitations

See [`docs/MODEL_CARD.md`](MODEL_CARD.md) for per-stage limitations
and [`docs/DATA_CARD.md`](DATA_CARD.md) for corpus-level limitations.
The single most important one to internalise:

> Kahzaabu's corpus is **one speaker** (the Office of the President
> of Maldives). Contradictions are found *within* this source's
> output. Cross-source verification — comparing against opposition
> statements, news media, or independent records — requires
> human follow-up.

## Citation

If you use kahzaabu's methodology, code, or data, please cite both
the project and the upstream papers it builds on:

```bibtex
@software{kahzaabu,
  title  = {Kahzaabu — automated fact-checking archive for the Maldives Presidency},
  author = {Mohamed, Sofwathullah and contributors},
  year   = {2026},
  url    = {https://github.com/Sofwath/kahzaabu},
  license = {Apache-2.0}
}

@inproceedings{schlichtkrull2023averitec,
  title     = {AVeriTeC: A Dataset for Real-world Claim Verification with Evidence from the Web},
  author    = {Schlichtkrull, Michael and Guo, Zhijiang and Vlachos, Andreas},
  booktitle = {EMNLP},
  year      = {2023}
}

@article{khaliq2024ragar,
  title  = {RAGAR: Chain-of-RAG for Political Fact-Checking},
  author = {Khaliq, Mohammad Abdul and others},
  year   = {2024},
  eprint = {2404.12065},
  archivePrefix = {arXiv}
}

@inproceedings{thorne2018fever,
  title     = {{FEVER}: a Large-scale Dataset for Fact Extraction and {VER}ification},
  author    = {Thorne, James and Vlachos, Andreas and Christodoulopoulos, Christos and Mittal, Arpit},
  booktitle = {NAACL-HLT},
  year      = {2018}
}

@inproceedings{hassan2017claimbuster,
  title     = {Toward Automated Fact-Checking: Detecting Check-worthy Factual Claims by ClaimBuster},
  author    = {Hassan, Naeemul and Arslan, Fatma and Li, Chengkai and Tremayne, Mark},
  booktitle = {KDD},
  year      = {2017}
}
```
