# SPDX-License-Identifier: Apache-2.0
"""Manifesto-promises API endpoints (public)."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db_dep import get_db

router = APIRouter()

@router.get("/manifesto")
def list_promises(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    sql = """SELECT id, section, promise_text_dv, promise_text_en, category, subject,
                    target_value, deadline_stated, delivery_status,
                    delivery_evidence_json, published
             FROM manifesto_promises WHERE 1=1"""
    params: list = []
    sql += " AND published = 1"
    if status:
        sql += " AND delivery_status = ?"
        params.append(status)
    if category:
        sql += " AND category = ?"
        params.append(category)
    if q:
        sql += " AND (promise_text_en LIKE ? OR subject LIKE ? OR promise_text_dv LIKE ?)"
        params += [f"%{q}%"] * 3
    count_sql = "SELECT COUNT(*) FROM (" + sql + ")"
    total = conn.execute(count_sql, params).fetchone()[0]
    sql += """ ORDER BY
        CASE delivery_status
            WHEN 'broken'      THEN 0
            WHEN 'modified'    THEN 1
            WHEN 'abandoned'   THEN 2
            WHEN 'in_progress' THEN 3
            WHEN 'delivered'   THEN 4
            WHEN 'unmentioned' THEN 5
            ELSE 6
        END, id LIMIT ? OFFSET ?"""
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["delivery_evidence"] = json.loads(d.pop("delivery_evidence_json") or "{}")
        except Exception:
            d["delivery_evidence"] = {}
        items.append(d)
    # Status breakdown
    by_status = {}
    for r in conn.execute(
        "SELECT delivery_status, COUNT(*) FROM manifesto_promises "
        "WHERE published = 1 GROUP BY delivery_status"
    ).fetchall():
        by_status[r[0]] = r[1]
    by_cat = {}
    for r in conn.execute(
        "SELECT category, COUNT(*) FROM manifesto_promises "
        "WHERE published = 1 GROUP BY category"
    ).fetchall():
        by_cat[r[0]] = r[1]
    return {"total": total, "limit": limit, "offset": offset, "items": items,
            "by_status": by_status, "by_category": by_cat}

@router.get("/manifesto/{promise_id}")
def get_promise(
    promise_id: int,
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    sql = "SELECT * FROM manifesto_promises WHERE id = ? AND published = 1"
    r = conn.execute(sql, (promise_id,)).fetchone()
    if not r:
        raise HTTPException(404, f"promise {promise_id} not found")
    d = dict(r)
    try:
        d["delivery_evidence"] = json.loads(d.pop("delivery_evidence_json") or "{}")
    except Exception:
        d["delivery_evidence"] = {}
    # Hydrate linked articles + fact_checks
    ev = d.get("delivery_evidence", {})
    art_ids = ev.get("linked_article_ids") or []
    fc_ids = ev.get("linked_fact_check_ids") or []
    if art_ids:
        placeholders = ",".join("?" * len(art_ids))
        rows = conn.execute(
            f"SELECT id, title, published_date, category FROM articles "
            f"WHERE id IN ({placeholders}) AND language='EN'",
            art_ids
        ).fetchall()
        d["linked_articles"] = [dict(x) for x in rows]
    else:
        d["linked_articles"] = []
    if fc_ids:
        placeholders = ",".join("?" * len(fc_ids))
        rows = conn.execute(
            f"SELECT id, category, claim_date, claim FROM fact_checks "
            f"WHERE id IN ({placeholders})",
            fc_ids
        ).fetchall()
        d["linked_fact_checks"] = [dict(x) for x in rows]
    else:
        d["linked_fact_checks"] = []
    return d
