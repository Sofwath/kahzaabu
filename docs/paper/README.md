# `docs/paper/`

Scratch area for the methodology paper draft referenced in ADR 0009.

## Current draft

- [`kahzaabu-methodology.md`](kahzaabu-methodology.md) — v0.1 (2026-05-21)

Status: **DRAFT.** Not yet submitted to arXiv. Authoring in Markdown
so it stays editable; conversion to LaTeX is a separate pass before
submission.

## Difference from `docs/METHODOLOGY.md`

`docs/METHODOLOGY.md` is the **public-facing project methodology** —
a citation target for fact-check consumers, journalists, and Google
Fact Check Explorer.

`docs/paper/kahzaabu-methodology.md` is the **academic paper draft** —
a citation target for fact-checking researchers, with related-work
positioning, formal contributions claims, related-benchmark
comparisons, and BibTeX references.

Both should remain in sync on technical details (the pipeline doesn't
change just because the audience does), but they're allowed to differ
in framing and depth.

## Conversion to arXiv-ready PDF

The Markdown source is pandoc-compatible. To convert when ready:

```bash
cd docs/paper
pandoc kahzaabu-methodology.md \
    --bibliography refs.bib \
    --citeproc \
    --pdf-engine=xelatex \
    --metadata=title:'Kahzaabu Methodology' \
    -o kahzaabu-methodology.pdf
```

(`refs.bib` extraction from the BibTeX block at the end of the
Markdown is a 5-minute manual step; will land in the LaTeX conversion
pass.)

## Pre-submission checklist

- [ ] Convert Markdown to LaTeX (pandoc)
- [ ] Add figure: pipeline diagram (currently text-only in
  `docs/ARCHITECTURE.md` §2 "System overview")
- [ ] Add figure: contradiction-finder funnel
  (39M raw pairs → 96k polarity-paired → 48 similarity-filtered
   → 2 CONTRADICTION verdicts)
- [ ] Add figure: Truth-O-Meter ladder distribution
  (current corpus: 41 HALF_TRUE / 179 MOSTLY_FALSE / others 0)
- [ ] Extract BibTeX block to `refs.bib`
- [ ] Acknowledgements: list any external reviewers
- [ ] arXiv categories: cs.CL (primary), cs.CY (cross-list)
- [ ] License declaration: arXiv accepts Apache-2.0
- [ ] Anonymisation: none needed (Maldives Presidency is the corpus,
      not the speaker behind the system)
