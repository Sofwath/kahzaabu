# SPDX-License-Identifier: Apache-2.0
"""Shared DB connection dependency for FastAPI routes.

Each request gets its own SQLite connection so cross-thread issues with
FastAPI's threadpool can't bite us. We still set check_same_thread=False
as a belt-and-braces for any internal handoff.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import Request

from .. import claims_db

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "kahzaabu.db"

# Apply schema once at module import — avoids repeating ALTERs per request.
_schema_initialised = False


def _ensure_schema() -> None:
    global _schema_initialised
    if _schema_initialised:
        return
    conn = sqlite3.connect(str(DEFAULT_DB), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        # init_db is in the scraper package; we keep it minimal here
        from .. import db as kdb
        kdb.init_db(conn)
        claims_db.init_claims_schema(conn)
    finally:
        conn.close()
    _schema_initialised = True


def get_db(_request: Request) -> Iterator[sqlite3.Connection]:
    _ensure_schema()
    conn = sqlite3.connect(str(DEFAULT_DB), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
