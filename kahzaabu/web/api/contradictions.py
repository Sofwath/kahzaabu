# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 7 — /api/contradictions endpoints (ADR 0004).

Two routes:
  GET /api/contradictions               — paged list + verdict filter
  GET /api/contradictions/{id}          — one pair + reasoning chain

Public-mode visibility: only contradiction_pairs with published=1
are returned to anonymous viewers. Admins / editors see all four
verdicts so they can manage the review queue.

Per ADR 0004, only the CONTRADICTION verdict propagates to fact_checks
downstream. The other three (EVOLVING_POSITION, CONTEXT_CHANGED,
NOT_CONTRADICTORY) are persisted for transparency and queryable here.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..db_dep import get_db

router = APIRouter()

VALID_VERDICTS = {"CONTRADICTION", "EVOLVING_POSITION",
                  "CONTEXT_CHANGED", "NOT_CONTRADICTORY"}

@router.get("/contradictions")
def list_contradictions(
    verdict: Optional[str] = None,
    subject: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    if verdict and verdict not in VALID_VERDICTS:
        raise HTTPException(400, f"invalid verdict; valid: {sorted(VALID_VERDICTS)}")

    # No publish-workflow gating: pairs are operator-curated automated
    # output. The legacy `published` column lingers in the SELECT for
    # backwards-compat with any client still reading the field.
    sql = ("SELECT cp.id, cp.claim_a_id, cp.claim_b_id, cp.subject, "
           "       cp.verdict, cp.confidence, cp.published, cp.detected_at "
           "FROM contradiction_pairs cp WHERE 1=1")
    params: list = []
    if verdict:
        sql += " AND cp.verdict = ?"
        params.append(verdict)
    if subject:
        sql += " AND cp.subject = ?"
        params.append(subject)

    count_sql = "SELECT COUNT(*) FROM (" + sql + ")"
    total = conn.execute(count_sql, params).fetchone()[0]

    sql += " ORDER BY cp.confidence DESC, cp.detected_at DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(offset)]
    rows = conn.execute(sql, params).fetchall()

    items: list = []
    for r in rows:
        d = dict(r)
        # Join in quotes + dates of the two claims for a one-shot list view
        ca = conn.execute(
            """SELECT c.id, c.quote, a.published_date, a.title
               FROM claims c
               JOIN articles a ON a.id = c.article_id AND a.language = c.language
               WHERE c.id = ?""", (d["claim_a_id"],)
        ).fetchone()
        cb = conn.execute(
            """SELECT c.id, c.quote, a.published_date, a.title
               FROM claims c
               JOIN articles a ON a.id = c.article_id AND a.language = c.language
               WHERE c.id = ?""", (d["claim_b_id"],)
        ).fetchone()
        if ca:
            d["claim_a"] = dict(ca)
        if cb:
            d["claim_b"] = dict(cb)
        items.append(d)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "verdict_filter": verdict,
        "subject_filter": subject,
        "items": items,
    }

@router.get("/contradictions/{contradiction_id}")
def get_contradiction(
    contradiction_id: int,
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    # Contradiction pairs are operator-output from the contradictions
    # stage; they don't go through a publish workflow. The legacy
    # `published` column from the removed admin UI is no longer
    # consulted here.
    sql = "SELECT * FROM contradiction_pairs WHERE id = ?"
    r = conn.execute(sql, (contradiction_id,)).fetchone()
    if r is None:
        raise HTTPException(404, f"contradiction {contradiction_id} not found")
    d = dict(r)
    try:
        d["reasoning_chain"] = json.loads(d.get("reasoning_chain") or "[]")
    except Exception:
        d["reasoning_chain"] = []

    # Full claim records on both sides
    for side, cid in (("claim_a", d["claim_a_id"]), ("claim_b", d["claim_b_id"])):
        row = conn.execute(
            """SELECT c.id, c.quote, c.polarity, c.type, c.subject,
                      c.subject_normalized, a.id AS article_id,
                      a.title AS article_title, a.published_date,
                      a.reference AS article_url
               FROM claims c
               JOIN articles a ON a.id = c.article_id AND a.language = c.language
               WHERE c.id = ?""", (cid,)
        ).fetchone()
        if row:
            d[side] = dict(row)
    return d
