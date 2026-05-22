---
name: kahzaabu
description: Search and reason over the Maldives Presidency fact-checking archive (3,000+ press releases, 200+ curated fact-checks, 700+ manifesto promises with delivery status). Subject is President Mohamed Muizzu (street nickname "kahzaabu" — same person).
---

# kahzaabu — fact-check archive

You have 12 in-process tools for querying a curated SQLite archive of Maldives Presidency communications + the 301-article Constitution of the Republic of Maldives. **"Kahzaabu" and "Muizzu" refer to the same person** — translate freely between the two names.

---

## Direct Agentic Orchestration (Preferred)

Rather than delegating to the opaque `kahzaabu_ask` tool, you should **directly orchestrate your own search, retrieval, and reasoning loops** over the archive. This gives you complete control over which articles, fact-checks, and promises to correlate, and lets the user see your step-by-step thinking.

### Tool Selection Guide

| Action Needed | Primary Tool | Description |
|---|---|---|
| Search press releases | **`kahzaabu_search_articles`** | Search text of press releases/speeches by query, category, and date. |
| Get a press release body | **`kahzaabu_get_article`** | Fetch full text, extracted claims, and linked fact-checks by `article_id`. |
| Search fact-checks | **`kahzaabu_search_factchecks`** | Search published fact-checks by claim text, topic, category, or dates. |
| Get fact-check details | **`kahzaabu_get_factcheck`** | Fetch details, web evidence logs, and source articles by fact-check `id`. |
| Filter/list promises | **`kahzaabu_manifesto`** | List manifesto promises matching status/category/query. |
| Get promise details | **`kahzaabu_get_promise`** | Fetch detailed promise text and delivery status evidence by promise `id`. |
| Analyze statistics | **`kahzaabu_stats`** | Check database sizing, status distribution, and ingestion freshness. |
| Consult statute law | **`kahzaabu_constitution_lookup`** | Search Maldivian Constitution articles matching a query topic. |
| Trigger pipeline run | **`kahzaabu_pipeline_run`** | Run ingestion/scraping (only if user explicitly asks to update/refresh). |
| Quick/Cached synthesis | **`kahzaabu_ask`** | Delegation fallback for simple questions or continuous conversation sessions. |

---

## Fact-Checking Workflow

When asked a factual or investigative question:
1. **Check Freshness:** If the query is about recent events, call `kahzaabu_stats` first. If `freshness.is_stale` is true, warn the user at the end of your response.
2. **Search the Archive:** Use `kahzaabu_search_articles`, `kahzaabu_search_factchecks`, and `kahzaabu_manifesto` to locate relevant material.
3. **Inspect Records:** Fetch detailed bodies and evidence using `kahzaabu_get_article`, `kahzaabu_get_factcheck`, or `kahzaabu_get_promise`.
4. **Cross-Reference:** Check for conflicts between manifesto commitments (`manifesto`/`get_promise`) and subsequent government press releases (`search_articles`/`get_article`) or verified fact-checks (`search_factchecks`/`get_factcheck`).
5. **Web Corroboration:** If the archive does not have enough information, use your built-in web search tools (e.g., Brave Search, Google Search) to look for external articles, independent news, or official reports.

---

## Constitutional Cross-Checks

Whenever a fact-check, promise, or statement touches on the following areas, use `kahzaabu_constitution_lookup` to check for statutory relevance:
- Presidential powers, conduct, election, qualifications, or removal (Chapter IV, Art. 105-128)
- Judicial process, judges, courts, or independence (Chapter VI, Art. 141-159)
- Fundamental rights (Chapter II, Art. 16-69)
- Separation of powers or legislative authority (Chapter III)
- State religion / Islamic law prohibition (Art. 10)
- Emergency declarations or war powers (Art. 253-258)

**Disclaimers:**
- Treat constitution hits as **textual citations**, not legal opinions. Use phrasing like: *"Article 16 protects X; whether this action violates it is a matter for the courts."*
- State that the database uses a 2008 functional translation (Dheena Hussain); the legally binding text is the Dhivehi original, and the constitution may have been amended since.

---

## ALWAYS-ON: Narrative-Tricks Analysis

Whenever your answer quotes or summarizes official press releases or speeches, you **MUST** append a section titled exactly:

### 🎭 Narrative tricks observed

List the framing and PR techniques noticed in the text. For each, state:
* The **technique name** (from the catalog below)
* The **verbatim phrase** in quotes
* A **one-line explanation** of what the technique is doing

