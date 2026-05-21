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

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .api import admin, articles, ask, auth, claimreview, contradictions, corrections, factchecks, freshness, inspect, manifesto, stats, viz
from .limits import limiter

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


# Mount API routers
app.include_router(stats.router, prefix="/api", tags=["stats"])
app.include_router(articles.router, prefix="/api", tags=["articles"])
app.include_router(factchecks.router, prefix="/api", tags=["factchecks"])
app.include_router(viz.router, prefix="/api/viz", tags=["viz"])
app.include_router(ask.router, prefix="/api", tags=["ask"])
app.include_router(inspect.router, prefix="/api", tags=["inspect"])
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(admin.router, prefix="/api", tags=["admin"])
app.include_router(corrections.router, prefix="/api", tags=["corrections"])
app.include_router(manifesto.router, prefix="/api", tags=["manifesto"])
app.include_router(freshness.router, prefix="/api", tags=["freshness"])
app.include_router(claimreview.router, prefix="/api", tags=["claimreview"])
app.include_router(contradictions.router, prefix="/api", tags=["contradictions"])


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/admin\n"
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


@app.get("/login", include_in_schema=False)
def page_login():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/admin", include_in_schema=False)
@app.get("/admin/queue", include_in_schema=False)
def page_admin_queue():
    return FileResponse(STATIC_DIR / "admin_queue.html")


@app.get("/admin/run", include_in_schema=False)
def page_admin_run():
    return FileResponse(STATIC_DIR / "admin_run.html")


@app.get("/manifesto", include_in_schema=False)
def page_manifesto():
    return FileResponse(STATIC_DIR / "manifesto.html")


@app.get("/manifesto/{promise_id}", include_in_schema=False)
def page_manifesto_detail(promise_id: int):
    return FileResponse(STATIC_DIR / "manifesto_detail.html")
