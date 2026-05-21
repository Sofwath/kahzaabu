# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 12 — Prometheus metrics (ADR 0010).

Counters + histograms exported at /metrics. Names follow the ADR
vocabulary so a future move to OpenTelemetry preserves them.

Two integration surfaces:

  1. **Web requests** — FastAPI middleware in `app.py` increments
     `api_requests_total` and observes `api_request_duration_seconds`.

  2. **Pipeline / LLM-call sites** — callers use the helper
     functions below to record:
       record_pipeline_run(stage, status, duration_s)
       record_llm_call(stage, model, tokens_in, tokens_out, cost_usd, status="ok")
       record_fact_check_published(category, verdict_label)

`prometheus_client` is loaded lazily; without it (e.g. when running
the CLI on a host that didn't install the [web] extras) the helpers
no-op silently. Tests verify this graceful degradation.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("kahzaabu")

# Lazy import. If prometheus_client is not installed, every metric
# becomes a no-op stub — but the module remains import-safe.
try:
    from prometheus_client import (
        Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST,
        CollectorRegistry, REGISTRY,
    )
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when extra missing
    _PROM_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain"

    def generate_latest(*_a, **_kw):  # type: ignore[no-redef]
        return b"# prometheus_client not installed\n"


# Cheap latency buckets: web is sub-second, pipeline stages are seconds-to-minutes.
_WEB_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_STAGE_BUCKETS = (0.5, 1, 5, 10, 30, 60, 300, 600, 1800)


class _NoopMetric:
    """Stub used when prometheus_client isn't installed."""
    def labels(self, **_kw): return self
    def inc(self, *_a, **_kw): pass
    def observe(self, *_a, **_kw): pass


def _new_counter(name: str, doc: str, labels: tuple[str, ...]) -> "Counter":
    if not _PROM_AVAILABLE:
        return _NoopMetric()  # type: ignore[return-value]
    try:
        return Counter(name, doc, labels)
    except ValueError:
        # Duplicate registration — happens during test reloads.
        return REGISTRY._names_to_collectors[name]  # type: ignore[attr-defined]


def _new_histogram(name: str, doc: str, labels: tuple[str, ...],
                    buckets) -> "Histogram":
    if not _PROM_AVAILABLE:
        return _NoopMetric()  # type: ignore[return-value]
    try:
        return Histogram(name, doc, labels, buckets=buckets)
    except ValueError:
        return REGISTRY._names_to_collectors[name]  # type: ignore[attr-defined]


# ───────────────────────────────────────────────────────────────────
# Metric definitions — ADR 0010 vocabulary
# ───────────────────────────────────────────────────────────────────

api_requests = _new_counter(
    "kahzaabu_api_requests_total",
    "Total HTTP requests to kahzaabu web API",
    ("path", "method", "status"),
)

api_duration = _new_histogram(
    "kahzaabu_api_request_duration_seconds",
    "HTTP request duration in seconds",
    ("path",),
    _WEB_BUCKETS,
)

pipeline_runs = _new_counter(
    "kahzaabu_pipeline_stage_runs_total",
    "Pipeline stage executions",
    ("stage", "status"),
)

pipeline_duration = _new_histogram(
    "kahzaabu_pipeline_stage_duration_seconds",
    "Pipeline stage duration in seconds",
    ("stage",),
    _STAGE_BUCKETS,
)

llm_calls = _new_counter(
    "kahzaabu_llm_calls_total",
    "LLM API calls grouped by stage and model",
    ("stage", "model", "status"),
)

llm_tokens = _new_counter(
    "kahzaabu_llm_tokens_total",
    "Total LLM tokens consumed",
    ("stage", "model", "direction"),
)

llm_cost = _new_counter(
    "kahzaabu_llm_cost_usd_total",
    "Cumulative LLM cost in USD",
    ("stage", "model"),
)

fact_checks_published = _new_counter(
    "kahzaabu_factchecks_published_total",
    "Fact-checks published, grouped by category + verdict label",
    ("category", "verdict_label"),
)


# ───────────────────────────────────────────────────────────────────
# Public helpers
# ───────────────────────────────────────────────────────────────────

def record_api_request(*, path: str, method: str, status: int,
                        duration_s: float) -> None:
    """Called by the FastAPI middleware on each request."""
    api_requests.labels(path=path, method=method,
                         status=str(status)).inc()
    api_duration.labels(path=path).observe(duration_s)


def record_pipeline_run(*, stage: str, status: str,
                         duration_s: float) -> None:
    pipeline_runs.labels(stage=stage, status=status).inc()
    pipeline_duration.labels(stage=stage).observe(duration_s)


def record_llm_call(*, stage: str, model: str,
                     tokens_in: int = 0, tokens_out: int = 0,
                     cost_usd: float = 0.0,
                     status: str = "ok") -> None:
    llm_calls.labels(stage=stage, model=model, status=status).inc()
    if tokens_in:
        llm_tokens.labels(stage=stage, model=model,
                           direction="in").inc(tokens_in)
    if tokens_out:
        llm_tokens.labels(stage=stage, model=model,
                           direction="out").inc(tokens_out)
    if cost_usd:
        llm_cost.labels(stage=stage, model=model).inc(cost_usd)


def record_fact_check_published(*, category: str,
                                  verdict_label: Optional[str]) -> None:
    fact_checks_published.labels(
        category=category or "_NULL",
        verdict_label=verdict_label or "_NULL",
    ).inc()


def render_metrics_payload() -> tuple[bytes, str]:
    """Return (body, content_type) for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST


# Exposed for tests / introspection.
def prometheus_available() -> bool:
    return _PROM_AVAILABLE
