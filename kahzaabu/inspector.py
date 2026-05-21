# SPDX-License-Identifier: Apache-2.0
"""Per-article fact-card generator.

For each article without a fact card, generate:
  - 2-3 sentence summary
  - key checkable claims (3-5 from the article)
  - history_check: how this compares to prior statements/fact-checks on same topic
  - severity: 'clean' | 'flag' | 'red_flag'
  - visualization_spec: a Chart.js spec for inline rendering (timeline, before/after,
    or category-pie depending on what's most informative)
  - web_evidence: if severity >= flag, do up to 2 web searches

Cost target: $0.05-0.15 per article (Sonnet for synthesis, Haiku for web if used).
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
from . import pricing
from . import metrics

logger = logging.getLogger("kahzaabu")

MODEL = pricing.MODELS["sonnet"].id
PRICE_IN_PER_M = pricing.MODELS["sonnet"].in_per_m
PRICE_OUT_PER_M = pricing.MODELS["sonnet"].out_per_m
WEB_MODEL = pricing.MODELS["haiku-ws"].id
WEB_IN  = pricing.MODELS["haiku-ws"].in_per_m
WEB_OUT = pricing.MODELS["haiku-ws"].out_per_m
WEB_SEARCH_PRICE = pricing.MODELS["haiku-ws"].web_search_per_call

TRUNC_PR = 4000
TRUNC_SPEECH = 8000
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


SYSTEM = """You are a forensic article inspector for the Maldives Presidency archive.

You receive:
- ARTICLE: a press release or speech from the President's Office (Muizzu administration, started 2023-11-17)
- EXTRACTED_CLAIMS: structured claims already pulled from this article
- HISTORICAL_CONTEXT: prior claims and existing fact-checks on related topics

Today is {today}. Produce STRICT JSON describing your inspection:

{{
  "summary": "2-3 sentence neutral summary of what this article says",
  "key_claims": [
    {{
      "type": "numeric_promise|deadline_promise|credit_claim|policy_assertion|boast|comparison_to_predecessor",
      "subject": "...",
      "value": "...",
      "deadline": "...",
      "quote": "verbatim, <200 chars",
      "checkability": "high|medium|low"
    }}
  ],
  "history_check": "Does anything in this article contradict, repeat, or shift from earlier statements? Be specific — cite prior article_ids/dates from HISTORICAL_CONTEXT where relevant. If nothing notable, say so.",
  "severity": "clean|flag|red_flag",
  "severity_reason": "one-sentence justification",
  "visualization": {{
    "kind": "timeline|before_after|stacked_bars|none",
    "title": "...",
    "data": {{ /* shape depends on kind:
                 timeline: {{labels:[dates], values:[numbers], label:"..."}},
                 before_after: {{prior:{{date,value,quote}}, current:{{date,value,quote}}, subject:"..."}},
                 stacked_bars: {{labels:[...], series:{{name:[...]}}}}, ...
              */ }}
  }}
}}

Severity rubric:
- "clean":   routine reporting, no checkable falsifiable claims of consequence, no conflict with history
- "flag":    contains specific checkable numbers/deadlines/credit-claims that should be verified
- "red_flag":directly contradicts prior statement, claims credit for a previous-govt project, or shifts a previously-stated number

Be CONSERVATIVE about red_flag. Only assign when the conflict is clear and citable.

