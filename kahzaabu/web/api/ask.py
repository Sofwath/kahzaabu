# SPDX-License-Identifier: Apache-2.0
"""POST /api/ask — agentic natural-language Q&A.

Wraps qna_agentic.ask_agentic() (multi-tool agent with web_search + session memory).

Hardening for public deployment:
- rate-limited to 10/min per IP
- 500-char hard cap on question
- daily cap on /api/ask spend (env KAHZAABU_ASK_DAILY_CAP_USD) —
  applies to ALL callers post-ADR-0013 (no in-app auth)
- LRU cache for repeat questions WITHOUT session_id (1h TTL)
- session_id round-trip lets the front-end continue a conversation
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ... import claims_db
from ..db_dep import get_db
from ..limits import ASK_DAILY_CAP_USD, ask_cache, limiter

router = APIRouter()

class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    session_id: Optional[str] = Field(default=None, max_length=64)
    enable_web: bool = True
    max_iterations: int = Field(default=5, ge=1, le=8)

@router.post("/ask")
@limiter.limit("10/minute")
def ask(request: Request, req: AskRequest,
        conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured on server")

    # Daily cap applies to ALL callers — ADR 0013 removed in-app
    # auth, so there's no "admin bypass" tier any more.
    daily = claims_db.daily_spend(conn)
    if daily >= ASK_DAILY_CAP_USD:
        raise HTTPException(
            503,
            f"daily question budget exhausted "
            f"(${daily:.2f} / ${ASK_DAILY_CAP_USD:.2f}). Try again tomorrow."
        )

    # Cache only for first-turn questions (no session_id). Following turns vary.
    # Include PROMPT_VERSION so prompt/format changes auto-invalidate old entries.
    if not req.session_id:
        from ...qna_agentic import PROMPT_VERSION
        key = (f"{PROMPT_VERSION}|{req.question.strip().lower()}|"
               f"{req.enable_web}|{req.max_iterations}")
        cached = ask_cache.get(key)
        if cached:
            out = dict(cached)
            out["_cached"] = True
            return out
    else:
        key = None

    from ...qna_agentic import ask_agentic
    try:
        res = ask_agentic(
            conn, req.question,
            session_id=req.session_id,
            max_iterations=req.max_iterations,
            enable_web=req.enable_web,
            daily_budget_usd=ASK_DAILY_CAP_USD,
        )
    except Exception as e:
        raise HTTPException(500, f"ask failed: {e}")

    out = {
        "question": req.question,
        "answer": res["answer"],
        "session_id": res["session_id"],
        "n_iterations": res["n_iterations"],
        "cost_usd": res["cost_usd"],
        "web_searches": res.get("web_searches", 0),
        "tool_trace": res.get("tool_trace", []),
    }
    if key:
        ask_cache.set(key, out)
    return out
