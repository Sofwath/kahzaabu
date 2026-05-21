# SPDX-License-Identifier: Apache-2.0
"""FastAPI app for kahzaabu web UI.

Routes:
  GET  /            → dashboard (index.html)
  GET  /browse      → article browser
  GET  /lies        → fact-check browser
  GET  /article/{id}→ single article page
  GET  /ask         → Q&A page

  GET  /api/stats
  GET  /api/articles?limit=&offset=&date_from=&date_to=&category=&q=
  GET  /api/article/{id}
  GET  /api/factchecks?category=&topic=&date_from=&date_to=&limit=
  GET  /api/viz/claims-per-month
  GET  /api/viz/factchecks-by-category
  GET  /api/viz/topics
  POST /api/ask     {question, limit?}
"""
from __future__ import annotations

import logging
from pathlib import Path

import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .api import (articles, ask, claimreview, constitution,
                  contradictions, corrections, factchecks, freshness, inspect,
                  manifesto, reproducibility, stats, viz)
from .limits import limiter
from .. import metrics

# Quiet noisy logs
for name in ("httpx", "httpcore", "anthropic"):
    logging.getLogger(name).setLevel(logging.WARNING)

PKG_DIR = Path(__file__).parent
STATIC_DIR = PKG_DIR / "static"

app = FastAPI(title="kahzaabu", docs_url="/api/docs", redoc_url=None)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
def _rate_limit_handler(request, exc):
    return PlainTextResponse("rate limit exceeded — try again shortly", status_code=429)


# Cache-busting for HTML pages.
# HTML pages contain inline <script> blocks; when we ship a JS fix
# (e.g. fc311be unbroke /factcheck/{id}), users with the old HTML
# still cached in their browser see the broken version. Force the
# browser to always revalidate HTML; static JS/CSS may still be
# cached (their URLs don't change between deploys).
@app.middleware("http")
async def _no_cache_html(request, call_next):
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# Mount API routers
app.include_router(stats.router, prefix="/api", tags=["stats"])
app.include_router(articles.router, prefix="/api", tags=["articles"])
app.include_router(factchecks.router, prefix="/api", tags=["factchecks"])
app.include_router(viz.router, prefix="/api/viz", tags=["viz"])
app.include_router(ask.router, prefix="/api", tags=["ask"])
app.include_router(inspect.router, prefix="/api", tags=["inspect"])
# Authentication / admin routers removed by design — the web UI is
# read-only public. Publishing, user creation, and pipeline triggers
# happen via the `kahzaabu` CLI, which inherits the operator's filesystem
# permissions. No web-side credentials exist anywhere in the system.
app.include_router(corrections.router, prefix="/api", tags=["corrections"])
app.include_router(manifesto.router, prefix="/api", tags=["manifesto"])
app.include_router(freshness.router, prefix="/api", tags=["freshness"])
app.include_router(claimreview.router, prefix="/api", tags=["claimreview"])
app.include_router(contradictions.router, prefix="/api", tags=["contradictions"])
app.include_router(reproducibility.router, prefix="/api", tags=["reproducibility"])
app.include_router(constitution.router, prefix="/api", tags=["constitution"])


# ADR 0010 — prometheus metrics middleware + /metrics endpoint.
# Middleware times every request and records (path, method, status).
# Path is templated where possible (the router's matched route) so
# /api/article/1 and /api/article/2 collapse to /api/article/{id}.
@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    try:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        metrics.record_api_request(
            path=path,
            method=request.method,
            status=response.status_code,
            duration_s=time.monotonic() - start,
        )
    except Exception:  # pragma: no cover - middleware must not crash
        pass
    return response


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
    """Prometheus exposition format. No auth — these are operational
    counters/histograms, not corpus data. ADR 0010."""
    body, content_type = metrics.render_metrics_payload()
    return Response(content=body, media_type=content_type)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
    )


# Mount static assets at /static
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Page routes — serve the corresponding HTML directly
@app.get("/", include_in_schema=False)
def page_dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/browse", include_in_schema=False)
def page_browse():
    return FileResponse(STATIC_DIR / "browse.html")


@app.get("/lies", include_in_schema=False)
def page_lies():
    return FileResponse(STATIC_DIR / "lies.html")


@app.get("/contradictions", include_in_schema=False)
def page_contradictions():
    return FileResponse(STATIC_DIR / "contradictions.html")


@app.get("/constitution", include_in_schema=False)
def page_constitution():
    return FileResponse(STATIC_DIR / "constitution.html")


@app.get("/laws", include_in_schema=False)
def page_laws():
    """Link-out page to old.mvlaw.gov.mv. No scrape, no fetch.
    See docs/adr/0012-mvlaw-link-out-not-scrape.md."""
    return FileResponse(STATIC_DIR / "laws.html")


@app.get("/factcheck/{fact_check_id}", include_in_schema=False)
def page_factcheck(fact_check_id: int):
    return FileResponse(STATIC_DIR / "factcheck.html")


@app.get("/article/{article_id}", include_in_schema=False)
def page_article(article_id: int):
    # The page reads the article_id from window.location and fetches /api/article/{id}
    return FileResponse(STATIC_DIR / "article.html")


@app.get("/ask", include_in_schema=False)
def page_ask():
    return FileResponse(STATIC_DIR / "ask.html")


@app.get("/compare", include_in_schema=False)
def page_compare():
    return FileResponse(STATIC_DIR / "compare.html")


@app.get("/compare/{en_article_id}", include_in_schema=False)
def page_compare_detail(en_article_id: int):
    return FileResponse(STATIC_DIR / "compare_detail.html")


@app.get("/methodology", include_in_schema=False)
def page_methodology():
    return FileResponse(STATIC_DIR / "methodology.html")


@app.get("/corrections", include_in_schema=False)
def page_corrections():
    return FileResponse(STATIC_DIR / "corrections.html")


@app.get("/manifesto", include_in_schema=False)
def page_manifesto():
    return FileResponse(STATIC_DIR / "manifesto.html")


@app.get("/manifesto/{promise_id}", include_in_schema=False)
def page_manifesto_detail(promise_id: int):
    return FileResponse(STATIC_DIR / "manifesto_detail.html")


@app.get("/disclaimer", include_in_schema=False)
def page_disclaimer():
    return FileResponse(STATIC_DIR / "disclaimer.html")
