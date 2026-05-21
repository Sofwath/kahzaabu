# Constitution of the Republic of Maldives — provenance notes

## Source

- **File**: `ConstitutionOfMaldives.pdf` (459,686 bytes)
- **URL**: <https://storage.googleapis.com/presidency.gov.mv/Documents/ConstitutionOfMaldives.pdf>
- **Downloaded**: 2026-05-21
- **PDF metadata**: Creator `PScript5.dll Version 5.2.2`; CreationDate / ModDate `2008-08-10` (Acrobat Distiller 6.0)
- **Pages**: 129
- **Translator**: Ms. Dheena Hussain, LLB (Birmingham), LLM (London), Barrister-at-Law (Lincoln's Inn)
- **Commissioner**: Ministry of Legal Reform, Information and Arts
- **Self-described status**: "Functional translation"

## What "functional translation" means

This is **not** an official legal text. The legally binding constitution of the Maldives is the **Dhivehi original**. A functional translation is intended for working/reference use and may differ from the official text in:

- Word choice (translator's interpretation of legal terms)
- Sentence structure
- Inclusion or omission of marginal annotations

For any legal-stakes use, refer to the official Dhivehi text published by the Majlis or the Attorney General's office.

## Amendments since 2008

The 2008 Constitution has been amended on several occasions. The PDF in this repo is the **2008 baseline only** and does NOT include subsequent amendments.

Known amendments (non-exhaustive — verify against the official record before relying on any specific list):

- Constitutional Court rulings have interpreted several provisions in ways that effectively modify their operation (e.g., judicial appointment process, presidential term arithmetic).
- The Maldives has experienced several political crises (2012 transfer of power, 2018 declaration of emergency, others) that produced amendments or interpretive changes.
- Specific articles affected by post-2008 amendments are NOT tracked in this archive.

**Implication for kahzaabu users**: when the agent cites an article on a time-sensitive matter (presidential succession arithmetic, judicial appointments, emergency declarations, etc.), the citation is to the 2008 text. The current law may be different. The agent's SYSTEM_PROMPT and the lookup tool's response both include a disclaimer; do not strip it from public outputs.

## Where to verify

- **Official Maldivian legal portal**: <https://www.majlis.gov.mv> (Majlis — legislative body)
- **Attorney General's Office**: <https://www.agoffice.gov.mv>
- **Supreme Court rulings** on constitutional interpretation are the binding authority for ambiguous text.

## How this file is used

`kahzaabu/constitution.py:parse_constitution()` reads the sibling `.txt` file (produced by `pdftotext -layout`) and writes ~301 article records into the `constitution_articles` table. The agentic Q&A loop and the `kahzaabu_constitution_lookup` plugin tool query this table via SQLite FTS5 (BM25-ranked) with a LIKE fallback for SQLite builds without FTS5.

Every tool response carries a `disclaimer` field reminding callers of the translation + amendment caveats above. **Do not strip the disclaimer when surfacing constitutional citations to end users.**
