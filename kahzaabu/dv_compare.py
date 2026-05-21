# SPDX-License-Identifier: Apache-2.0
"""EN vs Dhivehi (DV) translation-consistency checker.

For paired articles where both EN and DV body text exist, send both to an LLM
and flag factual differences:
  - numeric_discrepancy : a number differs between versions
  - omission           : a claim present in one is missing in the other
  - softening          : version tones down a claim
  - embellishment      : version adds emphasis/claim not in the other
  - other              : any other notable factual difference

Stores rows in dv_en_inconsistencies, and tracks which pairs have been compared
in dv_compare_pairs (so future runs skip already-checked pairs).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import anthropic

from . import claims_db

logger = logging.getLogger("kahzaabu")

MODEL = "claude-sonnet-4-6"
PRICE_IN_PER_M = 3.0
PRICE_OUT_PER_M = 15.0

TRUNC_BODY = 6000  # chars per language
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


SYSTEM = """You are a forensic translation auditor for Maldives Presidency press releases.

You receive two versions of the same press release: ENGLISH and DHIVEHI.

Your job: list factual differences between them. Ignore stylistic / formatting differences
(line breaks, punctuation, etc.) — only flag SUBSTANTIVE divergences:

Categories:
- "numeric_discrepancy"  : a number, date, percentage, or quantity differs
- "omission"             : a claim/fact present in one version is missing in the other
- "softening"            : one version softens or weakens a claim relative to the other
- "embellishment"        : one version adds emphasis, superlatives, or claims not in the other
- "other"                : any other substantive factual difference

Severity:
- "minor"     : trivial wording or low-impact
- "moderate"  : changes meaning but not dramatically
- "serious"   : changes the factual content materially

Return strict JSON:
{
  "inconsistencies": [
    {
      "severity": "minor|moderate|serious",
      "category": "numeric_discrepancy|omission|softening|embellishment|other",
      "en_quote": "verbatim EN snippet (<=200 chars)",
      "dv_quote": "verbatim DV snippet (<=200 chars) — or empty if omission",
      "dv_translation_to_en": "your literal back-translation of dv_quote, <=200 chars",
      "explanation": "one-sentence explanation of the divergence"
    }
  ]
}

Be conservative — only flag clear factual divergences. Empty list is a valid answer.
If translations match faithfully, return {"inconsistencies": []}.

Return ONLY the JSON."""


def _trim(text: str, limit: int = TRUNC_BODY) -> str:
    text = (text or "").strip()
    return text[:limit] if len(text) > limit else text


def _compare_one(client: anthropic.Anthropic, en_body: str, dv_body: str,
                 retries: int = 3) -> dict:
    user = (
        "ENGLISH:\n\"\"\"\n" + _trim(en_body) + "\n\"\"\"\n\n"
        "DHIVEHI:\n\"\"\"\n" + _trim(dv_body) + "\n\"\"\"\n\n"
        "List factual differences as JSON."
    )
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=3000, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
            m = JSON_RE.search(text)
            if not m:
                return {"_parse_error": True, "_raw": text[:300],
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError as e:
                return {"_parse_error": True, "_err": str(e)[:120],
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            return {"verdict": d,
                    "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"_error": str(e)[:200]}
            time.sleep(2 ** attempt)
    return {"_error": "exhausted retries"}


_SEV_RANK = {"minor": 1, "moderate": 2, "serious": 3}


def run_dv_compare(conn: sqlite3.Connection, *, limit: int = 20,
                   since_date: str = "2024-01-01", require_claims: bool = True,
                   concurrency: int = 3, daily_budget_usd: float = 1.0,
                   progress_cb=None) -> dict:
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    claims_db.init_claims_schema(conn)

    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        logger.warning(f"daily budget ${daily_budget_usd:.2f} already spent; skipping dv-compare")
        return {"skipped": True, "reason": "budget", "today_spent": today_spent}

    pairs = claims_db.pairs_missing_dv_compare(
        conn, since_date=since_date, require_claims=require_claims, limit=limit,
    )
    if not pairs:
        logger.info("no paired articles need DV-EN compare")
        return {"pairs_processed": 0, "cost_usd": 0.0}

    logger.info(f"dv-compare: {len(pairs)} pairs to compare "
                f"(budget remaining: ${daily_budget_usd - today_spent:.2f})")
    run_id = claims_db.start_dv_compare_run(conn)
    client = anthropic.Anthropic()

    todo = [dict(p) for p in pairs]
    tokens_in = tokens_out = 0
    inconsistencies = 0
    pairs_with_issues = 0
    done = 0

    def worker(idx):
        p = todo[idx]
        res = _compare_one(client, p["en_body"], p["dv_body"])
        return idx, res

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i) for i in range(len(todo))]
            for fut in as_completed(futures):
                idx, res = fut.result()
                p = todo[idx]
                tokens_in += res.get("_in") or 0
                tokens_out += res.get("_out") or 0
                verdict = res.get("verdict") or {}
                ins = verdict.get("inconsistencies") or []

                max_sev = None
                for item in ins:
                    sev = (item.get("severity") or "minor").lower()
                    cat = (item.get("category") or "other").lower()
                    claims_db.insert_dv_inconsistency(
                        conn,
                        en_article_id=p["en_article_id"],
                        dv_article_id=p["dv_article_id"],
                        severity=sev, category=cat,
                        en_quote=(item.get("en_quote") or "")[:400],
                        dv_quote=(item.get("dv_quote") or "")[:400],
                        dv_translation_to_en=(item.get("dv_translation_to_en") or "")[:400],
                        explanation=(item.get("explanation") or "")[:400],
                        run_id=run_id,
                    )
                    if not max_sev or _SEV_RANK.get(sev, 0) > _SEV_RANK.get(max_sev, 0):
                        max_sev = sev
                    inconsistencies += 1

                cost_pair = ((res.get("_in") or 0) / 1e6 * PRICE_IN_PER_M
                             + (res.get("_out") or 0) / 1e6 * PRICE_OUT_PER_M)
                claims_db.record_dv_pair(
                    conn,
                    en_article_id=p["en_article_id"],
                    dv_article_id=p["dv_article_id"],
                    n_inconsistencies=len(ins),
                    max_severity=max_sev,
                    run_id=run_id,
                    cost_usd=round(cost_pair, 5),
                )
                if ins:
                    pairs_with_issues += 1
                done += 1

                total_cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M)
                if progress_cb:
                    progress_cb(done, len(todo), inconsistencies, total_cost)
                if total_cost + today_spent >= daily_budget_usd:
                    logger.warning(f"budget hit (${total_cost + today_spent:.2f}); stopping")
                    break
    except KeyboardInterrupt:
        total_cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M)
        claims_db.finish_dv_compare_run(
            conn, run_id, pairs_processed=done, pairs_with_issues=pairs_with_issues,
            inconsistencies_logged=inconsistencies,
            tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=total_cost, status="interrupted",
        )
        raise

    total_cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M)
    claims_db.finish_dv_compare_run(
        conn, run_id, pairs_processed=done, pairs_with_issues=pairs_with_issues,
        inconsistencies_logged=inconsistencies,
        tokens_in=tokens_in, tokens_out=tokens_out,
        cost_usd=total_cost, status="completed",
    )
    logger.info(f"dv-compare done: {done} pairs, {pairs_with_issues} flagged, "
                f"{inconsistencies} inconsistencies, cost=${total_cost:.2f}")
    return {
        "run_id": run_id,
        "pairs_processed": done,
        "pairs_with_issues": pairs_with_issues,
        "inconsistencies_logged": inconsistencies,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": total_cost,
    }
