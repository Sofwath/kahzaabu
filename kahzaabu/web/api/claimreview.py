# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 6 — ClaimReview JSON-LD API endpoints (ADR 0006).

Two public endpoints:
  GET /api/factchecks/{id}/jsonld         — single fact-check
  GET /api/claimreviews/feed.json         — paged aggregate

Both serve the cached blob from fact_checks.claimreview_jsonld unless
?refresh=1 is supplied (regenerates from the live row + truth_score
derivation). The cache is populated by `kahzaabu export-claimreview`
on a schedule.

Only published fact_checks are exposed via these endpoints. Unpublished
items would imply editorial publication that hasn't happened.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from ... import claimreview as cr_mod
from ..db_dep import get_db

router = APIRouter()


@router.get("/factchecks/{fact_check_id}/jsonld")
def get_factcheck_jsonld(
    fact_check_id: int,
    refresh: int = Query(0, ge=0, le=1, description="1 = regenerate from live data"),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Return the schema.org ClaimReview JSON-LD blob for one
    published fact-check. Sets Content-Type so indexers see it as
    structured data."""
    row = conn.execute(
        "SELECT published, claimreview_jsonld FROM fact_checks WHERE id = ?",
        (fact_check_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"fact_check {fact_check_id} not found")
    if not row["published"]:
        raise HTTPException(404, "fact_check is not published")

    if refresh:
        blob = cr_mod.cache_jsonld(conn, fact_check_id)
    else:
        cached = row["claimreview_jsonld"]
        if cached:
            try:
                blob = json.loads(cached)
            except json.JSONDecodeError:
                blob = cr_mod.cache_jsonld(conn, fact_check_id)
        else:
            blob = cr_mod.cache_jsonld(conn, fact_check_id)

    return JSONResponse(
        content=blob,
        media_type="application/ld+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/claimreviews/feed.json")
def get_claimreviews_feed(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    since: Optional[str] = Query(None, description="ISO date — filter to created_at >= since"),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Aggregate feed of all published ClaimReviews. Suitable for bulk
    indexing by Google Fact Check Explorer / archival mirrors.

    Response shape:
      {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "publisher": {...},
        "numberOfItems": <total>,
        "itemListElement": [{"@type": "ListItem", "position": N, "item": <ClaimReview>}, ...],
        "next": <url or null>
      }
    """
    sql = "SELECT id, claimreview_jsonld FROM fact_checks WHERE published = 1"
    params: list = []
    if since:
        sql += " AND created_at >= ?"; params.append(since)
    sql += " ORDER BY id LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = list(conn.execute(sql, params))

    total = conn.execute(
        "SELECT COUNT(*) FROM fact_checks WHERE published = 1"
        + (" AND created_at >= ?" if since else ""),
        ([since] if since else []),
    ).fetchone()[0]

    items: list = []
    for i, r in enumerate(rows):
        cached = r["claimreview_jsonld"]
        if not cached:
            try:
                blob = cr_mod.cache_jsonld(conn, r["id"])
            except Exception:
                continue
        else:
            try:
                blob = json.loads(cached)
            except json.JSONDecodeError:
                continue
        items.append({
            "@type": "ListItem",
            "position": offset + i + 1,
            "item": blob,
        })

    next_url = None
    if offset + len(rows) < total:
        params_str = f"limit={limit}&offset={offset + limit}"
        if since:
            params_str += f"&since={since}"
        next_url = f"/api/claimreviews/feed.json?{params_str}"

    return JSONResponse(
        content={
            "@context": "https://schema.org",
            "@type": "ItemList",
            "publisher": cr_mod._org_block(),
            "numberOfItems": total,
            "itemListElement": items,
            "next": next_url,
        },
        media_type="application/ld+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )
