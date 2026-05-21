# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 12 — Reproducibility manifest API (ADR 0010)."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from ... import reproducibility
from ..db_dep import get_db

router = APIRouter()


@router.get("/reproducibility/{fact_check_id}.json",
             summary="Full provenance trace for one fact-check")
def reproducibility_manifest(
    fact_check_id: int,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Return the full reproducibility manifest for a fact_check.

    Joins the fact_check row with its curation_run, supporting claims
    (with their extraction_runs + articles), decomposition questions,
    verification evidence (with run + model), contradiction_pair (if
    any), cached ClaimReview JSON-LD, and the git commit at publish
    time. See ADR 0010.
    """
    manifest = reproducibility.get_manifest(conn, fact_check_id)
    if manifest is None:
        raise HTTPException(status_code=404,
                             detail=f"fact_check {fact_check_id} not found")
    return manifest
