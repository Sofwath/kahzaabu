# SPDX-License-Identifier: Apache-2.0
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
from kahzaabu import registry

router = APIRouter()

ALLOWED_CATEGORIES = (
    "LIE", "CONTRADICTION", "MISLEADING", "SHIFTING NUMBERS",
    "CREDIT THEFT", "BROKEN DEADLINE"
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
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    sql = """SELECT id, category, claim_date, claim, what_actually_happened, type,
                    topic, source, source_article_ids, evidence_quotes, created_at,
                    published, public_summary,
                    verdict_label, truth_score, truth_score_label,
                    contradiction_pair_id, speaker
             FROM fact_checks WHERE 1=1""" + " AND published = 1"
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
            (d["id"],)
        ).fetchone()[0]
        items.append(d)
    return {"total": total, "limit": limit, "offset": offset, "items": items}

@router.get("/factcheck/{fc_id}")
def get_factcheck(
    fc_id: int,
    conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    sql = """SELECT id, category, claim_date, claim, what_actually_happened, type,
                    topic, source, source_article_ids, evidence_quotes, confidence,
                    created_at, published, public_summary,
                    verdict_label, truth_score, truth_score_label, reasoning_chain,
                    contradiction_pair_id, speaker
             FROM fact_checks WHERE id = ?""" + " AND published = 1"
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
    try:
        d["reasoning_chain"] = json.loads(d["reasoning_chain"] or "[]")
    except Exception:
        d["reasoning_chain"] = []
    ev = conn.execute(
        """SELECT id, url, title, snippet, relevance, summary, retrieved_at,
                  authoritative_entity_id
           FROM fact_check_evidence WHERE fact_check_id = ? ORDER BY id""",
        (fc_id,)
    ).fetchall()
    web_evidence = []
    for e in ev:
        row = dict(e)
        ent_id = row.get("authoritative_entity_id")
        if ent_id:
            ent = registry.entity_by_id(ent_id)
            row["authoritative_entity"] = {
                "id": ent_id,
                "name": ent["official_name"],
                "domain": ent["domain"],
                "type": ent["entity_type"],
            } if ent else None
        else:
            row["authoritative_entity"] = None
        web_evidence.append(row)
    d["web_evidence"] = web_evidence
    # Also surface a deduped list of authoritative entities for this
    # fact-check, so the UI can render a "Verified against:" header
    # without re-grouping client-side.
    seen = set()
    auth_entities = []
    for row in web_evidence:
        ent = row.get("authoritative_entity")
        if ent and ent["id"] not in seen:
            seen.add(ent["id"])
            auth_entities.append(ent)
    d["authoritative_entities"] = auth_entities
    # source articles
    if d["source_article_ids"]:
        placeholders = ",".join("?" * len(d["source_article_ids"]))
        rows = conn.execute(
            f"SELECT id, title, published_date, category FROM articles "
            f"WHERE id IN ({placeholders}) AND language='EN'",
            d["source_article_ids"]
        ).fetchall()
        d["source_articles"] = [dict(x) for x in rows]
    else:
        d["source_articles"] = []
    return d
