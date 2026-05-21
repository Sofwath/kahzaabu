# ADR 0010 — Reproducibility manifest, observability, audit CLIs

**Status**: Accepted (2026-05-21)

## Context

Two distinct gaps with related solutions:

**Reproducibility.** A researcher trying to replicate kahzaabu's fact-check #87 currently has to dig through the SQLite DB by hand to figure out: which curation_runs row produced it, which prompt version was active at the time, which articles fed it, which embeddings clustered the supporting claims, what the LLM cost was. The pipeline keeps this data — there's no public way to query it. AVeriTeC's reference systems publish provenance per output; we don't.

**Observability.** Once kahzaabu is deployed publicly, we'll need to know: are requests slow? Are LLM calls failing? Is the daily budget exhausted? Today the FastAPI app emits stdlib logs only; there are no metrics, no dashboards, no alerts.

**Bias / transparency.** A fact-checking corpus that publishes claims about a sitting head of state must be able to show that its categorical distributions aren't a function of the analyst's politics. We need quantitative evidence — chi-squared tests on category-by-topic, category-by-time, etc. — runnable on demand.

## Decision

### Reproducibility manifest

A new endpoint `GET /api/reproducibility.json` and a corresponding CLI command. For any `fact_check_id`, it returns:

```json
{
  "fact_check_id": 87,
  "verdict_label": "REFUTED",
  "truth_score": 2,
  "claim": "...",
  "produced_by": {
    "curation_run_id": 42,
    "curator_prompt_version": "v3",
    "curator_model": "claude-sonnet-4-6",
    "curator_cost_usd": 0.034
  },
  "supporting_claims": [
    {"claim_id": 1287, "canonical_claim_id": 1240,
     "extraction_run_id": 11, "extractor_prompt_version": "v2",
     "article_id": 32675, "article_title": "..."},
    ...
  ],
  "verification_evidence": [
    {"evidence_id": 99, "verification_run_id": 8,
     "verifier_model": "claude-haiku-4-5", "web_search_query": "..."},
    ...
  ],
  "constitutional_refs": [
    {"article_no": 10, "title": "State Religion"}
  ],
  "decomposition_questions": [
    {"question": "...", "answer_type": "Boolean", "source_medium": "archive"},
    ...
  ],
  "narrative_tricks": [...],
  "git_commit_at_publication": "abc1234..."
}
```

Everything but `git_commit_at_publication` already exists in the DB; this endpoint just joins it together. The git commit is a new column `fact_checks.git_sha_at_publication` populated from `git rev-parse HEAD` at publish time.

### Observability

`prometheus_client` integration in `kahzaabu/web/app.py`. Counters and histograms:

```
kahzaabu_api_requests_total{path, method, status}
kahzaabu_api_request_duration_seconds{path}
kahzaabu_pipeline_stage_runs_total{stage, status}
kahzaabu_pipeline_stage_duration_seconds{stage}
kahzaabu_llm_calls_total{stage, model, status}
kahzaabu_llm_tokens_total{stage, model, direction}
kahzaabu_llm_cost_usd_total{stage, model}
kahzaabu_factchecks_published_total{category, verdict_label}
kahzaabu_db_query_duration_seconds{table}
```

Exposed at `/metrics` (no auth — these are operational stats, not corpus data).

A Grafana dashboard JSON in `docs/observability/grafana-dashboard.json` ready to import. Panels: API latency P50/P95, daily LLM spend, fact-check throughput, error rates.

### Audit CLIs

**`kahzaabu audit`** — bias/fairness summary. Runs on demand; output is markdown:

```
## Category distribution by year
            2024   2025   2026
LIE           1      3      1
MISLEADING    4      6      1
...
chi-squared: ... (p = ...)

## Category distribution by topic
...

## Speaker concentration
- President Muizzu: 218 / 218 fact-checks (100%)
  [single-subject corpus by design; rerun this audit once corpus expands]
```

**`kahzaabu transparency-report --since YYYY-MM-DD`** — generates a public-facing markdown report:
- Fact-checks issued in window, by category and verdict_label
- Corrections received and acted on (from `corrections` table)
- Methodology updates (read from git log of `docs/METHODOLOGY.md`)
- LLM spend total (read from per-stage `*_runs` cost columns)

Both CLIs save their output under `data/reports/`; the transparency report is intended to be regenerated monthly and published on the public site.

### Dockerfile

`Dockerfile` at repo root that builds the entire stack:
- `python:3.11-slim` base
- `pip install -e .[all]`
- Bake the constitution PDF/text + the empty DB schema
- Entry point: `kahzaabu --help`

`docker build -t kahzaabu .` followed by `docker run --rm kahzaabu` reproduces the kahzaabu environment with one command. Critical for the "anyone can reproduce" claim of the reference project.

## Alternatives considered

- **OpenTelemetry instead of Prometheus.** Rejected — OTel is heavier, requires a collector. Prometheus is the de facto standard for a single-host civic-tech tool; we can adopt OTel later without breaking the metric names (same vocabulary).
- **Skip the Dockerfile (local install is documented).** Rejected — the explicit goal is "anyone can reproduce". A Dockerfile turns multi-step setup into one command.
- **Audit + transparency report as web pages, not CLIs.** Rejected for now — pages require a deploy + auth; CLIs run anywhere. The CLIs' markdown output can be served as web pages in a later slice once the public deploy lands.

## Consequences

**Positive.**

- Citable reproducibility — researchers can produce a complete provenance trace per fact-check via one endpoint.
- Production-ready observability — when deployed, we'll know what's happening.
- Quantitative bias / transparency posture — kahzaabu can defend its neutrality with data, not assertions.
- Docker one-command reproduction — clears the OSS-onboarding bar.

**Negative.**

- New dep: `prometheus_client>=0.20` in the `[web]` extra. Small.
- The `/metrics` endpoint is unauthenticated — if deployed publicly, it leaks LLM spend totals (cheap to monitor; not a privacy issue, but worth knowing).
- Maintaining the Grafana dashboard JSON across schema changes is friction. Acceptable.
