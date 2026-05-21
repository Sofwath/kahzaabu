# SPDX-License-Identifier: Apache-2.0
"""Pre-computed series for Chart.js front-end.

In public mode, fact-check viz endpoints only count published items for
anonymous viewers (admins/editors see everything).
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..db_dep import get_db

router = APIRouter()

@router.get("/claims-per-month")
def claims_per_month(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = conn.execute(
        """SELECT SUBSTR(a.published_date,1,7) AS month, COUNT(*) AS n
           FROM claims c JOIN articles a ON c.article_id=a.id AND c.language=a.language
           WHERE c.type != 'no_specific_claims'
             AND a.language='EN' AND a.published_date >= '2023-11-17'
           GROUP BY month ORDER BY month"""
    ).fetchall()
    return {
        "labels": [r["month"] for r in rows],
        "values": [r["n"] for r in rows],
    }

@router.get("/factchecks-by-category")
def fc_by_category(
                   conn: sqlite3.Connection = Depends(get_db)) -> dict:
    where = " WHERE published = 1"
    rows = conn.execute(
        f"SELECT category, COUNT(*) AS n FROM fact_checks{where} "
        "GROUP BY category ORDER BY n DESC"
    ).fetchall()
    return {"labels": [r["category"] for r in rows], "values": [r["n"] for r in rows]}

# ADR 0005 — Truth-O-Meter ladder distribution.
# Returns the PolitiFact 6-rung ladder in its canonical order, even
# for rungs with zero counts. The dashboard renders this as a stack.
@router.get("/truth-score-ladder")
def truth_score_ladder(
                        conn: sqlite3.Connection = Depends(get_db)) -> dict:
    LADDER = ["TRUE", "MOSTLY_TRUE", "HALF_TRUE",
              "MOSTLY_FALSE", "FALSE", "PANTS_ON_FIRE"]
    where = " WHERE published = 1"
    rows = dict(conn.execute(
        f"SELECT truth_score_label, COUNT(*) FROM fact_checks{where} "
        "GROUP BY truth_score_label"
    ).fetchall())
    return {
        "labels": LADDER,
        "values": [int(rows.get(k, 0) or 0) for k in LADDER],
        "_NULL":  int(rows.get(None, 0) or 0),
    }

@router.get("/factchecks-by-month")
def fc_by_month(
                conn: sqlite3.Connection = Depends(get_db)) -> dict:
    pub = " AND published = 1"
    rows = conn.execute(
        f"""SELECT SUBSTR(claim_date,1,7) AS month,
                   SUM(CASE WHEN category='LIE' THEN 1 ELSE 0 END) AS lie,
                   SUM(CASE WHEN category='CONTRADICTION' THEN 1 ELSE 0 END) AS contradiction,
                   SUM(CASE WHEN category='MISLEADING' THEN 1 ELSE 0 END) AS misleading,
                   SUM(CASE WHEN category='SHIFTING NUMBERS' THEN 1 ELSE 0 END) AS shifting,
                   SUM(CASE WHEN category='CREDIT THEFT' THEN 1 ELSE 0 END) AS credit_theft,
                   SUM(CASE WHEN category='BROKEN DEADLINE' THEN 1 ELSE 0 END) AS broken_deadline
            FROM fact_checks
            WHERE claim_date >= '2023-11-17'{pub}
            GROUP BY month ORDER BY month"""
    ).fetchall()
    return {
        "labels": [r["month"] for r in rows],
        "series": {
            "LIE":              [r["lie"] for r in rows],
            "CONTRADICTION":    [r["contradiction"] for r in rows],
            "MISLEADING":       [r["misleading"] for r in rows],
            "SHIFTING NUMBERS": [r["shifting"] for r in rows],
            "CREDIT THEFT":     [r["credit_theft"] for r in rows],
            "BROKEN DEADLINE":  [r["broken_deadline"] for r in rows],
        },
    }

@router.get("/topics")
def topics(
           conn: sqlite3.Connection = Depends(get_db)) -> dict:
    pub = "published = 1 AND "
    rows = conn.execute(
        f"""SELECT topic, COUNT(*) AS n FROM fact_checks
            WHERE {pub}topic IS NOT NULL AND topic != ''
            GROUP BY topic ORDER BY n DESC LIMIT 15"""
    ).fetchall()
    return {"labels": [r["topic"] for r in rows], "values": [r["n"] for r in rows]}

@router.get("/articles-per-month")
def articles_per_month(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = conn.execute(
        """SELECT SUBSTR(published_date,1,7) AS month, COUNT(*) AS n
           FROM articles
           WHERE language='EN' AND body_text IS NOT NULL AND body_text != ''
             AND published_date >= '2023-11-17'
             AND category IN ('press_release','speech','vp_speech')
           GROUP BY month ORDER BY month"""
    ).fetchall()
    return {
        "labels": [r["month"] for r in rows],
        "values": [r["n"] for r in rows],
    }
