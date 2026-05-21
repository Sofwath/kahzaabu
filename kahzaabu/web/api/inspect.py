# SPDX-License-Identifier: Apache-2.0
"""GET /api/article/{id}/factcard and /api/compare endpoints.

Public-mode aware: anonymous viewers only see items with published=1.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query

from ... import claims_db
from ..db_dep import get_db

router = APIRouter()

@router.get("/recent-factcards")
def recent_factcards(
    limit: int = Query(5, ge=1, le=30),
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    where = " WHERE published = 1"
    rows = conn.execute(
        f"""SELECT id, article_id, language, summary, severity, created_at, published
            FROM article_fact_cards{where}
            ORDER BY id DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    return {"items": [dict(r) for r in rows]}

@router.get("/article/{article_id}/factcard")
def get_factcard(article_id: int, language: str = "EN",
                 conn: sqlite3.Connection = Depends(get_db)) -> dict:
    fc = claims_db.get_fact_card(conn, article_id, language)
    if not fc:
        return {"exists": False, "article_id": article_id, "language": language}
    if True and not fc.get("published"):
        return {"exists": False, "article_id": article_id, "language": language,
                "_hidden": "not_published"}
    fc["exists"] = True
    return fc

@router.get("/compare")
def list_compare(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 severity: Optional[str] = None,
                 conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """List EN articles that have been DV-compared, with their issue counts."""
    sql = """SELECT p.en_article_id, p.dv_article_id, p.n_inconsistencies,
                    p.max_severity, p.compared_at,
                    a.title, a.published_date
             FROM dv_compare_pairs p
             JOIN articles a ON a.id = p.en_article_id AND a.language='EN'
             WHERE 1=1"""
    params: list = []
    sql += " AND p.published = 1"
    if severity:
        sql += " AND p.max_severity = ?"
        params.append(severity)
    count_sql = "SELECT COUNT(*) FROM (" + sql + ")"
    total = conn.execute(count_sql, params).fetchone()[0]
    sql += " ORDER BY p.n_inconsistencies DESC, p.compared_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [dict(r) for r in rows]}

@router.get("/compare/{en_article_id}")
def get_compare(en_article_id: int,
                conn: sqlite3.Connection = Depends(get_db)) -> dict:
    sql = """SELECT p.*, a.title, a.published_date, a.body_text AS en_body,
                    dv.body_text AS dv_body
             FROM dv_compare_pairs p
             JOIN articles a  ON a.id  = p.en_article_id AND a.language='EN'
             JOIN articles dv ON dv.id = p.dv_article_id AND dv.language='DV'
             WHERE p.en_article_id = ?"""
    sql += " AND p.published = 1"
    pair = conn.execute(sql, (en_article_id,)).fetchone()
    if not pair:
        return {"exists": False, "en_article_id": en_article_id}
    pair = dict(pair)
    incs = conn.execute(
        """SELECT id, severity, category, en_quote, dv_quote, dv_translation_to_en, explanation
           FROM dv_en_inconsistencies WHERE en_article_id = ? ORDER BY
             CASE severity WHEN 'serious' THEN 0 WHEN 'moderate' THEN 1 ELSE 2 END,
             id""",
        (en_article_id,)
    ).fetchall()
    pair["inconsistencies"] = [dict(i) for i in incs]
    pair["exists"] = True
    return pair
