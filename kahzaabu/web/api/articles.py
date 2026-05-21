# SPDX-License-Identifier: Apache-2.0
"""Articles endpoints."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..db_dep import get_db

router = APIRouter()

@router.get("/articles")
def list_articles(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    language: str = "EN",
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    sql = """SELECT id, language, category, title, published_date,
                    SUBSTR(body_text, 1, 240) AS snippet
             FROM articles
             WHERE language = ?
               AND body_text IS NOT NULL AND body_text != ''
               AND published_date >= '2023-11-17'"""
    params: list = [language]
    if date_from:
        sql += " AND published_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND published_date <= ?"
        params.append(date_to)
    if category:
        sql += " AND category = ?"
        params.append(category)
    if q:
        sql += " AND (title LIKE ? OR body_text LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    # Count total
    count_sql = "SELECT COUNT(*) FROM (" + sql + ")"
    total = conn.execute(count_sql, params).fetchone()[0]
    sql += " ORDER BY published_date DESC, id DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }

@router.get("/article/{article_id}")
def get_article(
    article_id: int,
    language: str = "EN",
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    art = conn.execute(
        """SELECT id, language, paired_id, category, title, body_text,
                  body_html, reference, published_date, image_urls, scraped_at
           FROM articles WHERE id = ? AND language = ?""",
        (article_id, language)
    ).fetchone()
    if not art:
        raise HTTPException(404, f"article {article_id} ({language}) not found")
    art = dict(art)
    # parse image_urls
    try:
        art["image_urls"] = json.loads(art.get("image_urls") or "[]")
    except Exception:
        art["image_urls"] = []
    # claims for this article
    claims = conn.execute(
        """SELECT type, subject, value, deadline, actor_credited, quote, created_at
           FROM claims
           WHERE article_id = ? AND language = ? AND type != 'no_specific_claims'
           ORDER BY id""",
        (article_id, language)
    ).fetchall()
    art["claims"] = [dict(c) for c in claims]
    # fact_checks referencing this article — always published-only
    # (the web UI is read-only public; there's no admin/editor anymore).
    fc_sql = """SELECT id, category, claim_date, claim, what_actually_happened, type,
                       topic, source, source_article_ids, evidence_quotes, published
                FROM fact_checks
                WHERE source_article_ids LIKE ? AND published = 1"""
    rows = conn.execute(fc_sql, [f"%{article_id}%"]).fetchall()
    fcs = []
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
        # Only include if article_id is actually in the parsed list (avoid substring false hits like 36 vs 3677)
        if article_id in d["source_article_ids"]:
            # attach web evidence
            ev = conn.execute(
                """SELECT url, title, snippet, relevance, summary, retrieved_at
                   FROM fact_check_evidence WHERE fact_check_id = ? ORDER BY id""",
                (d["id"],)
            ).fetchall()
            d["web_evidence"] = [dict(e) for e in ev]
            fcs.append(d)
    art["fact_checks"] = fcs
    # paired article (DV/EN counterpart)
    if art.get("paired_id"):
        paired = conn.execute(
            "SELECT id, language, title, published_date FROM articles WHERE id = ?",
            (art["paired_id"],)
        ).fetchone()
        art["paired"] = dict(paired) if paired else None
    return art
