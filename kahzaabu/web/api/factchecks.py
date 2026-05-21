"""Fact-checks endpoints.

In public mode (KAHZAABU_PUBLIC_MODE env var set) anonymous viewers only see
fact-checks with published=1. Authenticated admins/editors see everything.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db_dep import get_db
from ..limits import PUBLIC_MODE
from .auth import current_user

router = APIRouter()


def _public_filter(user: Optional[dict]) -> str:
    """Returns extra WHERE clause for public-mode anonymous viewers."""
    if PUBLIC_MODE and not user:
        return " AND published = 1"
    return ""

ALLOWED_CATEGORIES = (
    "LIE", "CONTRADICTION", "MISLEADING", "SHIFTING NUMBERS",
    "CREDIT THEFT", "BROKEN DEADLINE",
)


@router.get("/factchecks")
def list_factchecks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    category: Optional[str] = None,
    topic: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    user: Optional[dict] = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    sql = """SELECT id, category, claim_date, claim, what_actually_happened, type,
                    topic, source, source_article_ids, evidence_quotes, created_at,
                    published, public_summary
             FROM fact_checks WHERE 1=1""" + _public_filter(user)
    params: list = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    if topic:
        sql += " AND topic = ?"
        params.append(topic)
    if date_from:
        sql += " AND claim_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND claim_date <= ?"
        params.append(date_to)
    if q:
        sql += " AND (claim LIKE ? OR what_actually_happened LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    count_sql = "SELECT COUNT(*) FROM (" + sql + ")"
    total = conn.execute(count_sql, params).fetchone()[0]
    sql += " ORDER BY claim_date DESC, id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["source_article_ids"] = json.loads(d["source_article_ids"] or "[]")
        except Exception:
            d["source_article_ids"] = []
        try:
            d["evidence_quotes"] = json.loads(d["evidence_quotes"] or "[]")
        except Exception:
            d["evidence_quotes"] = []
        # attach evidence count (cheap)
        d["n_evidence"] = conn.execute(
            "SELECT COUNT(*) FROM fact_check_evidence WHERE fact_check_id = ?",
            (d["id"],),
        ).fetchone()[0]
        items.append(d)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/factcheck/{fc_id}")
def get_factcheck(
    fc_id: int,
    user: Optional[dict] = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    sql = """SELECT id, category, claim_date, claim, what_actually_happened, type,
                    topic, source, source_article_ids, evidence_quotes, confidence,
                    created_at, published, public_summary
             FROM fact_checks WHERE id = ?""" + _public_filter(user)
    r = conn.execute(sql, (fc_id,)).fetchone()
    if not r:
        raise HTTPException(404, f"fact_check {fc_id} not found")
    d = dict(r)
    try:
        d["source_article_ids"] = json.loads(d["source_article_ids"] or "[]")
    except Exception:
        d["source_article_ids"] = []
    try:
        d["evidence_quotes"] = json.loads(d["evidence_quotes"] or "[]")
    except Exception:
        d["evidence_quotes"] = []
    ev = conn.execute(
        """SELECT id, url, title, snippet, relevance, summary, retrieved_at
           FROM fact_check_evidence WHERE fact_check_id = ? ORDER BY id""",
        (fc_id,),
    ).fetchall()
    d["web_evidence"] = [dict(e) for e in ev]
    # source articles
    if d["source_article_ids"]:
        placeholders = ",".join("?" * len(d["source_article_ids"]))
        rows = conn.execute(
            f"SELECT id, title, published_date, category FROM articles "
            f"WHERE id IN ({placeholders}) AND language='EN'",
            d["source_article_ids"],
        ).fetchall()
        d["source_articles"] = [dict(x) for x in rows]
    else:
        d["source_articles"] = []
    return d
