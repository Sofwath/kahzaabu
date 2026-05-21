<!-- See CONTRIBUTING.md for the slice discipline this template enforces. -->

## What & why

One paragraph: what's the problem, what does this PR change. Lead
with the user-facing motivation, not the diff.

## Linked context

- Slice / build-plan row:      <!-- e.g. V2 Slice N — short title -->
- ADR (if applicable):         <!-- e.g. docs/adr/NNNN-short-title.md -->
- Issue (if applicable):       <!-- e.g. #123 -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] Feature (new behaviour, non-breaking)
- [ ] V2 slice (architectural, ADR-tracked)
- [ ] Refactor (no behavioural change)
- [ ] Docs / README / ADR
- [ ] Test-only
- [ ] Build / CI / scripts
- [ ] Breaking change (explain below)

## Tests

- [ ] `./scripts/test.sh` passes (full suite)
- [ ] `./scripts/ci-dry-run.sh` passes (fresh-worktree validation)
- [ ] New tests added for new behaviour (or none needed because…)

```
# paste the final `Ran N tests in Ms` line
```

## Quality eval (LLM-call-site changes only)

If this PR touches a prompt or an LLM-call site, paste the
`kahzaabu eval` summary:

```
# paste here
```

- [ ] Verified-subset metric for affected stage **did not** regress
- [ ] Or: regression is intentional and explained in the commit body
      (ADR-bypass per ADR 0008)

## Schema / data changes

- [ ] No schema changes
- [ ] Additive only (new columns / tables; migration is idempotent in
      `claims_db.py`)
- [ ] Destructive (drop / rename) — explain why and the migration path

## Documentation

- [ ] README updated where relevant
- [ ] ADR added/updated for non-trivial decisions
- [ ] MODEL_CARD.md / DATA_CARD.md / METHODOLOGY.md updated if the
      change affects LLM call sites or the corpus

## Reviewer checklist

- [ ] Conforms to slice discipline (small, complete, testable)
- [ ] No unsolicited refactors / scope creep
- [ ] Commit messages follow the format in CONTRIBUTING.md
- [ ] SPDX header present on any new `.py` file

## Anything else

Tradeoffs, follow-ups, known limitations, anything that needs eyes
beyond the diff.
