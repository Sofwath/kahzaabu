# SPDX-License-Identifier: Apache-2.0
"""V2 — Constitution lookup API.

Exposes the 301 articles of the Constitution of the Republic of Maldives
(already parsed + FTS5-indexed in `kahzaabu/constitution.py`) over HTTP.

  GET  /api/constitution/articles?limit=&offset=         — paged list
  GET  /api/constitution/search?q=&limit=                — BM25 search
  GET  /api/constitution/{article_no}                    — single article
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ... import constitution
from ..db_dep import get_db

router = APIRouter()


@router.get("/constitution/articles",
             summary="List constitution articles, paged")
def list_articles(
    limit: int = Query(50, ge=1, le=301),
    offset: int = Query(0, ge=0),
    chapter: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    where = "1=1"
    params: list = []
    if chapter:
        where += " AND chapter = ?"
        params.append(chapter)
    total = conn.execute(
        f"SELECT COUNT(*) FROM constitution_articles WHERE {where}",
        params,
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT article_no, chapter, title, body, source_version "
        f"FROM constitution_articles WHERE {where} "
        f"ORDER BY article_no LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return {
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


@router.get("/constitution/search",
             summary="BM25 full-text search across constitution articles")
def search_articles(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    if not q.strip():
        return {"query": q, "items": []}
    try:
        hits = constitution.lookup(conn, q, limit=limit)
    except sqlite3.OperationalError as e:
        # FTS5 unavailable or query syntax issue.
        raise HTTPException(status_code=400, detail=str(e))
    return {"query": q, "items": hits}


@router.get("/constitution/{article_no}",
             summary="Fetch a single article by number")
def get_article(
    article_no: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = conn.execute(
        "SELECT article_no, chapter, title, body, source_version "
        "FROM constitution_articles WHERE article_no = ?",
        (article_no,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404,
                             detail=f"article {article_no} not found")
    return dict(row)


@router.get("/constitution/{article_no}/citing-factchecks",
             summary="Fact-checks whose body relates to this constitution article")
def citing_factchecks(
    article_no: int,
    limit: int = Query(10, ge=1, le=50),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Reverse cross-reference: given a constitution article, find
    fact-checks whose claim + topic best match its body via the same
    FTS5 BM25 ranking the forward search uses.

    There is no curated `fact_check.constitution_article_no` column
    (yet) — this is a best-effort symmetric search using the article's
    title + first ~30 body words as the query against the fact-checks
    text. A separate ADR'd backfill would produce explicit links."""
    art = conn.execute(
        "SELECT article_no, title, body "
        "FROM constitution_articles WHERE article_no = ?",
        (article_no,),
    ).fetchone()
    if not art:
        raise HTTPException(status_code=404,
                             detail=f"article {article_no} not found")
    # Build a focused query: title + first 30 body words. Strip
    # quote characters because they have special meaning in FTS5.
    title = (art["title"] or "").strip()
    body_words = (art["body"] or "").split()[:30]
    raw_query = f"{title} {' '.join(body_words)}".replace('"', " ")
    # FTS5 has a query-string length cap; trim defensively.
    query = raw_query[:240].strip()
    if not query:
        return {"article_no": article_no, "items": []}

    # We don't have an FTS5 index on fact_checks itself; fall back
    # to a LIKE-against-claim+topic with the most salient single
    # term from the article title. This is intentionally crude —
    # the goal is "show readers SOME related fact-checks", not
    # ranked retrieval.
    salient = title.split() or body_words
    if not salient:
        return {"article_no": article_no, "items": []}
    # Use the longest title token as the LIKE seed (longest = most
    # specific, avoids stopwords like "of"/"the").
    seed = max(salient, key=len)
    if len(seed) < 4:
        return {"article_no": article_no, "items": []}
    rows = conn.execute(
        """SELECT id, category, claim, topic, verdict_label, truth_score_label,
                  claim_date
           FROM fact_checks
           WHERE published = 1
             AND (claim LIKE ? OR topic LIKE ?)
           ORDER BY claim_date DESC
           LIMIT ?""",
        (f"%{seed}%", f"%{seed}%", limit),
    ).fetchall()
    return {
        "article_no": article_no,
        "query_seed": seed,
        "items": [dict(r) for r in rows],
    }
