"""GET /api/freshness — when was the archive last scraped?"""
from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends

from ... import claims_db
from ..db_dep import get_db

router = APIRouter()

STALE_HOURS = float(os.environ.get("KAHZAABU_STALE_HOURS", "24"))


@router.get("/freshness")
def freshness(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return claims_db.freshness(conn, stale_hours=STALE_HOURS)
