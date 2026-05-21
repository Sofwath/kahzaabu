# SPDX-License-Identifier: Apache-2.0
"""Web-search-backed fact-check verification.

For each fact-check item, ask Claude to search the web for related news/sources,
then judge whether the evidence confirms, contradicts, or is inconclusive.
All evidence is stored regardless of outcome.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import anthropic

from . import claims_db
from . import pricing
from . import metrics

logger = logging.getLogger("kahzaabu")

MODEL = pricing.MODELS["haiku-ws"].id  # Haiku + web_search server tool
PRICE_IN_PER_M = pricing.MODELS["haiku-ws"].in_per_m
PRICE_OUT_PER_M = pricing.MODELS["haiku-ws"].out_per_m
WEB_SEARCH_PRICE_PER_SEARCH = pricing.MODELS["haiku-ws"].web_search_per_call
DEFAULT_MAX_SEARCHES = 2  # halve from 4; cuts result-token volume too

SYSTEM = """You are a fact-checking researcher.

You will receive a fact-check item about the Maldives Presidency (Muizzu administration, in office since 2023-11-17).

Your job:
1. Use web search to find news articles, official statements, or third-party reporting relevant to the claim.
2. Prefer reputable Maldivian news outlets (Mihaaru, Sun.mv, Edition.mv, AvasOnline) plus international wires (Reuters, AP, AFP, BBC) and official sources.
3. Stop after at most 4 searches.
4. Return a STRICT JSON object summarizing what you found:

{
  "overall_verdict": "confirmed" | "contradicted" | "partially_confirmed" | "no_relevant_info" | "unclear",
  "summary": "1-2 sentence synthesis of what the web evidence shows",
  "citations": [
    {
      "url": "...",
      "title": "...",
      "snippet": "key phrase from the page",
      "relevance": "confirms" | "contradicts" | "context" | "unclear",
      "relevance_note": "one sentence on what this source contributes"
    }
  ]
}

