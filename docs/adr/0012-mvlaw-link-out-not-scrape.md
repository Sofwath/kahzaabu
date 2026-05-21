# ADR 0012 — mvlaw.gov.mv: link-out, not scrape

**Status**: Accepted (2026-05-21)

## Context

The kahzaabu project's corpus is Office of the President press releases.
Many fact-checks reference statutory law (Constitution articles,
ratified Acts, regulations) that kahzaabu has limited visibility into.
The canonical source for Maldivian statutory law is
`https://old.mvlaw.gov.mv/` — published by the Attorney General's
Office.

The natural product instinct is to scrape mvlaw.gov.mv into the
kahzaabu corpus alongside the press-release archive, so that:

- The agentic Q&A can ground answers in actual statute text
- Fact-checks citing "Article 158(h)" can render a tooltip with the
  article body
- The reproducibility manifest can carry the statutory cross-reference
  end-to-end

But mvlaw.gov.mv's `robots.txt` contains only the explanatory header
of the Cloudflare content-signals protocol followed by:

> ANY RESTRICTIONS EXPRESSED VIA CONTENT SIGNALS ARE EXPRESS
> RESERVATIONS OF RIGHTS UNDER ARTICLE 4 OF THE EUROPEAN UNION
> DIRECTIVE 2019/790 ON COPYRIGHT AND RELATED RIGHTS IN THE
> DIGITAL SINGLE MARKET.

No actual `User-agent: *` directive is present, and no explicit
`Allow:` / `Disallow:` rules. The content-signals (search / ai-input /
ai-train) are likewise not given explicit yes/no values. The protocol's
own semantics state that absent signals "neither grant nor restrict
permission." Combined with the EU 2019/790 reservation language at
the bottom, the responsible interpretation is that the site operator
has invoked the directive's rights-reservation mechanism for AI input
and training.

Kahzaabu is exactly that use case: every pipeline stage (extractor,
decomposer, curator, verifier, contradictions, inspector, dv-compare,
agentic Q&A) is an LLM-call site that takes its input text and feeds
it to a language model.

## Decision

**Do not scrape mvlaw.gov.mv. Link out to it.**

Specifically:

1. Ship a `/laws` page in kahzaabu's web UI that:
   - Lists the 5 canonical mvlaw.gov.mv sections (Constitution, Laws,
     Cancelled Laws, Regulations, Publications) with bilingual
     English + Dhivehi labels
   - Has a search box that constructs a `site:old.mvlaw.gov.mv`
     Google search URL and opens it in a new tab (Google's own
     index — no content is fetched, parsed, or stored by kahzaabu)
   - Carries explicit attribution to the Attorney General's Office
   - Includes a short "why we link out, not import" explainer
     citing this ADR
2. No `/api/laws/...` endpoint that returns mvlaw.gov.mv content.
3. No backend HTTP requests to mvlaw.gov.mv from kahzaabu code (no
   scraper module, no cron job, no corpus row).
4. Kahzaabu's existing `data/constitution/` artefact (parsed from a
   one-time text dump committed by the project owner) remains — that
   import happened pre-ADR-0012 and is documented in
   `kahzaabu/constitution.py`. No additional content imports.
5. If a future fact-check needs the body of a specific law, the
   author follows the link, copy-pastes the relevant section into
   the fact-check's `evidence_quotes` JSON field with explicit
   attribution. This is the same pattern used today for any other
   external quotation; no automation.

## Alternatives considered

- **Scrape under fair-use claim.** Rejected — the corpus would have
  to be re-litigated every time the AGO updates their site. The
  EU 2019/790 reservation is express, not inferred; we should
  honor it.
- **Get AGO permission first.** Reasonable, deferred. The user can
  pursue this independently; if granted in writing, ADR 0013 can
  supersede this decision and a scraper can land. The contact email
  is on every mvlaw.gov.mv page.
- **iframe mvlaw.gov.mv inside kahzaabu pages.** Rejected — the
  site's headers may block framing, and even if they didn't, an
  iframe is functionally a fetch + render which still triggers
  publisher concerns. A new-tab link out is cleaner.
- **Use mvlaw.gov.mv's own search box, not Google.** They don't
  publicly document a search URL pattern. Adopting an undocumented
  pattern would be brittle and uses their bandwidth on every
  search; Google's site-search shifts that cost to Google's index.
- **Index mvlaw.gov.mv ourselves with our own embeddings.** Same
  scrape problem just one level removed.

## Consequences

**Positive.**

- Kahzaabu honors the AGO's express EU 2019/790 reservation. The
  project's posture toward third-party rights-holders is documented
  and consistent (cf. ADR 0011 for public-sector domains, ADR 0009
  for OSS licensing).
- The user-facing experience is cleaner than embedded statutory text
  would be: visitors land on the canonical source, which means
  they get the AGO's current version (not a stale snapshot) and
  their attribution model is preserved.
- Zero ongoing maintenance burden — no scraper to update when the
  AGO redesigns their site, no embedding refresh when laws are
  amended.

**Negative.**

- Statutory grounding in the agentic Q&A is weaker than it could be
  with a local corpus. The agent can reference law numbers but
  cannot quote bodies inline. Acceptable trade-off; users get the
  AGO's text one click away.
- The reproducibility manifest cannot include statutory body text
  in its provenance trace. Article *numbers* can still be referenced
  in `evidence_quotes`; the user follows the link to see the body.
- Search latency is one extra page load (kahzaabu → Google → mvlaw).
  Trivial.