Return ONLY the JSON object."""


def _trim_body(text: str, category: str) -> str:
    limit = TRUNC_SPEECH if category in ("speech", "vp_speech") else TRUNC_PR
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    p = cut.rfind(". ")
    return cut[: p + 1] if p > limit * 0.7 else cut


def _gather_history(conn: sqlite3.Connection, article: dict, max_items: int = 30) -> dict:
    """Pull claims + fact-checks on the same topics/subjects/numbers as this article."""
    # Approach: use existing claims for THIS article to derive keyword/subject set,
    # then find historical rows mentioning the same keywords.
    own_claims = conn.execute(
        """SELECT type, subject, value, quote FROM claims
           WHERE article_id = ? AND language = ? AND type != 'no_specific_claims'""",
        (article["id"], article["language"]),
    ).fetchall()

    # Build keyword pool from subjects + values
    kws: set[str] = set()
    for c in own_claims:
        for src in (c["subject"], c["value"]):
            if not src:
                continue
            for w in re.findall(r"[A-Za-z]{5,}", src):
                kws.add(w.lower())
    # Cap to 20 most "interesting" — prefer long-ish words
    kws = set(sorted(kws, key=lambda w: -len(w))[:20])

    if not kws:
        return {"prior_claims": [], "prior_fact_checks": []}

    placeholders = " OR ".join(["subject LIKE ?"] * len(kws)) if kws else "0"
    params = [f"%{k}%" for k in kws]
    prior_claims = conn.execute(
        f"""SELECT c.article_id, c.type, c.subject, c.value, c.deadline, c.quote,
                   a.published_date, a.title
            FROM claims c JOIN articles a ON c.article_id = a.id AND c.language = a.language
            WHERE c.language = 'EN' AND c.type != 'no_specific_claims'
              AND a.id != ? AND a.published_date < ?
              AND ({placeholders})
            ORDER BY a.published_date DESC LIMIT ?""",
        [article["id"], article["published_date"][:10]] + params + [max_items],
    ).fetchall()

    placeholders_fc = " OR ".join(["claim LIKE ?"] * len(kws))
    prior_fcs = conn.execute(
        f"""SELECT id, category, claim_date, claim, what_actually_happened, type, topic
            FROM fact_checks
            WHERE ({placeholders_fc})
              AND claim_date < ?
            ORDER BY claim_date DESC LIMIT ?""",
        params + [article["published_date"][:10], 15],
    ).fetchall()

    return {
        "prior_claims": [dict(r) for r in prior_claims],
        "prior_fact_checks": [dict(r) for r in prior_fcs],
    }


def _inspect_one(client: anthropic.Anthropic, article: dict, history: dict,
                 retries: int = 3) -> dict:
    today = claims_db.now_iso()[:10]
    own_claims = [
        {"type": r["type"], "subject": r["subject"], "value": r["value"],
         "deadline": r["deadline"], "quote": (r["quote"] or "")[:200]}
        for r in (article.get("own_claims") or [])
    ]
    user = (
        f"ARTICLE id={article['id']} date={article['published_date'][:10]} category={article['category']}\n"
        f"Title: {article['title']}\n"
        f"Body:\n\"\"\"\n{_trim_body(article['body_text'], article['category'])}\n\"\"\"\n\n"
        f"EXTRACTED_CLAIMS (already parsed from this article):\n{json.dumps(own_claims, ensure_ascii=False)[:5000]}\n\n"
        f"HISTORICAL_CONTEXT — prior claims on similar subjects (oldest cropped):\n"
        f"{json.dumps(history.get('prior_claims', [])[:20], ensure_ascii=False)[:8000]}\n\n"
        f"HISTORICAL_CONTEXT — existing fact-checks on similar subjects:\n"
        f"{json.dumps(history.get('prior_fact_checks', [])[:10], ensure_ascii=False)[:6000]}\n\n"
        "Return the JSON object now."
    )
    sys_p = SYSTEM.format(today=today)
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=2500, system=sys_p,
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


def _web_verify(client: anthropic.Anthropic, fact_card: dict, article: dict,
                max_searches: int = 2) -> tuple[list[dict], dict]:
    """For flag/red_flag articles, do a quick web verification on the most checkable claim."""
    key_claims = fact_card.get("key_claims", [])
    if not key_claims:
        return [], {"tokens_in": 0, "tokens_out": 0, "searches": 0}
    # Pick the most checkable claim
    high = [c for c in key_claims if (c.get("checkability") or "").lower() == "high"]
    target = high[0] if high else key_claims[0]
    quote = target.get("quote") or target.get("subject") or ""
    query = (
        f"Article from Maldives Presidency on {article['published_date'][:10]} states: "
        f"\"{quote}\"\n"
        f"Subject: {target.get('subject')}\n"
        f"Value: {target.get('value')}\n"
        f"Use web search to verify this. Return JSON with overall_verdict and citations."
    )
    system = """You are verifying a single claim using web search.

