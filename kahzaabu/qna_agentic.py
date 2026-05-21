"""Agentic Q&A — Claude with tool-use loop over the kahzaabu archive.

Differences from qna.ask():
- Claude calls DB tools iteratively until confident
- Web search (Anthropic server tool) for external corroboration
- Session memory via session_id (conversation continuity)
- 5-iteration cap, budget gate per call

Public API:
    ask_agentic(conn, question, session_id=None, max_iterations=5,
                enable_web=True, daily_budget_usd=2.0) -> dict
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import date
from typing import Any, Optional

import anthropic

from . import claims_db

logger = logging.getLogger("kahzaabu")

MODEL = "claude-sonnet-4-6"
PRICE_IN_PER_M = 3.0
PRICE_OUT_PER_M = 15.0
WEB_SEARCH_PRICE_PER_SEARCH = 0.01

TODAY = date.today().isoformat()
MAX_SESSION_BYTES = 80_000        # ~20K tokens of history before we compress
MAX_TOOL_RESULT_BYTES = 8_000     # cap per tool result so context doesn't explode

ALIAS_NOTE = (
    "Subject context: 'Kahzaabu', 'Muizzu', 'the president', 'he', and 'him' all "
    "refer to President Mohamed Muizzu (in office since 2023-11-17). 'Kahzaabu' "
    "is a Dhivehi street nickname for him. Previous president: Solih (2018-2023). "
    "Yameen served 2013-2018."
)

SYSTEM_PROMPT = f"""You are a research assistant with access to the Kahzaabu archive — a structured corpus of Maldives Presidency press releases, extracted claims, curated fact-checks, web-evidence rows, manifesto promises, and EN/DV translation diffs. Today is {TODAY}.

{ALIAS_NOTE}

You have direct tools to query the archive AND a web_search tool for external corroboration.

Your job: answer the user's question accurately, drawing on the archive first and the web only where needed. ALWAYS cite specific article_ids (in `[NNNNN]` form), fact_check_ids, or manifesto promise_ids. Be honest about gaps.

Tool-use strategy:
- For "what's he up to" questions → search_articles or list_recent
- For "what lies/contradictions" → search_factchecks (filter by category if relevant)
- For "what did he promise" → search_manifesto
- For specific subjects → search across all three, then fetch details with get_article / get_factcheck / get_promise
- For independent verification of a claim → web_search (max 2-3 searches per turn)

Don't just dump tool results. Synthesize. If you find a conflict between manifesto promise and a fact-check, surface it. If the archive is silent on something, say so before using web_search.

DATA FRESHNESS: When the question is about "recent" or "this week" or "what's happening now", call `archive_stats` first and check the `freshness` field. If `is_stale` is true (>24h since last scrape), warn the user at the end of your answer that the data may be missing very recent items, and suggest they run the pipeline to refresh. Do NOT trigger the pipeline yourself — only report.

