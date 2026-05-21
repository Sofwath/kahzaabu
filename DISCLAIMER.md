# Disclaimer

**Kahzaabu is a sample / reference implementation of a Hermes Agent
plugin and fact-checking pipeline, built for educational and research
purposes.**

It is **not** a journalistic outlet, **not** a regulated fact-checking
organisation, and **not** an authoritative source of information about
the Maldives Presidency or any individual.

## What this means in practice

- **Verdicts are automated analysis, not findings of fact.** Every
  "fact-check", "verdict label", "truth score", and "contradiction"
  in this archive is the output of a probabilistic LLM pipeline.
  LLMs hallucinate, mis-extract numbers, conflate similar claims,
  and make category errors. The pipeline has a measured macro-F1
  in `docs/EVAL_RESULTS.md`; it is not 1.0.
- **Do not cite kahzaabu as a source.** Do not quote a Truth-O-Meter
  rating, a verdict label, or a "contradiction" from this archive
  as evidence in journalism, legal proceedings, academic writing,
  political argument, or social-media discourse. Cite the original
  press release on `presidency.gov.mv` directly.
- **The underlying press releases are the only authoritative
  material.** Kahzaabu's value is in *finding* claims worth a human
  fact-checker's attention, not in being one. Every fact-check
  payload links back to the originating article ID + URL on
  `presidency.gov.mv` for exactly this reason.
- **Constitutional citations are not legally binding.** The
  Constitution browser uses the 2008 Dheena Hussain functional
  English translation. The legally binding text is the Dhivehi
  original at `mvlaw.gov.mv`. The Constitution has been amended
  since 2008; the 2008 translation does not capture amendments.
- **No relationship with the Government of Maldives.** This is an
  independent open-source project. It is not endorsed by, affiliated
  with, or sponsored by the Presidency, the Government of the
  Republic of Maldives, or any ministry, commission, or SOE listed
  in the entity registry.
- **Read sources before drawing conclusions.** If you take an
  action — publish, share, quote, vote, comment — based on something
  you read in kahzaabu, the responsibility is yours to verify it
  against the primary source first.

## Intended audience

- **Hermes Agent plugin authors** studying how to build a domain-
  specific agent tool surface (9 in-process tools, 4-layer
  discovery cascade, manifest ↔ code regression-guards, etc.).
- **Researchers** studying automated fact-checking pipelines on
  small-state political corpora — kahzaabu's contribution to the
  literature is a worked example of AVeriTeC + RAGAR + Full Fact
  + PolitiFact + ClaimReview JSON-LD assembled into one open-
  source stack (see `docs/paper/kahzaabu-methodology.md`).
- **Civic-tech maintainers** in other small jurisdictions evaluating
  the reference architecture before adapting it to their own
  press-corpus.

## Not the intended audience

- **General members of the public** looking for a "did the President
  lie?" oracle.
- **Journalists** who need a primary source on a claim.
- **Lawyers, regulators, or compliance reviewers** who need
  authoritative facts.

If you fall in this second group, treat kahzaabu as a *pointer* to
material on `presidency.gov.mv` that may merit your attention. The
verification is your job.

## Bug reports and corrections

Mistakes in the archive are expected, not exceptional. The project
ships a corrections workflow:

- Use the **Report a correction** form in the web UI (footer link
  on every page).
- Or open an issue on GitHub at
  <https://github.com/Sofwath/kahzaabu/issues>.

Submit the article ID + the specific claim and what's wrong with
the verdict. The operator reviews corrections via the CLI and
re-runs the relevant pipeline stage; corrections do **not** alter
the original press release record.

## License

Apache-2.0. You may use, modify, and redistribute the code; you
may NOT misrepresent the source. See `LICENSE`.
