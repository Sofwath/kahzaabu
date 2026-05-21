"""Public POST /api/corrections — rate-limited submission form backend."""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ... import claims_db
from ..db_dep import get_db
from ..limits import limiter

router = APIRouter()


class CorrectionRequest(BaseModel):
    body: str = Field(..., min_length=10, max_length=3000)
    fact_check_id: Optional[int] = None
    article_id: Optional[int] = None
    reporter_contact: Optional[str] = Field(default=None, max_length=200)


@router.post("/corrections")
@limiter.limit("5/minute")
def submit_correction(
    request: Request, req: CorrectionRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    cid = claims_db.insert_correction(
        conn, body=req.body,
        fact_check_id=req.fact_check_id, article_id=req.article_id,
        reporter_contact=req.reporter_contact,
    )
    return {"ok": True, "id": cid}
