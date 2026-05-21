# SPDX-License-Identifier: Apache-2.0
"""GET /api/stats — dashboard counters.

The web UI is read-only public: fact-check counts always reflect the
published subset, since unpublished records aren't visible anywhere.
The article/claim totals stay as full-corpus (those don't have a
publish workflow).
"""
from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends

from ... import claims_db
from ..db_dep import get_db

router = APIRouter()


@router.get("/stats")
def get_stats(
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    s = claims_db.stats(conn)
    s["daily_spend_usd"] = round(claims_db.daily_spend(conn), 4)

    # Fact-check counts — published-only.
    s["n_fact_checks"] = conn.execute(
        "SELECT COUNT(*) FROM fact_checks WHERE published = 1"
    ).fetchone()[0]
    s["n_fact_checks_total"] = conn.execute(
        "SELECT COUNT(*) FROM fact_checks"
    ).fetchone()[0]

    by_cat = conn.execute(
        "SELECT category, COUNT(*) AS n FROM fact_checks "
        "WHERE published = 1 GROUP BY category ORDER BY n DESC"
    ).fetchall()
    s["fact_checks_by_category"] = [dict(r) for r in by_cat]

    by_topic = conn.execute(
        "SELECT topic, COUNT(*) AS n FROM fact_checks "
        "WHERE published = 1 AND topic IS NOT NULL AND topic != '' "
        "GROUP BY topic ORDER BY n DESC LIMIT 12"
    ).fetchall()
    s["fact_checks_by_topic"] = [dict(r) for r in by_topic]

    # Web evidence — only count rows backing published fact-checks.
    s["n_web_evidence"] = conn.execute(
        "SELECT COUNT(*) FROM fact_check_evidence e "
        "JOIN fact_checks f ON f.id = e.fact_check_id "
        "WHERE f.published = 1"
    ).fetchone()[0]
    s["n_factchecks_with_web_evidence"] = conn.execute(
        "SELECT COUNT(DISTINCT e.fact_check_id) FROM fact_check_evidence e "
        "JOIN fact_checks f ON f.id = e.fact_check_id "
        "WHERE f.published = 1"
    ).fetchone()[0]

    # Manifesto — likewise.
    try:
        s["n_manifesto_promises"] = conn.execute(
            "SELECT COUNT(*) FROM manifesto_promises WHERE published = 1"
        ).fetchone()[0]
        s["manifesto_by_status"] = {
            r[0]: r[1] for r in conn.execute(
                "SELECT delivery_status, COUNT(*) FROM manifesto_promises "
                "WHERE published = 1 GROUP BY delivery_status"
            )
        }
    except Exception:
        s["n_manifesto_promises"] = 0
        s["manifesto_by_status"] = {}

    # Freshness banner for the dashboard.
    s["freshness"] = claims_db.freshness(
        conn,
        stale_hours=float(os.environ.get("KAHZAABU_STALE_HOURS", "24")),
    )
    return s
