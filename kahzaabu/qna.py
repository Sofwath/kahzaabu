# SPDX-License-Identifier: Apache-2.0
"""Natural-language Q&A over the kahzaabu DB.

Pipeline:
  1. LLM parses the user's question into structured filters (JSON).
  2. Code runs SQL against articles / claims / fact_checks / evidence.
  3. LLM formats the matched rows into a natural-language answer with citations.

Aliasing: 'kahzaabu', 'muizzu', 'the president', 'he' all refer to Muizzu —
told to the parser LLM via the system prompt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Optional

import anthropic

from . import pricing

logger = logging.getLogger("kahzaabu")

# Pricing + model IDs centralised in kahzaabu.pricing (single source).
PARSER_MODEL = pricing.MODELS["haiku"].id    # cheap, structured-output
ANSWER_MODEL = pricing.MODELS["sonnet"].id   # better synthesis
PRICE_HAIKU_IN  = pricing.MODELS["haiku"].in_per_m
PRICE_HAIKU_OUT = pricing.MODELS["haiku"].out_per_m
PRICE_SONNET_IN  = pricing.MODELS["sonnet"].in_per_m
PRICE_SONNET_OUT = pricing.MODELS["sonnet"].out_per_m

TODAY_ISO = date.today().isoformat()

# ---------- prompt: intent parser ----------

PARSER_SYSTEM = f"""You parse natural-language questions about the Maldives Presidency archive into structured filters.

Today is {TODAY_ISO}.

Subject context:
- "Kahzaabu" is a Dhivehi street nickname for President Mohamed Muizzu (in office since 2023-11-17).
- "Muizzu", "the president", "he", "him", and "kahzaabu" all refer to the same person — President Muizzu.
- Previous president: Solih (2018-11-17 to 2023-11-17). "Yameen" served 2013-2018.

Available data shape:
- articles (press releases, speeches): id, title, body_text, published_date, category
- claims (extracted from articles): type, subject, value, deadline, actor_credited, quote
- fact_checks: category, date, claim, what_actually_happened, evidence_quotes, topic
- fact_check_evidence (web search results per fact-check): url, title, snippet, relevance

Your job: produce a strict JSON object capturing what the user wants:

{{
  "intent": "activity" | "lies" | "promises" | "credit_claims" | "speeches" | "topic_search",
  "fact_check_categories": ["LIE","CONTRADICTION","MISLEADING","SHIFTING NUMBERS","CREDIT THEFT","BROKEN DEADLINE"],
  "claim_types": ["numeric_promise","deadline_promise","credit_claim","numeric_update","boast","comparison_to_predecessor","denial"],
  "date_from": "YYYY-MM-DD" or null,
  "date_to":   "YYYY-MM-DD" or null,
  "location_keywords": ["..."],   // e.g. ["Vaadhoo","Hulhumal"]; include common spelling variants
  "topic_keywords":    ["..."],   // e.g. ["housing","Sukuk","India"]; subject-matter words
  "limit": 20                      // max rows to consider; cap at 40
}}

Intent rules:
- "what is kahzaabu/muizzu up to" / "what did he do" / "what's happening" → "activity"
- "what lies" / "what did he lie about" / "false claims" / "broken promises" → "lies"
  - default fact_check_categories: ["LIE","CONTRADICTION","MISLEADING","SHIFTING NUMBERS","CREDIT THEFT"]
  - if user says "broken promises", include "BROKEN DEADLINE"
- "what did he promise" / "promises" / "pledged" → "promises" (claim_types include numeric_promise, deadline_promise)
- "what did he take credit for" → "credit_claims" (claim_types: ["credit_claim"])
- "what did he say in speeches" / "speeches" → "speeches"
- anything else → "topic_search" with topic_keywords filled

Time phrase rules (today = {TODAY_ISO}):
- "this week" → date_from = today-6, date_to = today
- "last week" → date_from = today-13, date_to = today-7
- "this month" → date_from = first-of-month, date_to = today
- "last month" → date_from = first-of-prev-month, date_to = last-of-prev-month
- "this year" → date_from = YYYY-01-01, date_to = today
- "since taking office" → date_from = 2023-11-17, date_to = today
- explicit dates ("in March 2024", "in 2025") → parsed accordingly
- if no time phrase: leave both null (means since 2023-11-17 by default)

Location handling:
- Maldivian islands/atolls/places: include common spelling variants. e.g.:
  - "Hulhumalé" → ["Hulhumal"] (matches both Hulhumalé and Hulhumale)
  - "Malé" → ["Mal"] (would over-match; prefer ["Malé","Male city","Greater Mal"])
  - Atoll names like "Laamu" → ["Laamu","L. ","L Atoll"]
- If no location mentioned, leave list empty.