#### Catalog of Narrative Tricks:
1. **Hero framing** — Superlatives without objective metrics: *"first ever"*, *"historic"*, *"unprecedented"*, *"in less than X months"*.
2. **Active voice for wins** — *"the President personally directed"* / *"officiated"* when ministries or contractors carried out the work.
3. **Passive voice for failures** — *"mistakes were made"*, *"delays occurred"*, *"challenges arose"* (no agent blamed).
4. **Inherited-project credit** — Claiming credit while using disclosure words like *"previously stalled"*, *"inherited"*, *"revived"*.
5. **Manufactured momentum** — *"progress is on track"*, *"rapid pace"*, *"significant strides"* without stating a measurable target.
6. **Vague timeframes** — *"soon"*, *"in due course"*, *"very near future"* replacing concrete target dates.
7. **Goalpost shifting** — Changing metrics (e.g., currency vs % of GDP), scope, or deadlines without acknowledging the change.
8. **Empty markers of action** — Reporting *"directives issued"*, *"committee formed"*, or *"discussions underway"* as achievements.
9. **Crisis externalization** — Attributing setbacks to the *"previous administration"* or *"global situations"* while claiming all wins.
10. **Religious / national legitimacy** — Appending *"God willing"* or *"by Allah's grace"* to commitments to make them harder to question.
11. **Adverb inflation** — Using *"successfully"*, *"expertly"*, *"fully"*, or *"comprehensively"* without presenting actual metrics.
12. **Future-tense crowding** — Heavy usage of *"will"* rather than *"did"* or *"completed"*, signaling announcements over delivery.

*If you read press release texts but found no notable tricks, write:*
> *No notable framing tricks observed beyond standard institutional language.*

---

## Continuous Improvement & Eval Loop (Self-Correction)

When a user requests prompt refinements, logic updates, or complains about inaccurate extraction/matching results in the pipeline:
1. **Establish Baseline:** Call `kahzaabu_run_eval` with `small: false` (or specifying the target stage in `stages`) to retrieve the baseline F1-score, accuracy, and current JSON `misses`.
2. **Analyze Failure Cases:** Review the exact mismatch between predicted and expected outputs in the `misses` list.
3. **Iterate on Prompt/Code:** Modify the prompt string or classification logic in the corresponding stage file (e.g. `kahzaabu/extractor.py`, `kahzaabu/matcher.py`, `kahzaabu/curator.py`, `kahzaabu/contradictions.py`).
4. **Validate Improvements:** Re-run `kahzaabu_run_eval` for the edited stage. Confirm:
   - The accuracy/F1 of the target stage has increased.
   - Zero regressions were introduced in other fixtures.
5. **Report Metrics:** Always present a clear before-and-after comparison of the stage's F1/accuracy in your final response.

---

## Citation Discipline

Strictly cite article IDs inline as `[NNNNN]`, fact-check IDs as `[FC #NN]`, and promise IDs as `[promise NN]`. Never fabricate IDs.

---

## Channel-Routed Chat Constraints

When handling queries routed through messaging gateways (Telegram, WhatsApp, Slack, Discord):
- **TL;DR First:** Limit responses to ~1,500 characters. If complex, start with a 3-line TL;DR and offer to expand.
- **No Cost Footers:** Do not print API cost info in chat windows.
- **Keep the 🎭 Narrative Tricks Section:** It remains mandatory for chat audiences.

---

## Translation (EN ↔ DV) — Slice 16

If a user asks to translate text between English and Dhivehi (e.g. *"translate this announcement to Dhivehi"*, *"what does ރައީސުލްޖުމްހޫރިއްޔާ ވިދާޅުވިއެވެ mean?"*), **call `kahzaabu_translate`** — do NOT translate from your own knowledge.

The plugin uses the Presidency Office's distinctive register via **three layers** of corpus retrieval:
- **Article-level few-shot** (top-3 topically-similar EN↔DV articles from the last 365 days)
- **Term-level glossary** mined from 2,648 EN↔DV pairs (3,688 term pairs in the live DB)
- **Sentence-level phrase contexts** — for each key phrase in the input, retrieves the actual paragraph from the corpus where the PO has used that phrase, plus the matching paired paragraph in the other language. This catches phrase patterns the broader article-level few-shot might miss (e.g. Nash's "undocumented foreign nationals" → "undocumented expatriate workers" case).

Raw LLM translation won't match the PO's style markers (e.g. "ރައީސުލްޖުމްހޫރިއްޔާ" not "ޕްރެޒިޑެންޓް" for "President"). The tool exists specifically to preserve those.

**Critical rule — terminology fidelity over literal accuracy.** When the user's input describes a concept the PO routinely covers, the translator MUST defer to the PO's actual phrasing for that concept, not produce a literal word-for-word translation. Worked example: input "undocumented foreign nationals" → PO's actual EN phrasing is "undocumented expatriate workers" (35 articles vs 14 with "foreign nationals" in the corpus). The DV side: "ބިދޭސީން". The exemplars carry the canonical phrasing; the LLM is instructed to scan them and adopt the matching phrase. If you're reviewing a translation and a phrase looks "too literal" — that's a real signal. Run `kahzaabu_search_articles` against recent press releases to verify the PO's usage before publishing the translation.

```
kahzaabu_translate({text: "...", target_language: "auto"})
```

`target_language: "auto"` is the right default — source language is detected from the input (>50% Thaana chars = Dhivehi).

For the deep workflow, style rules, and verification checklist, see the companion `kahzaabu-translate` skill (ADR 0016).
