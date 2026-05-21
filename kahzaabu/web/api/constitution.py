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
