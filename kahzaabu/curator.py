"""Incremental fact-check curation — looks at newly-added claims, finds new findings."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import anthropic

from . import claims_db

logger = logging.getLogger("kahzaabu")

MODEL = "claude-sonnet-4-6"
PRICE_IN_PER_M = 3.0
PRICE_OUT_PER_M = 15.0
TODAY = date.today().isoformat()

# Reuse the topic taxonomy from phase4
TOPICS = {
    "housing": re.compile(r"\b(hous(?:e|ing)|flat|hectare|gulhifalhu|hulhumal|ras\s*mal|uthuruthilafalhu|reclamat|BML|land plot|Hiya|residen|Affordable)\b", re.I),
    "fiscal_debt": re.compile(r"\b(debt|deficit|budget|fiscal|sukuk|reserve|GDP|EXIM|loan|swap|austerity|MVR|USD\s*\d|currency)\b", re.I),
    "infrastructure": re.compile(r"\b(airport|terminal|hospital|bridge|harbour|harbor|port|road|ferry|RTL|sewer|water|cold storage|Felivaru|Ihavandhippolhu|Dharumavantha)\b", re.I),
    "tourism": re.compile(r"\b(resort|tourism|bed[s]?|tourist|arrival)\b", re.I),
    "energy": re.compile(r"\b(MW(?:p|h)?|solar|electricity|fuel|oil|renewable|grid|power|net-zero)\b", re.I),
    "diplomatic_india_china": re.compile(r"\b(india|china|EXIM|line of credit|bilateral|state visit|foreign military|UNGA|ICJ)\b", re.I),
    "social_education": re.compile(r"\b(school|education|student|university|teacher|mental health|Aasandha|Braille|Zakat|medical)\b", re.I),
    "sports_youth": re.compile(r"\b(sports?|futsal|stadium|athletic|youth|football)\b", re.I),
    "governance_legal": re.compile(r"\b(Act No|Bill|decree|amendment|ratif|councils?|elections?|judic|court|legal|referendum|terror)\b", re.I),
    "fisheries": re.compile(r"\b(fisher|tuna|MIFCO|fishing|fleet)\b", re.I),
}


def _topic_for(text: str) -> str:
    for t, p in TOPICS.items():
        if p.search(text):
            return t
    return "other"


CURATION_SYSTEM = """You are curating new fact-check items for a Maldives Presidency archive.

Inputs:
- existing_fact_checks: items ALREADY recorded (do NOT duplicate).
- new_claims: structured claims to evaluate.

Today is {today}. Muizzu admin began 2023-11-17. Previous (Solih) admin: 2018-11-17 to 2023-11-17.

Output ONLY high-confidence, specific fact-check items where one of these is clearly true:
  - BROKEN DEADLINE
  - SHIFTING NUMBERS
  - CREDIT THEFT
  - CONTRADICTION
  - MISLEADING
  - LIE

Be CONSERVATIVE. DO NOT include vague rhetoric. DO NOT duplicate existing items (check carefully).
Each item MUST cite at least one article_id and a verbatim quote.

Return STRICT JSON: {{"new_items": [{{
  "category": "...",
  "date": "YYYY-MM-DD",
  "claim": "concise summary (<=200 chars)",
  "what_actually_happened": "evidence-based explanation citing article_ids/dates/quotes",
  "type": "type tag",
  "source_article_ids": [int, ...],
  "evidence_quotes": ["...verbatim...", ...]
}}]}}