If web search returns nothing useful, return overall_verdict="no_relevant_info" and citations=[].
Do NOT fabricate URLs. Only include URLs you actually retrieved via web_search.
Return ONLY the JSON, no surrounding prose."""


JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_query(fc: dict) -> str:
    parts = [f"Category: {fc.get('category')}"]
    parts.append(f"Claim date: {fc.get('claim_date')}")
    parts.append(f"Claim: {fc.get('claim')}")
    if fc.get("what_actually_happened"):
        parts.append(f"Internal evidence/note: {fc['what_actually_happened'][:600]}")
    quotes = fc.get("evidence_quotes")
    if quotes:
        try:
            qs = json.loads(quotes) if isinstance(quotes, str) else quotes
            if qs:
                parts.append("Verbatim quotes from source articles:\n" + "\n".join(f"- {q[:200]}" for q in qs[:3]))
        except Exception:
            pass
    parts.append("\nSearch the web for independent reporting or verification.")
    return "\n".join(parts)


def _verify_one(client: anthropic.Anthropic, fc: dict, max_searches: int = DEFAULT_MAX_SEARCHES,
                retries: int = 2) -> dict:
    query = _build_query(fc)
    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_searches,
    }]
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL,
                max_tokens=2500,
                system=SYSTEM,
                tools=tools,
                messages=[{"role": "user", "content": query}],
            )
            # Walk content blocks: collect search calls + final text
            search_calls = 0
            final_text = ""
            for block in r.content:
                btype = getattr(block, "type", None)
                if btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
                    search_calls += 1
                elif btype == "text":
                    final_text += getattr(block, "text", "") + "\n"
            m = JSON_RE.search(final_text)
            verdict = {}
            if m:
                try:
                    verdict = json.loads(m.group(0))
                except Exception as e:
                    verdict = {"overall_verdict": "unclear", "summary": "JSON parse failed",
                               "citations": [], "_parse_error": str(e)[:100]}
            else:
                verdict = {"overall_verdict": "unclear", "summary": final_text[:300],
                           "citations": [], "_no_json": True}

            return {
                "fact_check_id": fc["id"],
                "verdict": verdict,
                "search_calls": search_calls,
                "_in": r.usage.input_tokens,
                "_out": r.usage.output_tokens,
            }
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 3)
        except Exception as e:
            if attempt == retries - 1:
                return {"fact_check_id": fc["id"], "verdict": None,
                        "search_calls": 0, "_error": str(e)[:200]}
            time.sleep(2 ** attempt * 2)


@metrics.tracked_stage("verifier", model=MODEL)
def run_verification(conn, *, limit: Optional[int] = None,
                     categories: tuple[str, ...] = ("LIE", "CONTRADICTION", "SHIFTING NUMBERS", "CREDIT THEFT"),
                     concurrency: int = 3, daily_budget_usd: float = 1.0,
                     max_searches_per_item: int = DEFAULT_MAX_SEARCHES,
                     progress_cb=None) -> dict:
    """Verify fact_checks that haven't yet been verified."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    claims_db.init_claims_schema(conn)

    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        logger.warning(f"daily budget ${daily_budget_usd:.2f} already spent; skipping verification")
        return {"skipped": True, "reason": "budget", "today_spent": today_spent}

    rows = claims_db.fact_checks_needing_verification(conn, categories=categories, limit=limit)
    if not rows:
        logger.info("no fact-checks need verification")
        return {"items_processed": 0, "evidence_collected": 0, "cost_usd": 0.0}

    targets = [dict(r) for r in rows]
    logger.info(f"verification: {len(targets)} items to check")

    run_id = claims_db.start_verification_run(conn)
    client = anthropic.Anthropic()

    tokens_in = tokens_out = web_searches = 0
    evidence_count = 0
    done = 0
    errors = 0

    def worker(idx):
        return idx, _verify_one(client, targets[idx], max_searches=max_searches_per_item)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i) for i in range(len(targets))]
            for fut in as_completed(futures):
                idx, res = fut.result()
                fc = targets[idx]
                if not res or res.get("_error"):
                    errors += 1
                    logger.warning(f"  fc#{fc['id']}: {res.get('_error') if res else 'no result'}")
                else:
                    tokens_in += res.get("_in") or 0
                    tokens_out += res.get("_out") or 0
                    web_searches += res.get("search_calls") or 0
                    verdict = res.get("verdict") or {}
                    overall = verdict.get("overall_verdict") or "unclear"
                    summary = verdict.get("summary") or ""
                    cits = verdict.get("citations") or []
                    if not cits:
                        # Store a single "no_relevant_info" sentinel row so we don't re-check
                        claims_db.insert_evidence(
                            conn, fc["id"], source_type="web",
                            url=None, title=None, snippet=None,
                            relevance=overall if overall in ("no_relevant_info", "unclear") else "not_found",
                            summary=summary or None,
                            verification_run_id=run_id,
                        )
                        evidence_count += 1
                    for c in cits:
                        claims_db.insert_evidence(
                            conn, fc["id"], source_type="web",
                            url=c.get("url"),
                            title=c.get("title"),
                            snippet=c.get("snippet"),
                            relevance=c.get("relevance") or overall,
                            summary=c.get("relevance_note") or summary,
                            verification_run_id=run_id,
                        )
                        evidence_count += 1
                done += 1
                cost = (tokens_in / 1e6 * PRICE_IN_PER_M
                        + tokens_out / 1e6 * PRICE_OUT_PER_M
                        + web_searches * WEB_SEARCH_PRICE_PER_SEARCH)
                if progress_cb:
                    progress_cb(done, len(targets), web_searches, cost)
                if cost + today_spent >= daily_budget_usd:
                    logger.warning(f"budget hit (${cost + today_spent:.2f}); stopping")
                    break
    except KeyboardInterrupt:
        cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                + web_searches * WEB_SEARCH_PRICE_PER_SEARCH)
        claims_db.finish_verification_run(
            conn, run_id, items_processed=done, evidence_collected=evidence_count,
            tokens_in=tokens_in, tokens_out=tokens_out, web_searches=web_searches,
            cost_usd=cost, status="interrupted",
        )
        raise

    cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
            + web_searches * WEB_SEARCH_PRICE_PER_SEARCH)
    claims_db.finish_verification_run(
        conn, run_id, items_processed=done, evidence_collected=evidence_count,
        tokens_in=tokens_in, tokens_out=tokens_out, web_searches=web_searches,
        cost_usd=cost, status="completed",
    )
    logger.info(f"verification done: {done} items, {evidence_count} evidence rows, "
                f"{web_searches} searches, cost=${cost:.2f}")
    return {
        "run_id": run_id,
        "items_processed": done,
        "evidence_collected": evidence_count,
        "web_searches": web_searches,
        "errors": errors,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
    }
