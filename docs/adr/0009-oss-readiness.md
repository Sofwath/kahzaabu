# ADR 0009 — OSS readiness, methodology cards, backup

**Status**: Accepted (2026-05-21)

## Context

Kahzaabu is being open-sourced as a reference civic-tech project. Today it has no LICENSE — meaning under default copyright law, no one can legally use, modify, or redistribute it. It also lacks the supporting files that GitHub's "community profile" checks for, the methodology documentation that academic and journalistic citers expect, and any backup mechanism for the 800 MB SQLite database that holds 9,000+ claims and 218 fact-checks.

For a project that aims to be the reference for Maldives-style civic-tech, this is unacceptable.

## Decision

### Licensing

**Apache-2.0**. Two-paragraph justification:

- **MIT** is shorter and more permissive but lacks an explicit patent grant. For a project that may apply novel NLP techniques (contradiction-pair detection methodology, Q&A decomposition tuned for political speech), the patent grant matters.
- **GPL-3.0** would force downstream forks to stay open-source. Tempting for civic-tech accountability, but excludes commercial reuse — and we want commercial fact-checking services to be able to learn from / extend kahzaabu, not avoid it.
- **Apache-2.0** is the standard for foundation-model-era ML projects (used by Llama, Mistral derivatives, every major AI tool). Implicit patent grant, attribution required, derivative works can re-license. Best fit.

LICENSE file goes at repo root. Every Python file gets a one-line SPDX comment: `# SPDX-License-Identifier: Apache-2.0`.

### Community files

| File | Content |
|---|---|
| `LICENSE` | Apache-2.0, year + author block filled |
| `CONTRIBUTING.md` | Slice discipline, ADR process, test requirements, signed commits expected |
| `CODE_OF_CONDUCT.md` | Adopt the Contributor Covenant 2.1 verbatim |
| `SECURITY.md` | Disclosure process: email + 90-day responsible disclosure window |
| `.github/ISSUE_TEMPLATE/bug_report.md` | Reproduction steps, environment, expected vs actual |
| `.github/ISSUE_TEMPLATE/feature_request.md` | Use case, alternatives considered, ADR-worthy or not |
| `.github/PULL_REQUEST_TEMPLATE.md` | Slice link, ADR link if any, tests passing, ci-dry-run passing |

### Methodology documentation

Two structured docs following Google's templates:

- **`docs/MODEL_CARD.md`** — per LLM-call stage in the pipeline. For each: model id + version, prompt version, training data (n/a — we use API), known biases, intended use, out-of-scope use. Required by ML publication standards (Mitchell et al. 2019).
- **`docs/DATA_CARD.md`** — describes the corpus: sources, coverage windows, known gaps (pre-Nov-2023 articles absent), Dhivehi/English coverage, fact-check selection criteria, refresh cadence, retention policy. Required by data publication standards (Pushkarna et al. 2022).
- **`docs/METHODOLOGY.md`** — public-facing extended version of the existing `methodology.html`. Explains the pipeline in plain English with citations to FEVER / AVeriTeC / RAGAR / PolitiFact. This becomes the basis for an arXiv-style methodology paper.

### Backup

`scripts/backup.sh` — single-file bash:
- `sqlite3 data/kahzaabu.db .dump | gzip > data/backups/$(date +%Y-%m-%d).sql.gz`
- Retain 30 days locally; user is responsible for off-machine sync (rclone / rsync to cloud).
- `scripts/restore.sh <date>` restores from a backup file.

Backup runs nightly via `hermes cron`:
```
hermes cron create --name kahzaabu-backup --no-agent \
  --script backup.sh '0 3 * * *'
```

Backup discipline is part of Slice 11's done criteria — the deploy slice can later add off-machine sync.

## Alternatives considered

- **Wait until V1 paper is published before licensing.** Rejected — without a LICENSE, no one can legally clone-and-experiment, blocking the very feedback we need.
- **MIT instead of Apache-2.0.** Rejected — see above; patent grant matters for the novel methodology pieces.
- **Custom "civic-tech only" license.** Rejected — non-standard licenses chill adoption + create license-compatibility headaches.
- **Skip CONTRIBUTING.md (the project is solo).** Rejected — the explicit goal is to attract collaborators. Even for a solo project today, the file documents the slice discipline.

## Consequences

**Positive.**

- Kahzaabu becomes legally usable, forkable, and citable.
- GitHub community profile hits 100%.
- The methodology docs become citation targets in their own right.
- Backup discipline prevents the existential risk of an SSD failure wiping 9,000+ claims.

**Negative.**

- Apache-2.0 derivative works can re-license; a hostile fork could relicense and continue without contributing back. We accept this — Apache is the right trade-off.
- Maintenance burden — every PR must follow CONTRIBUTING.md; every new pipeline stage updates MODEL_CARD.md. This is correct overhead.
- Backup file consumes ~50 MB/day compressed. After 30 days that's ~1.5 GB. User must manage retention if disk pressure becomes an issue.
