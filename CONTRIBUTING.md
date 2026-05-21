# Contributing to Kahzaabu

> **Heads-up.** Kahzaabu is a **sample / reference implementation** of a
> Hermes Agent plugin and fact-checking pipeline, built for educational
> and research purposes (see [`DISCLAIMER.md`](DISCLAIMER.md)). PRs that
> strengthen the reference architecture, add regression guards, improve
> docs, or fix measurement bugs are on-mission. PRs that remove the
> disclaimer banner, weaken the `not authoritative source` framing in
> ClaimReview JSON-LD, or reposition the project as a publicly-cited
> fact-checking outlet are off-mission and will be declined.

Thanks for your interest in contributing. Kahzaabu is a civic-tech reference
project for fact-checking Maldives Presidency press releases, combining
AVeriTeC verdict structure, RAGAR Chain-of-RAG reasoning, Full Fact claim
matching, and PolitiFact's Truth-O-Meter into one open pipeline.

This file documents how to land changes without breaking the slice discipline
that built V2.

## Ground rules

1. **Every non-trivial change starts with an ADR.** New ADRs go under
   `docs/adr/NNNN-short-title.md` and follow the Michael Nygard format
   (Context → Decision → Alternatives → Consequences). Append-only — if a
   decision is reversed, write a new ADR that supersedes it. Don't edit old
   ones except to mark `Status: Superseded by ADR XXXX`.
2. **Slice discipline.** Group related work into a single slice (commit) with
   tests, docs, and ADR all landing together. Don't ship half-finished
   features behind feature flags; ship narrow but complete.
3. **Verified ground truth ≠ pinned baselines.** When adding fixtures under
   `tests/golden/<stage>/`, default `verified: false` unless the `expected`
   is hand-confirmed. See ADR 0008.
4. **No silent prompt edits.** LLM-call sites have a `PROMPT_VERSION` const.
   Bump it when you change the prompt; document the rationale in the commit.
5. **Tests must pass.** `./scripts/test.sh` is the gate. `./scripts/ci-dry-run.sh`
   validates a fresh-worktree install in addition.
6. **No new top-level deps without a note in `pyproject.toml`.** Each block
   has a short comment explaining what it's for; keep it that way.

## Development environment

```bash
git clone <your-fork>
cd kahzaabu
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[web,tui,ml-local]"
./scripts/test.sh             # full suite (~2 sec)
```

For LLM-touching code, set `ANTHROPIC_API_KEY`. For embedding work, choose
a provider via `KAHZAABU_EMBED_PROVIDER` (`local` | `openai` | `voyage`)
— defaults to `local` (sentence-transformers, $0). See ADR 0007.

## Working on a slice

1. Create a branch: `git checkout -b feat/slice-N-short-title`.
2. Write the test first when the change has clear input/output.
3. Implement until the test passes.
4. Update `tests/golden/` if you changed an LLM-call site's behaviour.
5. Update `docs/EVAL_RESULTS.md` via `kahzaabu eval` if metrics shifted.
6. Run `./scripts/test.sh` AND `./scripts/ci-dry-run.sh`. Both must pass.
7. Update `docs/V2_BUILD_PLAN.md` if your slice closes one.
8. Commit with the message format below.

### Commit message format

```
type(scope): one-line summary

Longer paragraph(s) explaining what changed and why. Lead with the
problem, then the fix.

Tests / regressions / cost / migrations: any one-liners that the
maintainer needs to see during review.

Refs: ADR NNNN (if applicable)
```

`type`: `feat`, `fix`, `docs`, `chore`, `test`, `refactor`.
`scope`: `v2`, `web`, `eval`, `pipeline`, `db`, etc.

Sign commits when you can (`git commit -S`). Not enforced, but recommended.

## Pull requests

Open the PR against `main`. Use the template — it asks for:

- Linked ADR or slice number
- `./scripts/test.sh` output
- `./scripts/ci-dry-run.sh` output
- For prompt edits: a `kahzaabu eval` run showing no regression on the
  verified subset
- Anything destructive or non-reversible (DB migrations, fixture deletions,
  ADR supersessions) called out explicitly

Two-line PR descriptions get bounced. Use the body to explain *why* the
change is needed; the diff explains *what*.

## Code style

- No formatter is enforced. Match the file you're editing.
- Add an SPDX header to every new `.py` file:
  `# SPDX-License-Identifier: Apache-2.0`
- Type hints encouraged on public functions; not required for internal helpers.
- No new `print()` in library code — use the `kahzaabu` logger.
- New CLI commands go through Click; see `kahzaabu/cli.py` for the pattern.

## Adding golden-set fixtures

When you flag an LLM-output quality issue, the fix is usually a new golden
fixture before any prompt edit.

```json
{
  "id": "short-slug",
  "input":    { "..." },
  "expected": { "..." },
  "verified": true,
  "notes":    "Why this fixture, what it pins"
}
```

Promote `verified: false` → `verified: true` only after hand-confirming
the expected output. Add a `verification_notes` field with your reasoning;
that note becomes the durable justification.

## Code of Conduct

See [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md). We follow the Contributor
Covenant 2.1. Enforcement contact: Sofwathullah.Mohamed@gmail.com.

## Where to start

Good first issues are labelled `good-first-issue`. If none are open and you
want to contribute, the highest-leverage areas are:

- Promote pinned fixtures to verified after hand-review (`tests/golden/`)
- Extend `extractor` taxonomy / fix mis-tags surfaced by `kahzaabu eval`
- Translate `methodology.html` content to additional languages
- Wire `kahzaabu audit` for bias/fairness summaries (Slice 12 territory)

Open an issue first if it's larger than ~50 LoC; we'd rather align on
direction before you invest the time.

Thanks again for the help.
