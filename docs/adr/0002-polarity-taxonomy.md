# ADR 0002 — Polarity taxonomy (6 labels)

**Status**: Accepted (2026-05-21)

## Context

To detect contradictions automatically, claims must carry stance/polarity at extraction time. Without it, "we will not raise taxes" and "we have raised taxes" look identical to the LLM at curation time — distinguishing them requires deep semantic comparison every time, rather than a cheap polarity-pair lookup. None of the reference systems (FEVER, AVeriTeC, RAGAR) define a polarity taxonomy because they don't track contradictions over time; this is kahzaabu-specific.

The trade-off is granularity vs. LLM reliability. Fewer labels → easier for the LLM to apply consistently → coarser contradiction-pair detection. More labels → richer downstream queries → more LLM disagreement on edge cases.

## Decision

Six labels, applied by the LLM at extraction time:

| Label | Definition | Example |
|---|---|---|
| `AFFIRM` | Asserts that something IS, will be, or has been the case. | "We are building 5,000 housing units." |
| `DENY` | Asserts that something is NOT, will not be, or has not been the case. | "We will not raise taxes." |
| `PROMISE` | Future-tense commitment with a target (numeric, dated, or both). | "We will deliver 12,000 flats by end of 2025." |
| `DENIAL_OF_PROMISE` | Explicit disavowal of a previously-asserted commitment. | "I never promised that 12,000 figure." |
| `CLAIM_OF_FACT` | Past or present factual assertion not tied to the speaker's own action. | "The economy grew 4% last year." |
| `NEUTRAL` | Ceremonial / rhetorical / acknowledgement; no checkable content. | "I thank the people of Gulhi for their hospitality." |

`PROMISE` and `DENIAL_OF_PROMISE` are split from the general `AFFIRM` / `DENY` pair because the kahzaabu corpus's most common contradiction class — broken deadlines and shifting numbers — lives in the promise / denial-of-promise axis specifically. Collapsing them into `AFFIRM`/`DENY` makes downstream queries about "promises he later disavowed" require expensive text-search rather than a column lookup.

Few-shot prompting is mandatory: the extractor's LLM call gets one labeled example per polarity. Without examples, label-stability falls sharply at the boundary between `AFFIRM` and `CLAIM_OF_FACT`.

## Alternatives considered

- **3-label (`AFFIRM` / `DENY` / `NEUTRAL`).** Simpler, easier for the LLM. Rejected — `PROMISE` and `DENIAL_OF_PROMISE` carry deadline / target information that `AFFIRM` / `DENY` lose. The kahzaabu corpus has 115 `BROKEN DEADLINE` and 30 `SHIFTING NUMBERS` fact-checks; both are promise-class. Losing that signal at the polarity layer means rediscovering it every time downstream.
- **NLI-style (entailment / contradiction / neutral).** Comes from FEVER. Rejected because it labels claim-pairs, not single claims; it's the wrong primitive for a "tag at extraction time, match at find-contradictions time" architecture.
- **Continuous (-1 to +1).** Used by some sentiment-analysis pipelines. Rejected because contradictions need crisp opposite labels, not a real-valued spectrum.

## Consequences

**Positive.**

- The `find_contradictions` stage can shortlist candidate pairs by cheap SQL (`polarity IN ('AFFIRM','PROMISE') JOIN polarity IN ('DENY','DENIAL_OF_PROMISE')`) before any LLM verification.
- Manifesto cross-referencing gets the `PROMISE` label for free, replacing today's heuristic detection.
- Filter `NEUTRAL` claims out of fact-check curation entirely — saves LLM cost on ceremonial speech.

**Negative.**

- Six labels means the LLM has more room to be inconsistent. We will see drift between extraction runs. The verifier stage's pair-confirmation LLM acts as a safety net.
- Backfilling polarity over the 9,000 existing claims is part of the V2 backfill spend.
- A future ADR may collapse labels if data shows the granularity isn't being used downstream.