If no qualifying items, return {{"new_items": []}}."""


def _existing_for_topic(existing_master: list[dict], topic: str) -> list[dict]:
    return [m for m in existing_master if _topic_for((m.get("claim") or "") + " " + (m.get("what_actually_happened") or "")) == topic]


def _compact_claims(claims) -> list[dict]:
    out = []
    for c in claims:
        # c is either a dict or sqlite3.Row; both support item access
        out.append({
            "article_id": c["article_id"],
            "date": (c["date"] if "date" in c.keys() else None) if hasattr(c, "keys") else c.get("date"),
            "type": c["type"],
            "subject": c["subject"],
            "value": c["value"],
            "deadline": c["deadline"],
            "actor_credited": c["actor_credited"],
            "quote": (c["quote"] or "")[:200] if c["quote"] else None,
        })
    return out


def _curate_chunk(client: anthropic.Anthropic, topic: str, chunk_idx: int,
                  claims: list[dict], existing: list[dict], retries: int = 3) -> dict:
    user = (
        f"Topic: {topic} (chunk {chunk_idx + 1})\n\n"
        f"EXISTING fact-checks in this topic ({len(existing)}):\n"
        f"{json.dumps(existing, ensure_ascii=False)[:10000]}\n\n"
        f"NEW claims to evaluate ({len(claims)}):\n"
        f"{json.dumps(claims, ensure_ascii=False)[:30000]}\n\n"
        f"Return the JSON object now."
    )
    sys_prompt = CURATION_SYSTEM.format(today=TODAY)
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=6000, system=sys_prompt,
                messages=[{"role": "user", "content": user}],
            )
            text = r.content[0].text
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                return {"topic": topic, "chunk": chunk_idx, "new_items": [],
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens,
                        "_parse_error": True}
            d = json.loads(m.group(0))
            return {"topic": topic, "chunk": chunk_idx, "new_items": d.get("new_items", []),
                    "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"topic": topic, "chunk": chunk_idx, "new_items": [], "_error": str(e)[:200]}
            time.sleep(2 ** attempt)


def run_curation(conn, *, days_back: int = 7, max_chunk_claims: int = 200,
                 concurrency: int = 4, daily_budget_usd: float = 1.0,
                 force_full: bool = False, progress_cb=None) -> dict:
    """Curate fact-checks from recently-extracted claims.

    days_back: only consider claims created in the last N days (or force_full=True for all).
    """
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    claims_db.init_claims_schema(conn)

    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        logger.warning(f"daily budget ${daily_budget_usd:.2f} already spent; skipping curation")
        return {"skipped": True, "reason": "budget", "today_spent": today_spent}

    # Pull claims to consider
    if force_full:
        rows = conn.execute(
            """SELECT c.*, a.title, a.published_date
               FROM claims c JOIN articles a ON c.article_id = a.id AND c.language = a.language
               WHERE c.type != 'no_specific_claims'
               ORDER BY a.published_date, c.id"""
        ).fetchall()
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        rows = conn.execute(
            """SELECT c.*, a.title, a.published_date
               FROM claims c JOIN articles a ON c.article_id = a.id AND c.language = a.language
               WHERE c.created_at >= ? AND c.type != 'no_specific_claims'
               ORDER BY a.published_date, c.id""",
            (since,),
        ).fetchall()

    new_claims = [dict(r) for r in rows]
    if not new_claims:
        logger.info("no new claims to curate")
        return {"new_items": 0, "cost_usd": 0.0}

    # Annotate claims with topic and use article date as 'date'
    for c in new_claims:
        text = (c.get("subject") or "") + " " + (c.get("quote") or "")
        c["_topic"] = _topic_for(text)
        c["date"] = c.get("published_date", "")[:10]

    # Existing fact_checks become anti-duplication context (per topic)
    existing_rows = claims_db.all_fact_checks(conn)
    existing_master = [
        {
            "category": r["category"], "date": r["claim_date"], "claim": r["claim"],
            "what_actually_happened": r["what_actually_happened"],
        }
        for r in existing_rows
    ]

    # Group new claims by topic, then chunk
    by_topic = defaultdict(list)
    for c in new_claims:
        by_topic[c["_topic"]].append(c)
    logger.info(f"curation: {len(new_claims)} claims across {len(by_topic)} topics "
                f"(existing master: {len(existing_master)} items)")

    tasks = []
    for topic, claims in by_topic.items():
        if topic == "other" or len(claims) < 5:
            continue
        # Sort by date, split into chunks
        claims.sort(key=lambda c: c.get("date", ""))
        for i in range(0, len(claims), max_chunk_claims):
            tasks.append((topic, i // max_chunk_claims, claims[i: i + max_chunk_claims]))

    if not tasks:
        logger.info("not enough claims per topic to curate")
        return {"new_items": 0, "cost_usd": 0.0}

    run_id = claims_db.start_curation_run(conn)
    client = anthropic.Anthropic()
    cost_in = cost_out = 0
    all_new_items: list[dict] = []

    def worker(t, i, claims):
        compact = _compact_claims(claims)
        existing = _existing_for_topic(existing_master, t)
        return _curate_chunk(client, t, i, compact, existing)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, t, i, c) for t, i, c in tasks]
            for fut in as_completed(futures):
                res = fut.result()
                cost_in += res.get("_in") or 0
                cost_out += res.get("_out") or 0
                for item in res.get("new_items", []):
                    item["_topic"] = res["topic"]
                    all_new_items.append(item)
                if progress_cb:
                    cost = cost_in / 1e6 * PRICE_IN_PER_M + cost_out / 1e6 * PRICE_OUT_PER_M
                    progress_cb(res["topic"], res["chunk"], len(res.get("new_items", [])), cost)
                # Budget check
                cost = cost_in / 1e6 * PRICE_IN_PER_M + cost_out / 1e6 * PRICE_OUT_PER_M
                if cost + today_spent >= daily_budget_usd:
                    logger.warning(f"budget hit during curation (${cost + today_spent:.2f}); stopping")
                    break
    except KeyboardInterrupt:
        cost = cost_in / 1e6 * PRICE_IN_PER_M + cost_out / 1e6 * PRICE_OUT_PER_M
        claims_db.finish_curation_run(
            conn, run_id, chunks_processed=len(all_new_items), new_items=len(all_new_items),
            tokens_in=cost_in, tokens_out=cost_out, cost_usd=cost, status="interrupted",
        )
        raise

    # Insert with dedupe
    inserted = 0
    for item in all_new_items:
        new_id = claims_db.insert_fact_check(conn, item, run_id=run_id, source="auto")
        if new_id is not None:
            inserted += 1

    cost = cost_in / 1e6 * PRICE_IN_PER_M + cost_out / 1e6 * PRICE_OUT_PER_M
    claims_db.finish_curation_run(
        conn, run_id, chunks_processed=len(tasks), new_items=inserted,
        tokens_in=cost_in, tokens_out=cost_out, cost_usd=cost, status="completed",
    )
    logger.info(f"curation done: {inserted} new fact-checks (of {len(all_new_items)} proposed, "
                f"{len(all_new_items) - inserted} dupes), cost=${cost:.2f}")
    return {
        "run_id": run_id,
        "chunks": len(tasks),
        "proposed": len(all_new_items),
        "inserted": inserted,
        "duplicates": len(all_new_items) - inserted,
        "tokens_in": cost_in,
        "tokens_out": cost_out,
        "cost_usd": cost,
    }


