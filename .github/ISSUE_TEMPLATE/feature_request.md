---
name: Feature request
about: Suggest a change or addition to kahzaabu
title: '[feat] '
labels: enhancement
assignees: ''
---

## Use case

What's the actual problem you're trying to solve? Describe the
situation a real user is in, not just "wouldn't it be nice if…"

## Proposed change

What would solving it look like? A new CLI command, a new web route,
a new pipeline stage, a model swap, a fixture-set expansion?

## Alternatives considered

What other approaches did you think about? Why is the proposed one
preferable?

## ADR-worthy?

Kahzaabu uses Architecture Decision Records for non-obvious choices
(see `docs/adr/`). Pick one:

- [ ] **Yes, ADR-worthy.** The change involves a non-reversible
      decision, a new external dependency, a model/provider swap, a
      schema change, or a methodology shift. I'm willing to draft the
      ADR before code lands.
- [ ] **No, surgical change.** Bug fix, doc update, test addition,
      ergonomic CLI tweak, or other low-stakes work.

## Impact

- **Cost impact**: does this change LLM call patterns, embedding
  patterns, or web traffic? Estimate $/run or $/month.
- **Schema impact**: does this require a new column or table?
- **Backwards compat**: does this break existing CLIs, APIs, or saved
  data?

## I'd like to contribute

- [ ] Yes — I'm willing to open a PR if the proposal is approved.
- [ ] No — flagging for the maintainer's consideration.

## Additional context

Links, screenshots, references, prior art.
