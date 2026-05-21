# Kahzaabu: An Open-Source Pipeline for Automated Fact-Checking of a Single-Speaker Political Corpus

**Sofwathullah Mohamed**¹ and the kahzaabu contributors

¹ Independent researcher · `Sofwathullah.Mohamed@gmail.com`

**Draft v0.1 · 2026-05-21 · Apache-2.0 · arXiv submission planned**

---

## Abstract

We present **kahzaabu**, an open-source pipeline for automated
fact-checking of a single-speaker political corpus: the public press
output of the Office of the President of the Republic of Maldives
(November 2023 – present). The pipeline chains six established
methodologies from the fact-checking literature — claim extraction,
AVeriTeC-style question decomposition (Schlichtkrull et al., 2023),
Full-Fact-style canonical claim matching, four-way contradiction
detection with structured reasoning chains in the RAGAR
(Khaliq et al., 2024) style, PolitiFact-style Truth-O-Meter labelling,
and schema.org `ClaimReview` publication. We introduce two methodological
contributions: (1) an explicit **verified-vs-pinned** distinction in our
golden-evaluation set that separates hand-confirmed ground truth from
drift baselines, addressing the common pitfall of fact-checking
benchmarks that conflate "no change since last release" with "high
quality"; and (2) an **external-reference trust registry** that tags
verification evidence by publisher authority, enabling quantitative
transparency reports on primary-source coverage. On the live Maldives
Presidency corpus (20,811 articles; 9,876 claims; 220 published
fact-checks), the pipeline surfaces two independently-verifiable
contradictions and reports a 15.8% primary-source rate against the
public-sector entity registry. The full system, evaluation set,
reproducibility manifests, and architectural decision records are
released under Apache-2.0.

**Keywords:** automated fact-checking, civic technology, claim
matching, contradiction detection, evaluation methodology, low-resource
political corpora.

---

## 1. Introduction

Automated fact-checking has matured rapidly since the FEVER shared
task (Thorne et al., 2018) demonstrated that fact verification can be
operationalised as a three-stage pipeline of evidence retrieval, claim
matching, and verdict prediction. AVeriTeC (Schlichtkrull et al., 2023)
extended the paradigm to real-world claims with structured Q&A evidence,
and RAGAR (Khaliq et al., 2024) added retrieval-augmented chain-of-thought
reasoning that achieved F1 ≈ 0.85 on political fact-checking. In parallel,
practitioner organisations (Full Fact in the UK, PolitiFact in the US)
have demonstrated production-scale claim matching and public-facing
verdict communication.

