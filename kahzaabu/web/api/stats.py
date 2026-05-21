"""GET /api/stats — dashboard counters.

In public mode (KAHZAABU_PUBLIC_MODE=1) the fact-check counts reflect ONLY
published items; the article/claim totals stay as full-corpus.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends

from ... import claims_db
from ..db_dep import get_db
from ..limits import PUBLIC_MODE
from .auth import current_user

router = APIRouter()


@router.get("/stats")
def get_stats(
    user: Optional[dict] = Depends(current_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    s = claims_db.stats(conn)
    daily = claims_db.daily_spend(conn)
    s["daily_spend_usd"] = round(daily, 4)

    gated = PUBLIC_MODE and not user
    where = " WHERE published = 1" if gated else ""

    # Total fact-checks: when gated, override to count only published
    if gated:
        s["n_fact_checks"] = conn.execute(
            "SELECT COUNT(*) FROM fact_checks WHERE published = 1"
        ).fetchone()[0]
        s["n_fact_checks_total"] = conn.execute(
            "SELECT COUNT(*) FROM fact_checks"
        ).fetchone()[0]

    by_cat = conn.execute(
        f"SELECT category, COUNT(*) AS n FROM fact_checks{where} "
        "GROUP BY category ORDER BY n DESC"
    ).fetchall()
    s["fact_checks_by_category"] = [dict(r) for r in by_cat]

    by_topic = conn.execute(
        f"""SELECT topic, COUNT(*) AS n FROM fact_checks{where}
            {'AND' if gated else 'WHERE'} topic IS NOT NULL AND topic != ''
            GROUP BY topic ORDER BY n DESC LIMIT 12"""
    ).fetchall()
    s["fact_checks_by_topic"] = [dict(r) for r in by_topic]

    # Web evidence: only count for fact_checks the viewer can see
    ev_sql = "SELECT COUNT(*) FROM fact_check_evidence"
    fcev_sql = "SELECT COUNT(DISTINCT fact_check_id) FROM fact_check_evidence"
    if gated:
        ev_sql = """SELECT COUNT(*) FROM fact_check_evidence e
                    JOIN fact_checks f ON f.id = e.fact_check_id
                    WHERE f.published = 1"""
        fcev_sql = """SELECT COUNT(DISTINCT e.fact_check_id) FROM fact_check_evidence e
                      JOIN fact_checks f ON f.id = e.fact_check_id
                      WHERE f.published = 1"""
    s["n_web_evidence"] = conn.execute(ev_sql).fetchone()[0]
    s["n_factchecks_with_web_evidence"] = conn.execute(fcev_sql).fetchone()[0]
    s["public_mode"] = bool(PUBLIC_MODE)
    s["viewer"] = ("admin" if user and user.get("r") == "admin"
                   else "editor" if user and user.get("r") == "editor"
                   else "anonymous")

    # Manifesto totals
    pub_clause = " WHERE published = 1" if gated else ""
    try:
        s["n_manifesto_promises"] = conn.execute(
            f"SELECT COUNT(*) FROM manifesto_promises{pub_clause}"
        ).fetchone()[0]
        mfs_by_status = conn.execute(
            f"SELECT delivery_status, COUNT(*) FROM manifesto_promises{pub_clause}"
            " GROUP BY delivery_status"
        ).fetchall()
        s["manifesto_by_status"] = {r[0]: r[1] for r in mfs_by_status}
    except Exception:
        s["n_manifesto_promises"] = 0
        s["manifesto_by_status"] = {}
    return s