Return ONLY the JSON object, no surrounding text."""


# ---------- prompt: answer formatter ----------

ANSWERER_SYSTEM = """You are a research assistant answering questions about the Maldives Presidency archive.

You will receive:
- The user's original question
- A list of matched records (articles, claims, fact-checks, or evidence) returned by an SQL query
- Today's date

Write a clear, well-structured answer to the user's question that:
- Cites article ids (e.g. [36643], [36527]) and dates inline
- Quotes verbatim where useful
- For fact-check items, includes the web-evidence URLs if present
- Is honest about gaps ("no items matched in this window")
- Uses bullet points or short paragraphs as appropriate
- Aliases: "kahzaabu" and "muizzu" both mean President Muizzu — use the user's preferred term in your reply

Be specific. Don't pad. If matches are few, say so."""


JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ---------- helpers ----------

def _client() -> anthropic.Anthropic:
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic()


def _parse_intent(client: anthropic.Anthropic, question: str) -> tuple[dict, dict]:
    """Returns (parsed_intent, usage_dict)."""
    r = client.messages.create(
        model=PARSER_MODEL, max_tokens=600, system=PARSER_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
    m = JSON_RE.search(text)
    if not m:
        return ({"intent": "activity", "_parse_error": True, "_raw": text[:300]},
                {"in": r.usage.input_tokens, "out": r.usage.output_tokens})
    try:
        return (json.loads(m.group(0)),
                {"in": r.usage.input_tokens, "out": r.usage.output_tokens})
    except json.JSONDecodeError as e:
        return ({"intent": "activity", "_parse_error": True, "_err": str(e)},
                {"in": r.usage.input_tokens, "out": r.usage.output_tokens})


def _location_clause(field: str, keywords: list[str]) -> tuple[str, list[str]]:
    if not keywords:
        return "1=1", []
    parts = []
    params = []
    for kw in keywords:
        parts.append(f"{field} LIKE ?")
        params.append(f"%{kw}%")
    return "(" + " OR ".join(parts) + ")", params


def _execute(conn: sqlite3.Connection, intent: dict, *, default_limit: int = 20) -> list[dict]:
    """Build & run SQL based on parsed intent. Return list of dicts."""
    conn.row_factory = sqlite3.Row
    intent_type = intent.get("intent", "activity")
    df = intent.get("date_from")
    dt = intent.get("date_to")
    if not df:
        df = "2023-11-17"
    if not dt:
        dt = TODAY_ISO
    loc_kw = intent.get("location_keywords") or []
    topic_kw = intent.get("topic_keywords") or []
    limit = min(int(intent.get("limit") or default_limit), 40)

    if intent_type == "activity" or intent_type == "speeches":
        cats = ("press_release", "speech", "vp_speech")
        if intent_type == "speeches":
            cats = ("speech", "vp_speech")
        sql = f"""SELECT id, title, published_date, category,
                   SUBSTR(body_text, 1, 350) AS snippet
                  FROM articles
                  WHERE language='EN' AND body_text IS NOT NULL AND body_text != ''
                    AND category IN ({','.join('?' * len(cats))})
                    AND published_date BETWEEN ? AND ?"""
        params: list[Any] = list(cats) + [df, dt]
        # location filter on title OR body
        if loc_kw:
            loc_title, p1 = _location_clause("title", loc_kw)
            loc_body, p2 = _location_clause("body_text", loc_kw)
            sql += f" AND ({loc_title} OR {loc_body})"
            params += p1 + p2
        # topic filter
        if topic_kw:
            tparts = []
            for kw in topic_kw:
                tparts.append("(title LIKE ? OR body_text LIKE ?)")
                params.extend([f"%{kw}%", f"%{kw}%"])
            sql += " AND (" + " OR ".join(tparts) + ")"
        sql += " ORDER BY published_date DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    if intent_type == "lies":
        cats = intent.get("fact_check_categories") or [
            "LIE", "CONTRADICTION", "MISLEADING", "SHIFTING NUMBERS", "CREDIT THEFT",
        ]
        sql = f"""SELECT fc.id, fc.category, fc.claim_date, fc.claim,
                         fc.what_actually_happened, fc.type, fc.topic,
                         fc.source_article_ids, fc.evidence_quotes,
                         fc.source as origin
                  FROM fact_checks fc
                  WHERE fc.category IN ({','.join('?' * len(cats))})
                    AND fc.claim_date BETWEEN ? AND ?"""
        params = list(cats) + [df, dt]
        if loc_kw:
            loc_filters = []
            for kw in loc_kw:
                loc_filters.append("(fc.claim LIKE ? OR fc.what_actually_happened LIKE ? OR fc.evidence_quotes LIKE ?)")
                params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
            sql += " AND (" + " OR ".join(loc_filters) + ")"
        if topic_kw:
            tparts = []
            for kw in topic_kw:
                tparts.append("(fc.claim LIKE ? OR fc.what_actually_happened LIKE ? OR fc.topic LIKE ?)")
                params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
            sql += " AND (" + " OR ".join(tparts) + ")"
        sql += " ORDER BY fc.claim_date DESC, fc.id DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        # Attach web evidence
        for r in rows:
            ev = conn.execute(
                """SELECT url, title, snippet, relevance, summary
                   FROM fact_check_evidence WHERE fact_check_id = ?""",
                (r["id"],),
            ).fetchall()
            r["web_evidence"] = [dict(e) for e in ev]
            try:
                r["source_article_ids"] = json.loads(r.get("source_article_ids") or "[]")
            except Exception:
                pass
            try:
                r["evidence_quotes"] = json.loads(r.get("evidence_quotes") or "[]")
            except Exception:
                pass
        return rows

    if intent_type in ("promises", "credit_claims", "topic_search"):
        types = intent.get("claim_types") or []
        if intent_type == "promises" and not types:
            types = ["numeric_promise", "deadline_promise"]
        if intent_type == "credit_claims" and not types:
            types = ["credit_claim"]
        sql = """SELECT c.article_id, c.type, c.subject, c.value, c.deadline,
                        c.actor_credited, c.quote, a.title, a.published_date, a.category
                 FROM claims c
                 JOIN articles a ON c.article_id = a.id AND c.language = a.language
                 WHERE c.type != 'no_specific_claims'
                   AND a.language = 'EN'
                   AND a.published_date BETWEEN ? AND ?"""
        params = [df, dt]
        if types:
            sql += f" AND c.type IN ({','.join('?' * len(types))})"
            params += types
        if loc_kw:
            lps = []
            for kw in loc_kw:
                lps.append("(c.subject LIKE ? OR c.quote LIKE ? OR a.title LIKE ?)")
                params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
            sql += " AND (" + " OR ".join(lps) + ")"
        if topic_kw:
            tps = []
            for kw in topic_kw:
                tps.append("(c.subject LIKE ? OR c.quote LIKE ? OR a.title LIKE ?)")
                params += [f"%{kw}%", f"%{kw}%", f"%{kw}%"]
            sql += " AND (" + " OR ".join(tps) + ")"
        sql += " ORDER BY a.published_date DESC, c.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    return []


def _format_answer(client: anthropic.Anthropic, question: str, intent: dict,
                   rows: list[dict]) -> tuple[str, dict]:
    if not rows:
        return (
            "I didn't find any records matching that query.\n\n"
            f"Filters applied: intent={intent.get('intent')!r}, "
            f"dates {intent.get('date_from') or '(open)'}..{intent.get('date_to') or '(open)'}, "
            f"locations={intent.get('location_keywords') or []}, "
            f"topics={intent.get('topic_keywords') or []}",
            {"in": 0, "out": 0},
        )

    user = (
        f"User asked: {question!r}\n\n"
        f"Parsed intent (for your awareness): {json.dumps(intent, ensure_ascii=False)[:1000]}\n"
        f"Today's date: {TODAY_ISO}\n\n"
        f"Matched rows ({len(rows)}):\n"
        f"{json.dumps(rows, ensure_ascii=False)[:25000]}\n\n"
        "Write a clear answer with citations to article IDs and dates."
    )
    r = client.messages.create(
        model=ANSWER_MODEL, max_tokens=2000, system=ANSWERER_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
    return text, {"in": r.usage.input_tokens, "out": r.usage.output_tokens}


# ---------- public API ----------

def ask(conn: sqlite3.Connection, question: str, *, default_limit: int = 20,
        format_with_llm: bool = True) -> dict:
    """Answer a natural-language question. Returns a dict with answer + diagnostics."""
    client = _client()
    intent, parser_usage = _parse_intent(client, question)
    rows = _execute(conn, intent, default_limit=default_limit)
    if format_with_llm:
        answer, ans_usage = _format_answer(client, question, intent, rows)
    else:
        answer = ""
        ans_usage = {"in": 0, "out": 0}

    parser_cost = (parser_usage["in"] / 1e6 * PRICE_HAIKU_IN
                   + parser_usage["out"] / 1e6 * PRICE_HAIKU_OUT)
    answer_cost = (ans_usage["in"] / 1e6 * PRICE_SONNET_IN
                   + ans_usage["out"] / 1e6 * PRICE_SONNET_OUT)
    return {
        "question": question,
        "intent": intent,
        "n_matches": len(rows),
        "rows": rows,
        "answer": answer,
        "cost_usd": parser_cost + answer_cost,
        "tokens": {
            "parser_in": parser_usage["in"], "parser_out": parser_usage["out"],
            "answer_in": ans_usage["in"], "answer_out": ans_usage["out"],
        },
    }