These advances share a common assumption: a **multi-speaker, multi-source
corpus** — typically Wikipedia (FEVER), news (AVeriTeC), or campaign
discourse with adversarial frames. None of the existing benchmarks
directly addresses the methodologically distinct case of a
**single-speaker official corpus**: the consolidated press output of a
sitting head of state. This case is common in small-state contexts where
the executive's press office is the dominant source of policy
communication and where independent journalism has limited reach. The
methodological challenge is asymmetric: claim extraction is easier
(consistent voice, structured press releases), but **contradiction
detection becomes the dominant signal** (the same speaker's positions
across time are the primary check against the speaker's other positions),
and **verification evidence** must be carefully tier-labelled to avoid
inadvertently endorsing partisan secondary sources.

We present kahzaabu (Dhivehi: *ކަޒާބު*, "falsehood"), an open-source
pipeline addressing this configuration. The system runs on the public
press output of the Office of the President of the Republic of Maldives,
beginning at the inauguration of Mohamed Muizzu on 2023-11-17. Our
contributions are:

1. **A reference implementation** that chains six established
   fact-checking methodologies into a reproducible pipeline.
2. **An evaluation methodology** that distinguishes *verified* fixtures
   (hand-confirmed ground truth) from *pinned* fixtures (drift detectors
   that catch prompt regressions but make no truth claim).
3. **An external-reference trust registry** of 25 Maldivian
   public-sector entities, used to tag verification evidence at
   schema level.
4. **A reproducibility manifest** per published fact-check, joining
   the curation run, supporting claims with extraction provenance,
   decomposition questions, verification evidence with publisher tier,
   contradiction-pair reasoning chains, cached ClaimReview JSON-LD,
   and the git commit at publication time.
5. **A complete open-source release** under Apache-2.0 with twelve
   architectural decision records (ADRs), model and data cards,
   maintenance documentation, and CI-enforced quality gates.

This document presents the methodology underlying that release. Section 2
surveys related work. Sections 3 through 8 describe each pipeline stage.
Sections 9 and 10 present cross-cutting evaluation and trust mechanisms.
Section 11 discusses limitations.

---

## 2. Related Work

**Fact-checking pipelines.** ClaimBuster (Hassan et al., 2017)
introduced check-worthiness classification, distinguishing claims that
warrant verification from rhetorical or definitional statements. Recent
LLM-based extractors largely subsume this functionality.

**Evidence-structured fact-checking.** AVeriTeC (Schlichtkrull et al.,
2023) introduced a 4,568-claim benchmark with Q&A evidence and a
four-way verdict label — `SUPPORTED`, `REFUTED`, `NOT_ENOUGH_EVIDENCE`,
`CONFLICTING_EVIDENCE` — that we adopt directly (Section 6). Their
question-decomposition formulation underlies our stage 3 (Section 4).

**Chain-of-RAG.** RAGAR (Khaliq et al., 2024) demonstrated that
retrieval-augmented chain-of-thought reasoning over political fact-checks
reaches F1 ≈ 0.85. Our contradiction detector (Section 5) adopts a
similar structured-reasoning output (a four-step chain: assertion
extraction, compatibility analysis, mitigating-event search, verdict).

**Claim matching at scale.** Full Fact (2018–) pioneered production
claim matching for verified fact-checks, using sentence-similarity
ranking with reviewer-tuned thresholds. We follow the same shape
(Section 4) with an explicit embedding-provider abstraction so the
pipeline supports local sentence-transformers, OpenAI, and Voyage
backends interchangeably.

**Public-facing verdict communication.** PolitiFact's Truth-O-Meter
(Adair et al., 2007–) operationalises a six-rung ladder
(`TRUE` ↔ `PANTS_ON_FIRE`) that journalistic audiences have been trained
to read. We adopt the ladder as a deterministic function of an
AVeriTeC-style verdict + a self-reported model confidence (Section 6).

**Discoverability.** Google's Fact Check Explorer requires schema.org
`ClaimReview` JSON-LD on published fact-check pages. We cache this
payload per fact-check at publication time (Section 7).

**Single-source corpora.** To our knowledge, no existing benchmark
addresses the configuration we study (sustained single-speaker political
output). The closest analog is fact-checking of campaign-speech corpora
(Hassan et al., 2017; Wang, 2017), but those datasets typically draw
from multiple speakers and adversarial debate contexts.

---

## 3. Stage 1 — Claim Extraction

**Goal.** Surface specific, checkable factual claims from press-release
prose.

**Method.** A single Sonnet 4.6 (Anthropic) call per article returns a
list of claim records with the schema:

```
{ type:              {policy_assertion, credit_claim, numeric_promise,
                       numeric_update, deadline_promise, boast, …},
  polarity:          {AFFIRM, DENY, PROMISE, DENIAL_OF_PROMISE,
                       CLAIM_OF_FACT, NEUTRAL},
  subject_normalized: entity string,
  is_checkable:      bool,
  quote:             original surface form,
  ... }
```

The `polarity` taxonomy (ADR 0002) is critical for the downstream
contradiction-finder (Section 5), which pairs claims with opposite
polarities. The six-label taxonomy was developed iteratively on the
live corpus; pre-V2 work used a binary affirm/deny scheme that
under-counted policy reversals.

**Cost.** ~$0.005–0.010 per article. At ~5 new articles/day, this is
negligible against the project's $250 V2 budget.

---

## 4. Stage 2 — Question Decomposition (AVeriTeC-style)

**Goal.** Convert each claim into 3–5 verification questions, each
tagged with the type of answer expected (`Boolean`, `Extractive`,
`Abstractive`) and the source medium where it would be answered
(`archive` for the kahzaabu corpus, `web_search` for external
verification).

**Method.** A Haiku 4.5 (Anthropic) call per claim, batched 20 claims
at a time. The prompt enforces the AVeriTeC enum vocabulary so output
is directly comparable to the AVeriTeC reference system.

**Empirical observation.** Haiku 4.5 substantially outperformed our
Sonnet-based cost projection. The 8,954-claim backfill cost
**$12.51 against a $200 projected upper bound** — a 16× cost
improvement attributable to the Haiku architecture, not to prompt
engineering.

**Result.** 35,648 questions generated, averaging 3.98 per claim,
spanning the AVeriTeC answer-type distribution roughly evenly.

## 4.1 Stage 3 — Canonical Claim Matching

**Goal.** Group claims that are paraphrases of the same underlying
assertion (e.g. "60 years of diplomatic relations" appearing in seven
distinct press releases).

**Method.** A two-phase pipeline (ADR 0003, ADR 0007):

1. **Embed every claim** using a pluggable provider. The local default
   is `sentence-transformers/all-MiniLM-L6-v2` (384-dim, $0). OpenAI
   `text-embedding-3-small` (1536-dim, $0.02/M tokens) and Voyage AI
   `voyage-3` (1024-dim, $0.06/M tokens) are interchangeable
   alternatives selected via a `KAHZAABU_EMBED_PROVIDER` environment
   variable.
2. **Match candidates** above cosine similarity 0.85, filter by
   named-entity Jaccard ≥ 0.6 over a regex-extracted entity set, and
   resolve ties with a Haiku 4.5 binary classifier (SAME/DIFFERENT).

**Result.** 8,954 claims grouped into 151 canonical paraphrase groups
(1.7% repetition rate). At the lower end of typical political-corpus
repetition: meaningful but not noisy.

---

## 5. Stage 4 — Contradiction Detection (Headline Stage)

**Goal.** Identify pairs of statements the same speaker made at different
times that cannot both be true under any reasonable interpretation.

**Method.** Three filters narrow the candidate pool before any LLM is
called (ADR 0004):

1. **Polarity-pair SQL shortlist.** Only consider pairs whose polarities
   form a contradictory pair: `AFFIRM ↔ DENY`, `PROMISE ↔
   DENIAL_OF_PROMISE`, etc. Symmetric — each pair surfaces in either
   order. This stage reduced 39M raw pairs to 96,284 candidates.
2. **Semantic-similarity filter.** Cosine similarity in [0.55, 0.95].
   High enough to be on the same topic, low enough to not be a
   paraphrase (paraphrases of contradictory statements are themselves
   paraphrases of the same position — the high-end cut is essential).
   This stage reduced 96,284 candidates to 48 survivors.
3. **Sonnet 4.6 four-way classifier.** Each survivor is classified
   `CONTRADICTION` / `EVOLVING_POSITION` / `CONTEXT_CHANGED` /
   `NOT_CONTRADICTORY`. Each classification carries a four-step
   reasoning chain (assertion A, assertion B, logical-compatibility
   analysis, mitigating-event search).

**Why four labels.** A binary contradiction/no scheme loses signal
and offers no air for legitimate position changes. Of our 48
candidates:

| Verdict | Count |
|---|---|
| `CONTRADICTION` | 2 |
| `NOT_CONTRADICTORY` | 46 |
| `EVOLVING_POSITION` | 0 |
| `CONTEXT_CHANGED` | 0 |

Both surfaced `CONTRADICTION` verdicts are independently verifiable
through the linked source press releases (one on judicial interference,
one on external-debt repayment).

**Cost.** $0.41 total for the LLM step on the full corpus.

---

## 6. Stage 5 — Verdict and Truth-O-Meter Labelling

**Goal.** Translate the curator's internal six-category taxonomy
(`LIE`, `MISLEADING`, `BROKEN_DEADLINE`, `CREDIT_THEFT`,
`SHIFTING_NUMBERS`, `CONTRADICTION`) into two layers of public-facing
labels: an AVeriTeC verdict and a PolitiFact 6-rung score.

**Method.** A purely deterministic three-layer derivation
(ADR 0005), implemented in `kahzaabu/truth_score.py`:

```
(category, confidence)  →  AVeriTeC verdict_label
                         →  PolitiFact truth_score (1–6) + truth_score_label
```

The mapping has no LLM call. Rules are expressible as a small lookup
table; `LIE + confidence ≥ 0.95` resolves to `PANTS_ON_FIRE`, etc.
The verdict layer is `SUPPORTED` / `REFUTED` / `NOT_ENOUGH_EVIDENCE` /
`CONFLICTING_EVIDENCE` (AVeriTeC vocabulary).

**Justification.** A second LLM call would introduce non-determinism
into a pure mapping. Mathematical definition with unit-test ground
truth (the `tests/golden/truth_score/` set) is the right shape.

**Result.** 220 published fact-checks distributed as 41 `HALF_TRUE`,
179 `MOSTLY_FALSE`, 0 in the `TRUE` and `FALSE` extremes of the
ladder. The skew toward `MOSTLY_FALSE` reflects the curator's
selection bias — items are only created if there's at least a
moderate factual problem — and is consistent with the project's
position as a check on government communication, not a comprehensive
fact-coverage system.

---

## 7. Stage 6 — Web-Search Verification

**Goal.** Provide external corroboration or contradiction for
high-severity fact-checks.

**Method.** A Haiku 4.5 call per fact-check, with Anthropic's
`web_search_20250305` server tool granting bounded web access (≤4
searches per fact-check). The model classifies each retrieved citation
as `confirms` / `contradicts` / `context` / `unclear` /
`no_relevant_info`.

**Trust-tier integration.** Every evidence row's URL hostname is
matched against the public-sector entity registry (Section 9). Rows
on registered `.gov.mv` or `.com.mv` domains are tagged with the
registry's `entity_id` in a new `authoritative_entity_id` column.
Across the current corpus, 48 of 304 evidence rows (15.8%) are
tagged authoritative; the remainder are still ingested but classed
as secondary.

**Cost.** ~$0.03 + $0.01 per web search.

---

## 8. Stage 7 — ClaimReview JSON-LD Publication

**Goal.** Make published fact-checks discoverable by Google's Fact
Check Explorer and aligned with the schema.org `ClaimReview`
specification.

**Method.** Per ADR 0006, a `ClaimReview` JSON-LD payload is built at
fact-check publication time, embedding:

- the claim quote and date
- the speaker (defaulting to the Office of the President)
- the verdict label and Truth-O-Meter rung
- a mandatory disclaimer (automated analysis; verify against the
  original press release)
- the canonical source URL on `presidency.gov.mv`

The payload is cached in `fact_checks.claimreview_jsonld` and served
at two endpoints: `/api/factchecks/{id}/jsonld` (per fact-check) and
`/api/claimreviews/feed.json` (corpus-wide feed for the Fact Check
Markup Tool).

The disclaimer is mandatory by ADR 0006: every published payload
includes it irrespective of verdict, to honour the line between
automated analysis and finished journalism.

---

## 9. Cross-Cutting: External-Reference Trust Registry

A critical methodological question is *which sources count as
authoritative*. Our answer (ADR 0011) is a publicly-auditable registry
of 25 Maldivian public-sector entities — the Presidency, three
ministries, the legislature (Majlis), the judiciary, four independent
commissions, two regulators, five utilities, one airport operator,
one SOE, plus revenue, customs, immigration, police, and the oneGov
and eFaas digital portals.

The registry is stored as YAML (the human-editable contributor
surface) with a machine-loaded JSON twin; a parity test guards drift.
URL match is hostname-equality or strict-subdomain ancestry,
case-insensitive, with `www.` stripped. Hostnames matching a registered
domain receive the corresponding `entity_id` tag at evidence-insertion
time.

The registry is **an additive trust signal, not a filter**:
non-registered URLs are still ingested as evidence; they are simply
classed as secondary. This avoids the failure mode of
silently-suppressed third-party reporting while still making the trust
boundary visible to downstream consumers (the public-facing fact-check
page renders a "primary source" badge for tagged rows; the transparency
report aggregates by entity_type).

---

## 10. Cross-Cutting: Evaluation Methodology

### 10.1 Verified vs Pinned

A common failure mode in fact-checking benchmarks is conflating "the
system's output hasn't changed since last release" with "the system is
high-quality." We make the distinction explicit (ADR 0008): every
golden fixture under `tests/golden/<stage>/<id>.json` carries a
`verified: bool` field.

- `verified: true` — `expected` is hand-confirmed ground truth.
  Either mathematically determined (e.g., the deterministic
  `truth_score` mapping is fully specified in ADR 0005),
  structurally obvious (a matcher pair where both quotes share the
  same canonical claim ID), or human-reviewed.
- `verified: false` — `expected` was seeded from current pipeline
  output to act as a **drift detector**. A non-1.0 score after a
  prompt change means the LLM diverged from prior behaviour, but
  says nothing about which version is "correct."

The `kahzaabu eval` CLI reports both metrics side-by-side. The
verified-subset metric is the real quality signal; the all-fixture
metric guards against prompt drift between releases.

### 10.2 Current Coverage

| Stage | Fixtures | Verified |
|---|---|---|
| Truth-score (deterministic) | 6 | 6 |
| Matcher | 6 | 6 |
| Contradictions | 5 | 5 |
| Decomposer | 4 | 4 |
| Extractor | 4 | 3 (one taxonomy-ambiguous case held back) |
| Verifier | 8 | 0 (third-party-news relevance is subjective) |

**Verified-subset F1 across all stages: 1.000**, pinned to the
current pipeline baseline. Future prompt changes will surface
deltas in the verified subset as real quality regressions; deltas
in pinned-only stages flag drift for human review.

### 10.3 Reproducibility Manifest

For each published fact-check, the system can produce a complete
provenance trace (ADR 0010) via `/api/reproducibility/{id}.json` or
`kahzaabu reproducibility <id>`. The manifest joins the fact-check
row with: the curation run that produced it (model, prompt version,
token totals, cost), the supporting claims with their
extraction-run IDs and article titles, the decomposition questions,
the verification evidence with the trust-tier tags, the contradiction
pair (if applicable), the cached ClaimReview JSON-LD, and the git
commit hash at publication time. This provides per-fact-check
reproducibility in the AVeriTeC sense: a reader can re-derive every
step from the manifest.

---

## 11. Limitations

**Single-source corpus.** Our pipeline operates on one speaker's
output. Contradictions are detected *within* this source; comparison
against opposition statements, news media, or independent records
requires human follow-up. Multi-source extension is plausible (the
schema generalises) but out of scope for the current release.

**English-translation skew.** Press releases are typically published
in both Dhivehi and English; the LLM-extraction stage operates on
English only. A separate `dv_compare` stage flags translation
discrepancies between paired EN/DV documents but does not extract
DV-only claims into the structured tables. The corpus is therefore
biased toward what the press office chose to publish in English.

**Truth-O-Meter cultural mapping.** The PolitiFact ladder is a
US-political taxonomy adapted to the Maldives context. Subtle
differences in political-speech norms across cultures may make some
thresholds (especially `PANTS_ON_FIRE`) too strict or too lax. The
mapping in ADR 0005 is open to revision; the eval framework would
surface the change as a deterministic shift, not a model regression.

**Single-language constitutional cross-reference.** Our inline
Constitution corpus (301 articles) is a 2008-baseline English
translation. Constitutional amendments since 2008 are not
incorporated. The cross-reference is therefore a starting point for
human review, not authoritative.

**No statutory-law corpus.** We deliberately link out to
`old.mvlaw.gov.mv` (the Attorney General's Office archive) rather
than scrape statutory text (ADR 0012). The site invokes EU Directive
2019/790 Article 4 as an express reservation of rights for AI-input
use cases; we honour the reservation. This means fact-checks
referencing specific Acts can cite article numbers but not quote
bodies inline.

**Curator selection bias.** Fact-checks are created only when the
curator identifies a factual problem above a threshold. The resulting
corpus distribution skews toward `MOSTLY_FALSE` / `HALF_TRUE`,
because items toward `TRUE` rarely surface for review. This is
deliberate: kahzaabu is a check on government communication, not a
comprehensive fact-coverage system. Readers interested in the full
truth-spectrum should consult the unfiltered press releases via the
linked canonical URLs.

---

## 12. Ethics and Reproducibility Statement

The corpus is **publicly-published government output**. No private
data, no scraping of non-official sources, no use of paid-source
content that would conflict with publisher licences. The
`presidency.gov.mv` source is canonical and every fact-check links
back to the originating press release. A mandatory disclaimer is
embedded in the ClaimReview JSON-LD of every published fact-check.
Corrections can be filed through a public form (`/corrections`) which
populates an admin queue; corrections are processed by the project
maintainer with the same review workflow as fact-check publication.

The system is released under Apache-2.0. The reproducibility
manifest, evaluation set, ADRs, model card, data card, and
maintenance documentation are committed under `docs/`. Three Docker
variants (210 MB lean, 2.4 GB CPU-only, ~9 GB CUDA) reproduce the
build environment. A single command — `make eval` — re-runs the
quality evaluation against the committed golden set; a single
command — `make audit` — produces the bias / fairness report
referenced in ADR 0010.

---

## 13. Future Work

1. **Promote pinned fixtures to verified.** The verifier stage (8
   fixtures) and one extractor fixture are currently pinned; hand-
   reviewing the third-party-news relevance labels would grow the
   verified subset across all six stages.
2. **Multi-speaker extension.** The schema is multi-speaker-ready
   (`fact_checks.speaker` is already present); incorporating
   opposition or independent-commission statements is a matter of
   scraper extension + speaker disambiguation.
3. **Native-Dhivehi extraction.** A separate decomposer pass on
   `body_text_dv` would surface claims absent from the English
   re-presentation. Requires Dhivehi-tokeniser improvements in
   sentence-transformers.
4. **Active learning for fixtures.** When a `kahzaabu eval` run
   produces a low-F1 miss, the miss is a natural candidate for human
   review and promotion to a verified fixture. The infrastructure
   to expose misses is present in the `kahzaabu/eval.py` report; a
   CLI to walk misses interactively is the obvious next iteration.
5. **arXiv submission.** This document is the basis for a formal
   arXiv preprint. The next pass converts the Markdown to LaTeX,
   adds figure references for the pipeline diagram (currently in
   `docs/ARCHITECTURE.md`), and prepares the supplementary materials
   (evaluation set, anonymised manifests, model card).

---

## Acknowledgements

This work was developed in the open. Architectural decision records
(ADR 0001–0012) were reviewed throughout; the maintenance
infrastructure (CI workflows, drift detection, link-rot probing,
docker-variant matrix) was built collaboratively. Anthropic provided
Sonnet 4.6 and Haiku 4.5 inference; no other paid resources were used.

---

## References

Adair, B., Holan, A., Sharockman, A., et al. PolitiFact's Truth-O-Meter
methodology. *Tampa Bay Times*, 2007–. URL:
`https://www.politifact.com/article/2018/feb/12/principles-truth-o-meter-politifacts-methodology-i/`

Full Fact (2018). *Automated approach to detecting claims in political
discourse*. URL: `https://fullfact.org/about/automated/`

Hassan, N., Arslan, F., Li, C., and Tremayne, M. (2017). Toward
automated fact-checking: detecting check-worthy factual claims by
ClaimBuster. In *Proc. KDD*, pp. 1803–1812.
doi:10.1145/3097983.3098131

Khaliq, M. A. et al. (2024). RAGAR: Chain-of-RAG for political
fact-checking. *arXiv preprint arXiv:2404.12065*.

Schlichtkrull, M., Guo, Z., and Vlachos, A. (2023). AVeriTeC: a
dataset for real-world claim verification with evidence from the web.
In *Proc. EMNLP*. *arXiv:2305.13117*.

Thorne, J., Vlachos, A., Christodoulopoulos, C., and Mittal, A.
(2018). FEVER: a large-scale dataset for fact extraction and
verification. In *Proc. NAACL-HLT*, pp. 809–819.

Wang, W. Y. (2017). "Liar, liar pants on fire": a new benchmark
dataset for fake news detection. In *Proc. ACL*, pp. 422–426.

---

## BibTeX

```bibtex
@software{kahzaabu_2026,
  title  = {Kahzaabu: An Open-Source Pipeline for Automated Fact-Checking of a Single-Speaker Political Corpus},
  author = {Mohamed, Sofwathullah and contributors},
  year   = {2026},
  note   = {Apache-2.0; submitted to arXiv (preprint forthcoming)},
  url    = {https://github.com/<repo>/kahzaabu}
}

@inproceedings{schlichtkrull2023averitec,
  title     = {AVeriTeC: A Dataset for Real-world Claim Verification with Evidence from the Web},
  author    = {Schlichtkrull, Michael and Guo, Zhijiang and Vlachos, Andreas},
  booktitle = {EMNLP},
  year      = {2023}
}

@article{khaliq2024ragar,
  title         = {RAGAR: Chain-of-RAG for Political Fact-Checking},
  author        = {Khaliq, Mohammad Abdul and others},
  year          = {2024},
  eprint        = {2404.12065},
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

@inproceedings{wang2017liar,
  title     = {``Liar, Liar Pants on Fire'': A New Benchmark Dataset for Fake News Detection},
  author    = {Wang, William Yang},
  booktitle = {ACL},
  year      = {2017}
}
```

---

## Appendix A — Dataset and Pipeline Statistics (as of 2026-05-21)

| Metric | Value |
|---|---|
| Source articles, English | 14,125 |
| Source articles, Dhivehi | 6,686 |
| Total claims extracted | 9,876 |
| Claims with `is_checkable=1` | 8,502 |
| Canonical paraphrase groups | ~151 (1.7% repetition) |
| Q&A decomposition rows | 35,648 |
| Contradiction pairs scored | 48 |
| `CONTRADICTION` verdicts | 2 |
| Fact-checks published | 218 (of 220 total) |
| ClaimReview JSON-LD cached | 218 |
| Verification evidence rows | 304 |
| Authoritative-source evidence (registry-tagged) | 48 (15.8%) |
| Constitutional articles | 301 |
| Manifesto promises tracked | 717 |
| ADRs written | 12 |
| Tests passing | 283 |
| One-shot LLM spend (V2 build) | ≈ $16.50 |

---

## Appendix B — Architecture Decision Records

The full ADR list is committed under `docs/adr/`:

1. **0001** V2 architecture overview
2. **0002** Polarity taxonomy
3. **0003** Canonical claim matching
4. **0004** Contradiction verdict (4-way)
5. **0005** Dual labelling (AVeriTeC + PolitiFact)
6. **0006** ClaimReview JSON-LD
7. **0007** Embedding provider abstraction
8. **0008** Quality evaluation methodology (verified vs pinned)
9. **0009** OSS readiness (LICENSE, NOTICE, model and data cards)
10. **0010** Reproducibility, observability, audit CLIs
11. **0011** Public-sector entity registry
12. **0012** mvlaw.gov.mv: link-out, not scrape

---

*This is a draft. Section numbering, the abstract, and the figure
references will be adjusted in the LaTeX-conversion pass before arXiv
submission. Comments via the project's GitHub issue tracker or directly
to `Sofwathullah.Mohamed@gmail.com`.*
