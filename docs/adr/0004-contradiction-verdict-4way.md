# ADR 0004 — Contradiction verdict — 4-way

**Status**: Accepted (2026-05-21)

## Context

The user's stated headline priority for V2 is a "real contradiction detector": pair claims of opposite polarity on the same subject and flag the contradiction. The naive design is binary — either the pair contradicts or it doesn't. That's wrong for a fact-checking project that wants to be cited as credible.

A binary verdict treats two situations identically:

- "On Jan 1, Muizzu said the airport will be done in 30 months. On Jun 1, he said it'll take 60 months." — a deadline slip, possibly a broken promise, depending on whether external conditions changed.
- "On Jan 1, Muizzu said the army will not be deployed in X. On Jun 1, the army was deployed in X." — a hard contradiction with no defensible reframing.

Conflating these as "contradiction" gives ammunition to bad-faith critics on both sides. The kahzaabu project's goal is automated, defensible, citable analysis — not partisan accumulation. We need a verdict layer that distinguishes intent from circumstance.

## Decision

`contradiction_pairs.verdict` is one of four values, applied by an LLM verifier after polarity-pair shortlisting:

| Verdict | Meaning | Example |
|---|---|---|
| `CONTRADICTION` | Logical contradiction between claims; no plausible external explanation. The speaker has either changed position without acknowledgment OR made a false statement. | "We will not deploy the army" → army deployed within 6 months, no acknowledgment of the prior statement. |
| `EVOLVING_POSITION` | The speaker has changed position AND acknowledged the change (in the second statement or in surrounding context). Honest revision. | "We previously projected 12,000 flats by 2025, but given economic conditions we now target 9,000 by 2026." |
| `CONTEXT_CHANGED` | External facts shifted in a way that defensibly justifies the new position. Includes natural disasters, court rulings, IMF conditions, etc. Pair preserved for transparency, not flagged as misconduct. | "We will not raise taxes" → "Following the IMF agreement we will raise GST by 1%". |
| `NOT_CONTRADICTORY` | Polarity-pair shortlist false positive. The pair *looks* opposite but, on inspection, addresses different subjects, time windows, or scopes. | "We won't deploy combat troops" → "We deployed disaster-relief troops to the same island." |

Every contradiction_pair carries a `reasoning_chain` (JSON) explaining the verdict. For `CONTEXT_CHANGED`, the chain must cite the external fact that justifies the shift (court ruling, IMF letter, dated press release from another organization, etc.). For `EVOLVING_POSITION`, the chain must quote the acknowledgment.

Only `CONTRADICTION` produces a published fact-check. The other three categories are persisted for transparency and can be queried, but they don't accumulate as "lies".

## Alternatives considered

- **Binary (`CONTRADICTION` / `NOT_CONTRADICTORY`).** Simpler. Rejected — see Context above.
- **5-way adding `STRATEGIC_AMBIGUITY`** (deliberately vague statement that allows later reframing). Rejected — separating intentional ambiguity from honest revision requires reading the speaker's mind. The LLM cannot do this reliably; humans struggle too.
- **Numerical severity (1–5).** Rejected — collapses categorically distinct situations into a single axis. A `CONTEXT_CHANGED` shouldn't be "less severe" than a `CONTRADICTION`; it's a different category.

## Consequences

**Positive.**

- Defensible — researchers and journalists citing kahzaabu can point to the 4-way verdict and the reasoning chain.
- Catches honest political evolution as such, which is what good democratic discourse expects.
- The published fact-checks layer narrows automatically to genuinely problematic contradictions.

**Negative.**

- Adds a second LLM call to the contradiction pipeline (polarity shortlist → verdict classification). Cost: ~$0.05 per shortlisted pair. With ~30 pairs/month expected, that's $1.50/month.
- The `CONTEXT_CHANGED` verdict requires the LLM to evaluate external context. For early-stage operation we can fall back to "if the LLM is uncertain, label `CONTRADICTION` and let a human reviewer override" — the `confidence` column captures this uncertainty for triage.
- Public-facing UX must teach the user the distinction between the four labels. A short legend on the `/contradictions` page.

## Schema

```sql
CREATE TABLE contradiction_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_a_id INTEGER NOT NULL REFERENCES claims(id),
    claim_b_id INTEGER NOT NULL REFERENCES claims(id),
    subject TEXT NOT NULL,
    verdict TEXT NOT NULL
        CHECK(verdict IN ('CONTRADICTION','EVOLVING_POSITION',
                          'CONTEXT_CHANGED','NOT_CONTRADICTORY')),
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    reasoning_chain TEXT NOT NULL,    -- JSON: [{question, answer, source}, ...]
    published INTEGER DEFAULT 0,
    reviewed_at TEXT,
    reviewed_by TEXT,
    detected_at TEXT NOT NULL,
    UNIQUE(claim_a_id, claim_b_id)
);
CREATE INDEX idx_contra_subject ON contradiction_pairs(subject);
CREATE INDEX idx_contra_verdict ON contradiction_pairs(verdict);
CREATE INDEX idx_contra_published ON contradiction_pairs(published);
```
