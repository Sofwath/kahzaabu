---
name: kahzaabu-fact-check
description: "Fact-check any political claim against kahzaabu's archive of Maldives Presidency press releases. Returns a structured verdict (AVeriTeC scheme), a PolitiFact-style Truth-O-Meter rating, a RAGAR-style reasoning chain, and citations to the archive. Open-source pipeline; runs entirely against pre-built data."
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [kahzaabu, fact-checking, civic-tech, maldives, claim-verification]
    related_skills: [kahzaabu-self-improver]
---

# kahzaabu-fact-check

## Overview

Fact-check an arbitrary political claim about the Maldives Presidency by routing it through kahzaabu's pre-built pipeline (3,099 press releases, 8,954 extracted claims, 35,648 verification questions, 218 published fact-checks, 301 constitution articles indexed).

The output is structured: AVeriTeC verdict (`SUPPORTED` / `REFUTED` / `NOT_ENOUGH_EVIDENCE` / `CONFLICTING_EVIDENCE`) + PolitiFact 1-6 truth score with public-readable label + RAGAR-style reasoning chain + cited source articles.

## Inputs

A single claim string. Examples:
- "Muizzu promised 12,000 housing flats by end of 2025"
- "Did the President interfere with the judiciary?"
- "What's the government's position on the One-China Principle?"
- "Has Muizzu's administration delivered on the manifesto's renewable-energy targets?"

## What this skill does

1. Calls `kahzaabu_ask` with the input claim. That tool runs an internal multi-turn LLM loop with 9 sub-tools (archive search, manifesto lookup, constitution lookup, web search, etc.).
2. Receives a structured response: the synthesized answer, citations to source articles, a Truth-O-Meter rating (when applicable), and a "🎭 Narrative tricks observed" section listing PR/framing techniques visible in the source text.
3. Optionally cross-references the constitution via `kahzaabu_constitution_lookup` for rights / judicial / religious claims.
4. Optionally queries `kahzaabu_contradictions_about` to surface any related machine-detected contradictions.
5. Returns a clean, citable answer with the kahzaabu-standard disclaimer.

## What this skill does NOT do

- It does NOT update the corpus. The pipeline runs on a 12-hour cron; live re-scraping requires `kahzaabu_pipeline_run` (gated by `KAHZAABU_MCP_ALLOW_PIPELINE=1` env var).
- It does NOT render legal opinions. Constitutional citations point at text; interpretation is the Supreme Court's role.
- It does NOT publish corrections. Use `/corrections` on the kahzaabu web UI for that.
- It does NOT run on a non-Maldives corpus. The schema is portable but the data is Maldives-specific.

## Output format

Always emit Markdown in this shape:

```
## <one-line summary of the verdict>

**Truth-O-Meter**: <1-6>/6 <LABEL>  •  **Verdict**: <SUPPORTED|REFUTED|...>  •  **Confidence**: <low|medium|high>

<2-4 paragraph synthesis with inline citations [NNNNN] = article id, [FC #N] = fact-check id, [Const. Art. NN] = constitution article>

### 🔎 How we verified this

1. <reasoning step — what we checked + what we found>
2. <reasoning step>
3. <reasoning step>

### 📰 Sources

- [NNNNN] <article title> (<date>) — presidency.gov.mv
- ...

### 🎭 Narrative tricks observed

<only when source text contains framing patterns; omit otherwise>

---

*Automated analysis pipeline — kahzaabu. Not legal advice. Verify the original press release before quoting.*
```

## When to use which tool

| User asks… | Start with |
|---|---|
| Any natural-language fact-check question | `kahzaabu_ask` (the agentic loop handles routing) |
| "Did he say X on date Y?" | `kahzaabu_get_article` for the specific date's PR |
| "What's the constitutional take on X?" | `kahzaabu_constitution_lookup` |
| "Are there contradictions about X?" | `kahzaabu_contradictions_about` |
| Browsing fact-checks by category | `kahzaabu_list_lies` |

For most questions, just call `kahzaabu_ask` and let its internal loop pick.

## Performance + cost

- Typical cost per invocation: **$0.03–$0.10** (Sonnet 4.6 for the main reasoning, Haiku 4.5 for the narrative-tricks pass).
- Typical latency: **5–15 seconds** end-to-end (multiple internal tool calls in sequence).
- Daily budget cap is enforced at `$5/day` by default (configurable via `KAHZAABU_DAILY_BUDGET_USD`).
- Session continuity: pass `session_id` back on follow-up calls to retain context — the agent recalls prior questions and tool results, so follow-ups are cheaper.

## Disclaimers (mandatory, do not strip)

Every output MUST carry:

1. **Automated-analysis disclaimer**: "Automated analysis pipeline — kahzaabu. Not legal advice."
2. **Constitution-translation caveat** (when citing the Constitution): "Uses the 2008 Dheena Hussain functional translation. Legally binding text is the Dhivehi original."
3. **Data-freshness note** (when the archive is older than 24 hours): "Archive may be missing recent items; run `hermes kahzaabu update` to refresh."

The kahzaabu_ask tool surfaces these automatically; just ensure they reach the final output unmodified.

## Example invocation

```
> kahzaabu-fact-check "Muizzu promised to plant 5 million trees — has the government delivered?"
```

→ Agent calls `kahzaabu_ask` with the claim → receives synthesized answer with [article ids], Truth-O-Meter rating, narrative-tricks section, and sources → returns the formatted Markdown.

## Open source

Source code: this skill is part of the **kahzaabu** project — released under **Apache-2.0** at the project repository (currently single-developer, V2 build in flight). The architecture is portable: same pipeline could fact-check any executive office's press release archive in any country. Only the corpus and the constitution are Maldives-specific.

References:
- **AVeriTeC** (EMNLP 2023) — verdict labels + evidence model
- **RAGAR** (arXiv 2404.12065) — Chain-of-RAG reasoning
- **Full Fact AI workflow** — canonical claim matching
- **PolitiFact** — Truth-O-Meter
- **schema.org ClaimReview** — discoverability