Output: well-structured Markdown. Use headings, bullet points, and inline article-id citations. End with a one-line confidence/gap note when appropriate."""


# ---------- internal tools (DB-backed, cheap) ----------

def _truncate(s: str, n: int = 200) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + "…"


def _tool_search_articles(conn, q: str = "", date_from: Optional[str] = None,
                          date_to: Optional[str] = None,
                          category: Optional[str] = None, limit: int = 10) -> dict:
    sql = """SELECT id, category, title, published_date, SUBSTR(body_text, 1, 240) AS snippet
             FROM articles WHERE language='EN' AND body_text IS NOT NULL AND body_text != ''
               AND published_date >= '2023-11-17'"""
    params: list = []
    if category:
        sql += " AND category = ?"; params.append(category)
    if date_from:
        sql += " AND published_date >= ?"; params.append(date_from)
    if date_to:
        sql += " AND published_date <= ?"; params.append(date_to)
    if q:
        sql += " AND (title LIKE ? OR body_text LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY published_date DESC, id DESC LIMIT ?"
    params.append(min(int(limit), 30))
    rows = conn.execute(sql, params).fetchall()
    return {"count": len(rows), "items": [
        {"id": r["id"], "category": r["category"], "title": r["title"],
         "date": r["published_date"][:10],
         "snippet": _truncate(r["snippet"] or "", 220)}
        for r in rows
    ]}


def _tool_search_factchecks(conn, q: str = "", category: Optional[str] = None,
                            topic: Optional[str] = None,
                            date_from: Optional[str] = None,
                            date_to: Optional[str] = None, limit: int = 10) -> dict:
    sql = """SELECT id, category, claim_date, claim, what_actually_happened, topic
             FROM fact_checks WHERE published=1"""
    params: list = []
    if category:
        sql += " AND category = ?"; params.append(category)
    if topic:
        sql += " AND topic = ?"; params.append(topic)
    if date_from:
        sql += " AND claim_date >= ?"; params.append(date_from)
    if date_to:
        sql += " AND claim_date <= ?"; params.append(date_to)
    if q:
        sql += " AND (claim LIKE ? OR what_actually_happened LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY claim_date DESC LIMIT ?"
    params.append(min(int(limit), 30))
    rows = conn.execute(sql, params).fetchall()
    return {"count": len(rows), "items": [
        {"id": r["id"], "category": r["category"], "date": r["claim_date"][:10],
         "topic": r["topic"], "claim": _truncate(r["claim"], 280),
         "evidence_summary": _truncate(r["what_actually_happened"], 350)}
        for r in rows
    ]}


def _tool_search_manifesto(conn, q: str = "", status: Optional[str] = None,
                           category: Optional[str] = None, limit: int = 15) -> dict:
    sql = """SELECT id, category, subject, target_value, deadline_stated,
                    delivery_status, promise_text_en
             FROM manifesto_promises WHERE published=1"""
    params: list = []
    if status:
        sql += " AND delivery_status = ?"; params.append(status)
    if category:
        sql += " AND category = ?"; params.append(category)
    if q:
        sql += " AND (subject LIKE ? OR promise_text_en LIKE ? OR promise_text_dv LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY id LIMIT ?"
    params.append(min(int(limit), 40))
    rows = conn.execute(sql, params).fetchall()
    return {"count": len(rows), "items": [
        {"id": r["id"], "status": r["delivery_status"], "category": r["category"],
         "subject": r["subject"], "target": r["target_value"],
         "deadline": r["deadline_stated"],
         "promise": _truncate(r["promise_text_en"], 220)}
        for r in rows
    ]}


def _tool_get_article(conn, article_id: int) -> dict:
    r = conn.execute(
        """SELECT id, title, body_text, published_date, category, paired_id
           FROM articles WHERE id = ? AND language='EN'""",
        (int(article_id),),
    ).fetchone()
    if not r:
        return {"error": f"article {article_id} not found"}
    out = {
        "id": r["id"], "title": r["title"], "date": r["published_date"][:10],
        "category": r["category"], "body": _truncate(r["body_text"], 3500),
    }
    claims = conn.execute(
        """SELECT type, subject, value, deadline, quote
           FROM claims WHERE article_id=? AND language='EN'
             AND type != 'no_specific_claims' LIMIT 12""",
        (int(article_id),),
    ).fetchall()
    out["claims"] = [
        {"type": c["type"], "subject": c["subject"], "value": c["value"],
         "deadline": c["deadline"], "quote": _truncate(c["quote"], 200)}
        for c in claims
    ]
    return out


def _tool_get_factcheck(conn, factcheck_id: int) -> dict:
    r = conn.execute(
        """SELECT id, category, claim_date, claim, what_actually_happened, type,
                  topic, source_article_ids, evidence_quotes
           FROM fact_checks WHERE id=? AND published=1""",
        (int(factcheck_id),),
    ).fetchone()
    if not r:
        return {"error": f"fact_check {factcheck_id} not found"}
    out = dict(r)
    try: out["source_article_ids"] = json.loads(out.pop("source_article_ids") or "[]")
    except Exception: out["source_article_ids"] = []
    try: out["evidence_quotes"] = json.loads(out.pop("evidence_quotes") or "[]")
    except Exception: out["evidence_quotes"] = []
    ev = conn.execute(
        """SELECT url, title, snippet, relevance, summary
           FROM fact_check_evidence WHERE fact_check_id=? LIMIT 8""",
        (int(factcheck_id),),
    ).fetchall()
    out["web_evidence"] = [
        {"url": e["url"], "title": e["title"], "relevance": e["relevance"],
         "summary": _truncate(e["summary"], 200)}
        for e in ev
    ]
    return out


def _tool_get_promise(conn, promise_id: int) -> dict:
    r = conn.execute(
        "SELECT * FROM manifesto_promises WHERE id=? AND published=1",
        (int(promise_id),),
    ).fetchone()
    if not r:
        return {"error": f"promise {promise_id} not found"}
    out = dict(r)
    try:
        ev = json.loads(out.pop("delivery_evidence_json") or "{}")
    except Exception:
        ev = {}
    out["delivery_rationale"] = _truncate(ev.get("rationale"), 350)
    out["linked_article_ids"] = ev.get("linked_article_ids", [])
    out["linked_fact_check_ids"] = ev.get("linked_fact_check_ids", [])
    out["promise_text_en"] = _truncate(out.get("promise_text_en"), 300)
    # Keep Dhivehi (caller may need verbatim); truncate to keep tokens sane
    if out.get("promise_text_dv"):
        out["promise_text_dv"] = _truncate(out["promise_text_dv"], 300)
    out.pop("messages_json", None)
    return out


def _tool_list_recent(conn, days: int = 7, limit: int = 15) -> dict:
    from datetime import timedelta
    since = (date.today() - timedelta(days=int(days))).isoformat()
    rows = conn.execute(
        """SELECT id, title, published_date, category
           FROM articles WHERE language='EN' AND body_text IS NOT NULL AND body_text != ''
             AND category IN ('press_release','speech','vp_speech')
             AND published_date >= ?
           ORDER BY published_date DESC, id DESC LIMIT ?""",
        (since, min(int(limit), 40)),
    ).fetchall()
    return {"since": since, "count": len(rows), "items": [
        {"id": r["id"], "title": r["title"], "date": r["published_date"][:10],
         "category": r["category"]} for r in rows
    ]}


def _tool_archive_stats(conn) -> dict:
    n_a = conn.execute(
        """SELECT COUNT(*) FROM articles WHERE language='EN' AND body_text IS NOT NULL
           AND body_text != '' AND category IN ('press_release','speech','vp_speech')
           AND published_date >= '2023-11-17'"""
    ).fetchone()[0]
    n_fc = conn.execute("SELECT COUNT(*) FROM fact_checks WHERE published=1").fetchone()[0]
    n_m = conn.execute("SELECT COUNT(*) FROM manifesto_promises WHERE published=1").fetchone()[0]
    by_status = {r[0]: r[1] for r in conn.execute(
        "SELECT delivery_status, COUNT(*) FROM manifesto_promises WHERE published=1 "
        "GROUP BY delivery_status").fetchall()}
    by_cat = {r[0]: r[1] for r in conn.execute(
        "SELECT category, COUNT(*) FROM fact_checks WHERE published=1 GROUP BY category"
    ).fetchall()}
    return {"articles": n_a, "fact_checks": n_fc,
            "fact_checks_by_category": by_cat,
            "manifesto_promises": n_m,
            "manifesto_by_status": by_status,
            "freshness": claims_db.freshness(conn)}


TOOL_HANDLERS = {
    "search_articles":    _tool_search_articles,
    "search_factchecks":  _tool_search_factchecks,
    "search_manifesto":   _tool_search_manifesto,
    "get_article":        _tool_get_article,
    "get_factcheck":      _tool_get_factcheck,
    "get_promise":        _tool_get_promise,
    "list_recent":        _tool_list_recent,
    "archive_stats":      _tool_archive_stats,
}


def _tool_specs(include_web: bool = True) -> list[dict]:
    tools = [
        {"name": "archive_stats", "description": "Get the current size of the kahzaabu archive (articles, fact-checks, manifesto promises by status).",
         "input_schema": {"type": "object", "properties": {}, "required": []}},
        {"name": "search_articles",
         "description": "Find press releases by keyword. Returns id, title, date, snippet.",
         "input_schema": {"type": "object", "properties": {
             "q": {"type": "string", "description": "search text (matches title + body)"},
             "date_from": {"type": "string", "description": "YYYY-MM-DD"},
             "date_to":   {"type": "string", "description": "YYYY-MM-DD"},
             "category":  {"type": "string", "enum": ["press_release", "speech", "vp_speech"]},
             "limit":     {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}}}},
        {"name": "search_factchecks",
         "description": "Search curated fact-checks. Categories: LIE, CONTRADICTION, MISLEADING, SHIFTING NUMBERS, CREDIT THEFT, BROKEN DEADLINE.",
         "input_schema": {"type": "object", "properties": {
             "q":        {"type": "string"},
             "category": {"type": "string", "enum": ["LIE","CONTRADICTION","MISLEADING","SHIFTING NUMBERS","CREDIT THEFT","BROKEN DEADLINE"]},
             "topic":    {"type": "string", "description": "housing | fiscal_debt | infrastructure | tourism | energy | diplomatic_india_china | social_education | sports_youth | governance_legal | fisheries | spokesperson_brief"},
             "date_from":{"type": "string"},
             "date_to":  {"type": "string"},
             "limit":    {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}}}},
        {"name": "search_manifesto",
         "description": "Search 2023 manifesto promises by keyword + delivery status + category.",
         "input_schema": {"type": "object", "properties": {
             "q":        {"type": "string"},
             "status":   {"type": "string", "enum": ["delivered","in_progress","modified","broken","abandoned","unmentioned"]},
             "category": {"type": "string"},
             "limit":    {"type": "integer", "minimum": 1, "maximum": 40, "default": 15}}}},
        {"name": "get_article",
         "description": "Fetch full article body + extracted claims for one article_id.",
         "input_schema": {"type": "object", "properties": {
             "article_id": {"type": "integer"}}, "required": ["article_id"]}},
        {"name": "get_factcheck",
         "description": "Fetch full fact-check + web evidence for one factcheck_id.",
         "input_schema": {"type": "object", "properties": {
             "factcheck_id": {"type": "integer"}}, "required": ["factcheck_id"]}},
        {"name": "get_promise",
         "description": "Fetch full manifesto promise + delivery rationale + linked article/factcheck ids.",
         "input_schema": {"type": "object", "properties": {
             "promise_id": {"type": "integer"}}, "required": ["promise_id"]}},
        {"name": "list_recent",
         "description": "List articles published in the last N days.",
         "input_schema": {"type": "object", "properties": {
             "days":  {"type": "integer", "default": 7},
             "limit": {"type": "integer", "default": 15}}}},
    ]
    if include_web:
        tools.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 3,
        })
    return tools


def _run_tool(name: str, args: dict, conn) -> Any:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"unknown tool: {name}"}
    try:
        return handler(conn, **args)
    except TypeError as e:
        return {"error": f"bad args for {name}: {e}"}
    except Exception as e:
        return {"error": f"tool {name} failed: {e}"}


def _trim_messages(messages: list, max_bytes: int = MAX_SESSION_BYTES) -> list:
    """Drop oldest user/assistant turns when conversation gets too long."""
    while True:
        size = len(json.dumps(messages, ensure_ascii=False))
        if size <= max_bytes:
            return messages
        if len(messages) <= 4:
            return messages  # don't trim below 2 user/assistant pairs
        # Drop the oldest non-system turn(s)
        del messages[0]


def ask_agentic(conn: sqlite3.Connection, question: str, *,
                session_id: Optional[str] = None,
                max_iterations: int = 7,
                enable_web: bool = True,
                daily_budget_usd: float = 5.0) -> dict:
    """Agentic Q&A. Returns {answer, session_id, intent, n_iterations, cost_usd, tool_trace}."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    claims_db.init_claims_schema(conn)
    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        raise RuntimeError(
            f"daily budget ${daily_budget_usd:.2f} exhausted ({today_spent:.2f}); "
            "set daily_budget_usd higher or wait for UTC reset"
        )

    # Load session history if continuing
    messages: list[dict] = []
    if session_id:
        sess = claims_db.get_qna_session(conn, session_id)
        if sess:
            messages = list(sess.get("messages", []))
    else:
        session_id = uuid.uuid4().hex

    messages.append({"role": "user", "content": question})
    messages = _trim_messages(messages)

    client = anthropic.Anthropic()
    tools = _tool_specs(include_web=enable_web)

    tokens_in = tokens_out = web_searches = 0
    tool_trace: list[dict] = []
    iterations = 0
    final_text = ""

    while iterations < max_iterations:
        iterations += 1
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT,
                tools=tools, messages=messages,
            )
        except anthropic.APIError as e:
            return {"answer": f"API error: {e}", "session_id": session_id,
                    "cost_usd": 0.0, "tool_trace": tool_trace, "error": str(e)}

        tokens_in += r.usage.input_tokens
        tokens_out += r.usage.output_tokens
        # Append assistant response to messages
        messages.append({"role": "assistant", "content": [
            block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else block.__dict__
            for block in r.content
        ]})

        # Collect tool_use blocks
        tool_uses = [b for b in r.content if getattr(b, "type", None) == "tool_use"]
        # Server-tool web_search counter
        for b in r.content:
            if getattr(b, "type", None) == "server_tool_use" and getattr(b, "name", "") == "web_search":
                web_searches += 1

        if r.stop_reason != "tool_use" or not tool_uses:
            # Final text
            for block in r.content:
                if getattr(block, "type", None) == "text":
                    final_text += getattr(block, "text", "")
            break

        # Execute tool calls
        tool_results = []
        for tu in tool_uses:
            name = tu.name
            args = tu.input or {}
            result = _run_tool(name, args, conn)
            # Cap result size
            result_str = json.dumps(result, ensure_ascii=False)
            if len(result_str) > MAX_TOOL_RESULT_BYTES:
                result_str = result_str[:MAX_TOOL_RESULT_BYTES] + '..."}'
            tool_trace.append({"iteration": iterations, "tool": name, "args": args,
                                "result_preview": result_str[:300]})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_str,
            })
        messages.append({"role": "user", "content": tool_results})

        # Budget check mid-loop
        cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                + web_searches * WEB_SEARCH_PRICE_PER_SEARCH)
        if today_spent + cost >= daily_budget_usd:
            logger.warning(f"agentic ask budget hit (${today_spent + cost:.2f}); stopping")
            final_text = "(daily budget exhausted before answer was complete)"
            break

    # If we ran out of iterations while still in tool-use mode, force a final
    # synthesis. The last message in `messages` is a `[tool_result, ...]` user
    # message, which makes Claude think the prior tool-use turn is fully
    # resolved and the next move is whatever the user wants. Without a NEW
    # user prompt and without tools, Claude responds end_turn with empty
    # content. So we explicitly append a user instruction asking for the
    # summary, with no tools available.
    if not final_text:
        try:
            messages.append({
                "role": "user",
                "content": (
                    "You've used all the tool calls available for this question. "
                    "Now write your best answer based on what you've already "
                    "gathered above. Use Markdown, cite article ids inline as "
                    "[NNNNN] / fact_check ids / promise ids. If the evidence is "
                    "incomplete, say so explicitly rather than asking for more "
                    "tool calls."
                ),
            })
            r = client.messages.create(
                model=MODEL, max_tokens=4000, system=SYSTEM_PROMPT,
                messages=messages,
            )
            tokens_in += r.usage.input_tokens
            tokens_out += r.usage.output_tokens
            messages.append({"role": "assistant", "content": [
                block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else block.__dict__
                for block in r.content
            ]})
            for block in r.content:
                if getattr(block, "type", None) == "text":
                    final_text += getattr(block, "text", "")
            tool_trace.append({"iteration": iterations + 1, "tool": "(forced-synthesis)",
                                "args": {}, "result_preview": f"no-tools wrap-up, "
                                f"{len(final_text)} chars"})
        except Exception as e:
            final_text = (f"(no answer — agent hit iteration cap and forced-synthesis "
                          f"call failed: {e})")

    # If even after the forced synthesis we have nothing, surface the failure
    # honestly rather than silently returning "".
    if not final_text:
        final_text = (
            "(I hit the iteration cap on this question before producing an answer. "
            "Try rephrasing more narrowly, or run again with a higher `max_iterations` "
            "if your client supports it.)"
        )

    cost = (tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
            + web_searches * WEB_SEARCH_PRICE_PER_SEARCH)

    # Persist the session (post-trim again to keep stored size sane)
    messages = _trim_messages(messages)
    try:
        claims_db.save_qna_session(conn, session_id, messages, cost_usd=cost)
    except Exception as e:
        logger.warning(f"session save failed: {e}")

    return {
        "answer": final_text,
        "session_id": session_id,
        "n_iterations": iterations,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "web_searches": web_searches,
        "cost_usd": round(cost, 4),
        "tool_trace": tool_trace,
    }