Up to 2 searches. Prefer Maldivian outlets (Mihaaru, Sun.mv, Edition.mv, AvasOnline) and international wires.

Return strict JSON:
{"overall_verdict": "confirmed|contradicted|partial|no_relevant_info|unclear",
 "summary": "1-2 sentence reading",
 "citations": [{"url":"...","title":"...","snippet":"...","relevance":"confirms|contradicts|context|unclear","relevance_note":"..."}]}
Only include URLs you actually retrieved."""
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}]
    try:
        r = client.messages.create(
            model=WEB_MODEL, max_tokens=2000, system=system, tools=tools,
            messages=[{"role": "user", "content": query}],
        )
    except Exception as e:
        return [], {"_error": str(e)[:120], "tokens_in": 0, "tokens_out": 0, "searches": 0}
    text = ""
    searches = 0
    for block in r.content:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
            searches += 1
        elif btype == "text":
            text += getattr(block, "text", "") + "\n"
    m = JSON_RE.search(text)
    citations = []
    if m:
        try:
            d = json.loads(m.group(0))
            overall = d.get("overall_verdict", "unclear")
            summary = d.get("summary", "")
            cits = d.get("citations", [])
            if not cits:
                citations.append({
                    "url": None, "title": None, "snippet": None,
                    "relevance": overall, "summary": summary,
                })
            for c in cits:
                citations.append({
                    "url": c.get("url"), "title": c.get("title"),
                    "snippet": c.get("snippet"),
                    "relevance": c.get("relevance") or overall,
                    "summary": c.get("relevance_note") or summary,
                })
        except json.JSONDecodeError:
            pass
    usage = {
        "tokens_in": r.usage.input_tokens,
        "tokens_out": r.usage.output_tokens,
        "searches": searches,
    }
    return citations, usage


@metrics.tracked_stage("inspector", model=MODEL)
def run_inspection(conn: sqlite3.Connection, *, limit: Optional[int] = 20,
                   concurrency: int = 4, daily_budget_usd: float = 1.0,
                   web_verify_flagged: bool = True,
                   progress_cb=None) -> dict:
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    claims_db.init_claims_schema(conn)

    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        logger.warning(f"daily budget ${daily_budget_usd:.2f} already spent; skipping inspection")
        return {"skipped": True, "reason": "budget", "today_spent": today_spent}

    todo_rows = claims_db.articles_missing_fact_card(conn, limit=limit)
    if not todo_rows:
        logger.info("no articles missing fact cards")
        return {"cards_generated": 0, "cost_usd": 0.0}

    # Hydrate each row with own_claims for the prompt
    todo = []
    for r in todo_rows:
        d = dict(r)
        d["own_claims"] = [dict(c) for c in conn.execute(
            """SELECT type, subject, value, deadline, quote FROM claims
               WHERE article_id = ? AND language = ? AND type != 'no_specific_claims'""",
            (d["id"], d["language"]),
        ).fetchall()]
        todo.append(d)

    logger.info(f"inspection: {len(todo)} articles to inspect "
                f"(budget remaining: ${daily_budget_usd - today_spent:.2f})")
    run_id = claims_db.start_inspection_run(conn)
    client = anthropic.Anthropic()

    cards = 0
    flagged = 0
    red_flagged = 0
    tokens_in = tokens_out = web_searches = 0
    done = 0

    def worker(idx):
        art = todo[idx]
        # build history context (DB read; can be done in worker thread — sqlite connection is local)
        # We open a fresh connection per worker to avoid threading issues.
        from . import db as kdb
        from pathlib import Path
        # locate the active DB path via conn
        # Note: in tests we always use data/kahzaabu.db
        local = kdb.get_connection(Path("data") / "kahzaabu.db")
        local.row_factory = sqlite3.Row
        try:
            history = _gather_history(local, art)
        finally:
            local.close()
        res = _inspect_one(client, art, history)
        web = []
        web_usage = {"tokens_in": 0, "tokens_out": 0, "searches": 0}
        verdict = res.get("verdict") or {}
        sev = (verdict.get("severity") or "").lower()
        if web_verify_flagged and sev in ("flag", "red_flag") and not res.get("_error"):
            web, web_usage = _web_verify(client, verdict, art)
        return idx, res, web, web_usage

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i) for i in range(len(todo))]
            for fut in as_completed(futures):
                idx, res, web, web_usage = fut.result()
                art = todo[idx]
                tokens_in += res.get("_in") or 0
                tokens_out += res.get("_out") or 0
                tokens_in += web_usage.get("tokens_in") or 0
                tokens_out += web_usage.get("tokens_out") or 0
                web_searches += web_usage.get("searches") or 0

                verdict = res.get("verdict") or {}
                sev = (verdict.get("severity") or "clean").lower()
                if sev == "flag": flagged += 1
                elif sev == "red_flag": red_flagged += 1

                # Per-card cost (rough split: this article's tokens only — we have aggregate, not per-card)
                # Approximate per-call attribution; not perfectly accurate but useful.
                cost_main = ((res.get("_in") or 0) / 1e6 * PRICE_IN_PER_M
                              + (res.get("_out") or 0) / 1e6 * PRICE_OUT_PER_M)
                cost_web = ((web_usage.get("tokens_in") or 0) / 1e6 * WEB_IN
                             + (web_usage.get("tokens_out") or 0) / 1e6 * WEB_OUT
                             + (web_usage.get("searches") or 0) * WEB_SEARCH_PRICE)

                claims_db.upsert_fact_card(
                    conn,
                    article_id=art["id"], language=art["language"],
                    summary=verdict.get("summary"),
                    key_claims=verdict.get("key_claims") or [],
                    history_check=verdict.get("history_check"),
                    web_evidence=web,
                    severity=sev,
                    visualization_spec=verdict.get("visualization") or {},
                    cost_usd=round(cost_main + cost_web, 5),
                    run_id=run_id,
                )
                cards += 1
                done += 1

                total_cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                              + web_searches * WEB_SEARCH_PRICE)
                if progress_cb:
                    progress_cb(done, len(todo), flagged, red_flagged, total_cost)
                # Budget guard
                if total_cost + today_spent >= daily_budget_usd:
                    logger.warning(f"budget hit (${total_cost + today_spent:.2f}); stopping")
                    break
    except KeyboardInterrupt:
        total_cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                      + web_searches * WEB_SEARCH_PRICE)
        claims_db.finish_inspection_run(
            conn, run_id, articles_processed=done, cards_generated=cards,
            flagged=flagged, red_flagged=red_flagged,
            tokens_in=tokens_in, tokens_out=tokens_out, web_searches=web_searches,
            cost_usd=total_cost, status="interrupted",
        )
        raise

    total_cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                  + web_searches * WEB_SEARCH_PRICE)
    claims_db.finish_inspection_run(
        conn, run_id, articles_processed=done, cards_generated=cards,
        flagged=flagged, red_flagged=red_flagged,
        tokens_in=tokens_in, tokens_out=tokens_out, web_searches=web_searches,
        cost_usd=total_cost, status="completed",
    )
    logger.info(f"inspection done: {cards} cards ({flagged} flag, {red_flagged} red_flag), "
                f"{web_searches} searches, cost=${total_cost:.2f}")
    return {
        "run_id": run_id,
        "cards_generated": cards,
        "flagged": flagged,
        "red_flagged": red_flagged,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "web_searches": web_searches,
        "cost_usd": total_cost,
    }
