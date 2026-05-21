# SPDX-License-Identifier: Apache-2.0
"""Admin-only endpoints: review queue, publish/reject."""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ... import claims_db
from ..db_dep import get_db
from .auth import require_admin

router = APIRouter()


class PublishRequest(BaseModel):
    publish: bool = True
    public_summary: Optional[str] = Field(default=None, max_length=1500)


class CorrectionReviewRequest(BaseModel):
    status: str = Field(..., pattern="^(reviewed|rejected|open)$")
    review_notes: Optional[str] = Field(default=None, max_length=1500)


@router.get("/admin/queue")
def review_queue(
    limit: int = Query(50, ge=1, le=200),
    category: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    sql = """SELECT id, category, claim_date, claim, what_actually_happened, type,
                    topic, source, source_article_ids, evidence_quotes,
                    published, public_summary, reviewed_at, reviewed_by
             FROM fact_checks WHERE published = 0"""
    params: list = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    # Sort: most severe categories first
    sql += """ ORDER BY
        CASE category
            WHEN 'LIE' THEN 0
            WHEN 'CONTRADICTION' THEN 1
            WHEN 'CREDIT THEFT' THEN 2
            WHEN 'SHIFTING NUMBERS' THEN 3
            WHEN 'MISLEADING' THEN 4
            ELSE 5
        END, claim_date DESC, id LIMIT ?"""
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out = []
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
        d["n_evidence"] = conn.execute(
            "SELECT COUNT(*) FROM fact_check_evidence WHERE fact_check_id = ?",
            (d["id"],),
        ).fetchone()[0]
        out.append(d)
    total_pending = conn.execute(
        "SELECT COUNT(*) FROM fact_checks WHERE published = 0"
    ).fetchone()[0]
    return {"total_pending": total_pending, "items": out, "reviewer": user["u"]}


@router.post("/admin/factcheck/{fc_id}/publish")
def publish_factcheck(
    fc_id: int, req: PublishRequest,
    user: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    n = claims_db.set_fact_check_published(
        conn, fc_id,
        published=req.publish,
        reviewed_by=user["u"],
        public_summary=req.public_summary,
    )
    if n == 0:
        raise HTTPException(404, f"fact_check {fc_id} not found")
    return {"ok": True, "fc_id": fc_id, "published": req.publish}


@router.post("/admin/factcheck/bulk-publish")
def bulk_publish(
    ids: List[int], user: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    if not ids or len(ids) > 200:
        raise HTTPException(400, "provide 1-200 ids")
    placeholders = ",".join("?" * len(ids))
    n = conn.execute(
        f"UPDATE fact_checks SET published = 1, reviewed_at = ?, reviewed_by = ? "
        f"WHERE id IN ({placeholders})",
        [claims_db.now_iso(), user["u"]] + ids,
    ).rowcount
    conn.commit()
    return {"ok": True, "updated": n}


@router.get("/admin/corrections")
def list_corrections(
    status: Optional[str] = "open", limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    sql = "SELECT * FROM corrections WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/admin/correction/{cid}/review")
def review_correction(
    cid: int, req: CorrectionReviewRequest,
    user: dict = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    n = conn.execute(
        """UPDATE corrections SET status = ?, reviewed_at = ?, reviewed_by = ?,
                                   review_notes = ?
           WHERE id = ?""",
        (req.status, claims_db.now_iso(), user["u"], req.review_notes, cid),
    ).rowcount
    conn.commit()
    if n == 0:
        raise HTTPException(404, f"correction {cid} not found")
    return {"ok": True}


@router.post("/admin/pipeline/run")
def trigger_pipeline(
    user: dict = Depends(require_admin),
    no_scrape: bool = False, no_extract: bool = False,
    no_inspect: bool = False, no_curate: bool = False,
    no_verify: bool = False, no_dv_compare: bool = False,
    budget: float = 1.0,
) -> dict:
    """Trigger a one-shot pipeline run from the admin UI."""
    from pathlib import Path
    from ... import pipeline as kpipeline
    # Defer to a thread? For now, run synchronously and return result.
    # In production this should be async/queued, but for v1 sync is fine
    # (a typical cycle when caught up is <30s).
    db_path = Path(__file__).resolve().parents[3] / "data" / "kahzaabu.db"
    res = kpipeline.run_pipeline(
        db_path,
        scrape=not no_scrape, extract=not no_extract,
        inspect_stage=not no_inspect, curate=not no_curate,
        verify=not no_verify, dv_compare_stage=not no_dv_compare,
        daily_budget_usd=budget,
    )
    return {"ok": True, "result": res, "triggered_by": user["u"]}
