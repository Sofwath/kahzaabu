# SPDX-License-Identifier: Apache-2.0
"""POST /api/translate — press-office-style EN ↔ DV translation
(Slice 16, ADR 0016).

Wraps kahzaabu.translator.translate() — same multi-stage pipeline
(language detect → few-shot select → glossary subset → LLM call →
translation_runs persistence) the CLI uses.

Hardening (mirrors api/ask.py):
- rate-limited to 10/min per IP
- 4000-char hard cap on input text
- daily cap on /api/translate spend (env KAHZAABU_TRANSLATE_DAILY_CAP_USD,
  defaults to ASK_DAILY_CAP_USD/2). Without this, a single user
  spamming refresh could burn the budget.
- translation_runs doubles as the LRU-cache backing store —
  translator.translate() handles cache hits internally, so we don't
  need a separate cache layer here.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ... import claims_db
from ..db_dep import get_db
from ..limits import ASK_DAILY_CAP_USD, limiter

router = APIRouter()


# Daily cap defaults to half the /api/ask cap — translation is
# cheaper per call but can be spammed more easily.
_DEFAULT_TRANSLATE_DAILY_CAP_USD = max(2.0, ASK_DAILY_CAP_USD / 2)

def _translate_daily_cap() -> float:
    try:
        return float(os.environ.get(
            "KAHZAABU_TRANSLATE_DAILY_CAP_USD",
            str(_DEFAULT_TRANSLATE_DAILY_CAP_USD)))
    except ValueError:
        return _DEFAULT_TRANSLATE_DAILY_CAP_USD


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    target_language: Optional[str] = Field(
        default="auto", pattern=r"^(EN|DV|auto)$",
    )
    verify: bool = Field(
        default=False,
        description=(
            "Run a back-translation pass and flag numbers / proper "
            "nouns that drifted. Doubles the per-call cost; off by "
            "default."
        ),
    )


@router.post("/translate")
@limiter.limit("10/minute")
def translate(request: Request, req: TranslateRequest,
              conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Translate `text` to the target language (default: opposite of
    detected source). Returns the same shape as the CLI/tool, plus
    a `disclaimer` field for the frontend to render."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured on server")

    cap = _translate_daily_cap()
    daily = claims_db.daily_spend(conn)
    if daily >= cap:
        raise HTTPException(
            503,
            f"daily translation budget exhausted "
            f"(${daily:.2f} / ${cap:.2f}). Try again tomorrow."
        )

    from ...translator import translate as _do_translate
    try:
        res = _do_translate(
            conn, req.text,
            target_lang=req.target_language,
            verify=req.verify,
        )
    except ValueError as e:
        # detect_language thinks input is already in target_lang, etc.
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, f"translation failed: {e}")
    return res
