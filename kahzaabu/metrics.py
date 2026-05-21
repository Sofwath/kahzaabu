# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 12 — Prometheus metrics (ADR 0010).

Counters + histograms exported at /metrics. Names follow the ADR
vocabulary so a future move to OpenTelemetry preserves them.

Three integration surfaces:

  1. **Web requests** — FastAPI middleware in `web/app.py` increments
     `api_requests_total` and observes `api_request_duration_seconds`.

  2. **Pipeline stages** — each `run_*` function uses the
     `track_stage(name)` context manager to record duration + status
     at exit. The same context manager can carry per-call LLM totals
     via its `.record_llm(...)` method.

  3. **Direct LLM-call sites** — `record_llm_call(...)` per call when
     callers want per-API-call granularity. Aggregate (one per stage
     run) is the V2 default.

`prometheus_client` is loaded lazily; without it (e.g. when running
the CLI on a host that didn't install the [web] extras) the helpers
no-op silently. Tests verify this graceful degradation.

This module lives at the package root (NOT under `web/`) so any
module — extractor, decomposer, contradictions, etc. — can import
it without reaching into the web subpackage.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Iterator, Optional

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


# ───────────────────────────────────────────────────────────────────
# Stage context manager — the canonical wrapper for instrumented
# pipeline stages. Use as:
#
#   from kahzaabu import pricing
#   with metrics.track_stage("extractor") as t:
#       ... do work ...
#       t.record_llm(model=pricing.MODELS["sonnet"].id,
#                    tokens_in=t_in, tokens_out=t_out, cost_usd=cost)
#
# On exit it records pipeline_runs + pipeline_duration with status
# "completed" (or "error" if an exception escaped).
# ───────────────────────────────────────────────────────────────────

class _StageTracker:
    """Mutable handle yielded by `track_stage`. Accumulates LLM totals
    and emits them at __exit__ via record_llm_call."""
    def __init__(self, stage: str):
        self.stage = stage
        # Aggregate counters across the stage's lifetime
        self._llm_calls: list[tuple[str, int, int, float, str]] = []

    def record_llm(self, *, model: str, tokens_in: int = 0,
                    tokens_out: int = 0, cost_usd: float = 0.0,
                    status: str = "ok") -> None:
        """Buffer an LLM call to be emitted on stage exit.

        Callers using a single aggregate per stage (the default V2
        pattern) call this once at the end of `run_*` with the run-
        table totals. Callers wanting per-API-call granularity can
        call it inside the loop."""
        self._llm_calls.append((model, int(tokens_in or 0),
                                 int(tokens_out or 0),
                                 float(cost_usd or 0.0), status))

    def _flush(self) -> None:
        for model, t_in, t_out, cost, status in self._llm_calls:
            if t_in or t_out or cost:
                record_llm_call(
                    stage=self.stage, model=model,
                    tokens_in=t_in, tokens_out=t_out,
                    cost_usd=cost, status=status,
                )


def tracked_stage(stage: str, *,
                    model: Optional[str] = None,
                    cost_key: str = "cost_usd",
                    tokens_in_key: str = "tokens_in",
                    tokens_out_key: str = "tokens_out") -> "Callable":
    """Decorator that wraps a stage's run_* function with metrics.

    On return:
      - pipeline_runs counter incremented with status="completed"
        (or "skipped" if the dict has {"skipped": True})
      - pipeline_duration histogram observed
      - if `model` is set AND the return dict has cost_usd/tokens, an
        llm_call + llm_cost + llm_tokens record is emitted with the
        stage's totals (one aggregate per run; per-call granularity
        is opt-in via direct record_llm_call() calls inside the body)

    On exception:
      - status="error", exception re-raised

    Usage (model arg comes from kahzaabu.pricing — the stage module's
    own MODEL constant is the canonical source):
        @metrics.tracked_stage("extractor", model=MODEL)
        def run_extraction(conn, ...) -> dict:
            ...
            return {"cost_usd": cost, "tokens_in": ..., ...}

    Failures inside the metric recording itself never propagate to
    the caller — instrumentation must not break pipeline runs.
    """
    import functools

    def wrapper(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            started = time.monotonic()
            status = "completed"
            result = None
            try:
                result = fn(*args, **kwargs)
                if isinstance(result, dict) and result.get("skipped"):
                    status = "skipped"
                return result
            except Exception:
                status = "error"
                raise
            finally:
                elapsed = time.monotonic() - started
                try:
                    record_pipeline_run(stage=stage, status=status,
                                          duration_s=elapsed)
                    if (isinstance(result, dict) and model and (
                            (result.get(cost_key) or 0) > 0
                            or (result.get(tokens_in_key) or 0) > 0)):
                        record_llm_call(
                            stage=stage, model=model,
                            tokens_in=int(result.get(tokens_in_key) or 0),
                            tokens_out=int(result.get(tokens_out_key) or 0),
                            cost_usd=float(result.get(cost_key) or 0.0),
                            status="ok",
                        )
                except Exception:  # pragma: no cover
                    logger.debug(
                        f"metrics record failed for stage={stage}",
                        exc_info=True)
        return inner
    return wrapper


@contextlib.contextmanager
def track_stage(stage: str) -> Iterator[_StageTracker]:
    """Context manager: time a pipeline stage's run, emit metrics.

    On normal exit:   status=completed
    On exception:     status=error (exception is re-raised)

    Buffered LLM-call totals registered via tracker.record_llm() are
    emitted before the duration metric, so a scraper of /metrics
    sees a consistent snapshot.
    """
    tracker = _StageTracker(stage)
    started = time.monotonic()
    status = "completed"
    try:
        yield tracker
    except Exception:
        status = "error"
        raise
    finally:
        elapsed = time.monotonic() - started
        try:
            tracker._flush()
            record_pipeline_run(stage=stage, status=status,
                                  duration_s=elapsed)
        except Exception:  # pragma: no cover — metrics must not crash callers
            logger.debug(f"metrics record failed for stage={stage}",
                          exc_info=True)
