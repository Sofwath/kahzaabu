# Maintenance cadence

This document captures the operator's running checklist for keeping
kahzaabu healthy. Each item maps to a `make` target or a script.

## Daily (automated)

| What | Where | Failure mode if skipped |
|---|---|---|
| Pipeline cycle (scrape → extract → curate → verify) | `launchd` (`scripts/com.kahzaabu.pipeline.plist`) every 12h | Corpus goes stale; new press releases not extracted. |
| DB backup | Run `make backup` from a cron entry — 1 line | SSD failure = lose ~9k claims + 220 fact-checks. ADR 0009. |

## Weekly (automated)

| What | Where | Failure mode if skipped |
|---|---|---|
| `/laws` tile link-rot probe | `.github/workflows/external-links.yml` runs Mondays 02:00 UTC | If AGO renames a path, the tile silently 404s in users' new tabs. Workflow opens a GitHub issue on first detection. |

## Monthly (manual, ~5 min)

| What | Command | Why |
|---|---|---|
| Vendored-JS drift scan | `make check-updates` | Chart.js / marked have no Dependabot coverage. Surfaces upstream releases. |
| Transparency report | `make transparency-report SINCE=YYYY-MM-DD` | Publishable monthly summary per ADR 0010. |
| Bias / fairness audit | `make audit` | Quantitative posture per ADR 0010. Run after each curation batch. |
| Eval baseline refresh | `make eval` | Pin the verified-subset metric. Compare against `data/eval_history.jsonl`. |

## On every vendored-JS upgrade

```
make check-updates       # surfaces drift
# 1. follow the curl recipe in kahzaabu/web/static/js/NOTICE.md
# 2. bump version pins in NOTICE.md
make js-verify           # confirms call sites still work
make test                # full Python regression
```

The CI `js-verify` job will catch a broken upgrade on PR open; running
locally cuts the loop tighter.

## On every PR

CI enforces:
- `unit` job — full Python test suite + stale-name guard
- `js-verify` job — vendored JS libs work against kahzaabu's call sites

Local equivalents:
```
make test         # the unit job
make js-verify    # the js-verify job
make ci-dry-run   # validates fresh-worktree install (catches missing files)
```

## Release / deploy checklist

When preparing to publish (Slice 12+ goal):

1. `make check-updates` — vendored libs current
2. `make check-links` — `/laws` tiles live (one-shot, not the weekly cron)
3. `make eval` — verified-subset metric pinned
4. `make test` — 280+ tests green
5. `make js-verify` — call sites stable
6. `make docker-cpu` — image builds; smoke `kahzaabu eval` inside
7. `make audit` and `make transparency-report SINCE=...` — output to `data/reports/`
8. Backup: `make backup`

## Cleanup

Stale Docker images accumulate after a few rebuilds. `make clean-images`
removes all locally-tagged `kahzaabu:*` images. Recommended after every
major upgrade cycle (e.g. once a quarter).

## When something breaks

| Symptom | First diagnostic |
|---|---|
| Pages render but no data appears | Hard-refresh (browser cache holding old HTML). Server-side: `make js-verify`. |
| `/laws` tile 404s | `make check-links` |
| `kahzaabu eval` regressed | Compare against `data/eval_history.jsonl`; check if a prompt changed |
| CI red on `js-verify` | A vendored lib upgrade broke a call site — see `kahzaabu/web/static/js/NOTICE.md` |
| Docker image suddenly huge | `PIP_EXTRA_INDEX_URL` not respected? See Dockerfile for CPU-torch directive |

## Where the maintenance entry points live

```
Makefile                                  one-shot wrapper for everything below
scripts/test.sh                           Python unit suite + stale-name guard
scripts/ci-dry-run.sh                     Workflow-equivalent in fresh worktree
scripts/backup.sh + restore.sh            SQLite dump/restore (ADR 0009)
scripts/check-vendor-updates.sh           npm-registry drift detector (vendored JS)
scripts/check-external-links.sh           /laws tile link-rot probe (ADR 0012)
scripts/js-verify/                        JS call-site verifier (Node, lockfile-pinned)
.github/workflows/test.yml                CI: unit + js-verify jobs (every push/PR)
.github/workflows/external-links.yml      CI: scheduled link-rot probe (weekly)
```
