"""Kahzaabu MCP server — exposes the corpus as tools for any MCP-compatible agent.

Run standalone:
    .venv-mcp/bin/python -m kahzaabu.mcp_server                # stdio
    .venv-mcp/bin/python -m kahzaabu.mcp_server --http 8770    # HTTP/SSE

Hermes wires it as a stdio MCP server. See scripts/hermes-mcp-config.json.

Read-only by default. Pipeline trigger requires admin role (TODO: pass-through via
context); for now `pipeline_run` is gated by KAHZAABU_MCP_ALLOW_PIPELINE=1.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Make the kahzaabu package importable when run as `python -m kahzaabu.mcp_server`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Quiet anthropic / httpx INFO noise that would interleave with stdio MCP traffic
for name in ("httpx", "httpcore", "anthropic", "kahzaabu"):
    logging.getLogger(name).setLevel(logging.WARNING)

DB_PATH = ROOT / "data" / "kahzaabu.db"

mcp = FastMCP(
    "kahzaabu",
    instructions=(
        "Kahzaabu is an archive of Maldives Presidency press releases with extracted "
        "claims, curated fact-checks, web-evidence, manifesto promises and EN/DV diffs. "
        "Use ask() for natural-language Q&A. Use list_lies/get_factcheck for fact-check "
        "details. Use manifesto/* for 2023 campaign promises vs delivery. Use "
        "get_article for a single press-release with claims + linked fact-checks. "
        "The subject is President Mohamed Muizzu (street nickname: 'kahzaabu')."
    ),
)


# ---------- helpers ----------

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------- tools ----------

@mcp.tool()
def stats() -> dict:
    """Return current archive state: article counts, claim counts, fact-check counts,
    manifesto-promise breakdown by delivery status."""
    conn = _conn()
    try:
        n_articles = conn.execute(
            """SELECT COUNT(*) FROM articles WHERE language='EN'
               AND body_text IS NOT NULL AND body_text != ''
               AND category IN ('press_release','speech','vp_speech')
               AND published_date >= '2023-11-17'"""
        ).fetchone()[0]
        n_claims = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE type != 'no_specific_claims'"
        ).fetchone()[0]
        n_fc = conn.execute("SELECT COUNT(*) FROM fact_checks WHERE published=1").fetchone()[0]
        n_ev = conn.execute(
            """SELECT COUNT(*) FROM fact_check_evidence e
               JOIN fact_checks f ON f.id = e.fact_check_id WHERE f.published=1"""
        ).fetchone()[0]
        n_mfs = conn.execute(
            "SELECT COUNT(*) FROM manifesto_promises WHERE published=1"
        ).fetchone()[0]
        by_status = {r[0]: r[1] for r in conn.execute(
            "SELECT delivery_status, COUNT(*) FROM manifesto_promises WHERE published=1 "
            "GROUP BY delivery_status"
        ).fetchall()}
        return {
            "articles_muizzu_era": n_articles,
            "claims_extracted": n_claims,
            "fact_checks": n_fc,
            "web_evidence_rows": n_ev,
            "manifesto_promises": n_mfs,
            "manifesto_by_delivery_status": by_status,
            "db_path": str(DB_PATH),
        }
    finally:
        conn.close()


@mcp.tool()
def ask(
    question: str,
    session_id: Optional[str] = None,
    enable_web: bool = True,
    max_iterations: int = 5,
) -> dict:
    """Ask a natural-language question over the kahzaabu archive.

    The agent has 8 internal tools (search_articles, search_factchecks,
    search_manifesto, get_article, get_factcheck, get_promise, list_recent,
    archive_stats) and Anthropic's web_search server tool. It iterates up
    to max_iterations times to find a confident answer.

    'Kahzaabu' and 'Muizzu' refer to the same person.

    Pass `session_id` (from a previous response) to continue a conversation —
    the agent retains prior turns and tool results. Omit it on the first call;
    one will be returned.

    Set enable_web=False to disable Anthropic's web_search (cheaper, archive-only).
    Returns: {answer, session_id, n_iterations, cost_usd, tool_trace, tokens_in, tokens_out, web_searches}.
    Requires ANTHROPIC_API_KEY in the environment.
    """
    if not _has_anthropic_key():
        return {"error": "ANTHROPIC_API_KEY not set; kahzaabu_ask requires it"}
    from kahzaabu.qna_agentic import ask_agentic
    conn = _conn()
    try:
        res = ask_agentic(
            conn, question,
            session_id=session_id,
            max_iterations=max(1, min(int(max_iterations), 8)),
            enable_web=enable_web,
            daily_budget_usd=5.0,
        )
        return {
            "answer": res["answer"],
            "session_id": res["session_id"],
            "n_iterations": res["n_iterations"],
            "cost_usd": res["cost_usd"],
            "web_searches": res.get("web_searches", 0),
            "tool_trace": res.get("tool_trace", []),
        }
    finally:
        conn.close()


@mcp.tool()
def list_lies(
    category: Optional[str] = None,
    topic: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """List fact-checks. category ∈ {LIE, CONTRADICTION, MISLEADING, SHIFTING NUMBERS,
    CREDIT THEFT, BROKEN DEADLINE}. date_from/date_to in YYYY-MM-DD. q does substring
    match on claim text."""
    sql = """SELECT id, category, claim_date, claim, what_actually_happened, topic,
                    source_article_ids, evidence_quotes
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
    params.append(min(limit, 100))
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try: d["source_article_ids"] = json.loads(d["source_article_ids"] or "[]")
            except Exception: d["source_article_ids"] = []
            try: d["evidence_quotes"] = json.loads(d["evidence_quotes"] or "[]")
            except Exception: d["evidence_quotes"] = []
            d["n_web_evidence"] = conn.execute(
                "SELECT COUNT(*) FROM fact_check_evidence WHERE fact_check_id=?",
                (d["id"],),
            ).fetchone()[0]
            items.append(d)
        return {"count": len(items), "items": items}
    finally:
        conn.close()


@mcp.tool()
def get_factcheck(id: int) -> dict:
    """Get full detail for a fact-check: claim, evidence, web sources, linked articles."""
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT * FROM fact_checks WHERE id=? AND published=1", (id,)
        ).fetchone()
        if not r:
            return {"error": f"fact_check {id} not found or not published"}
        d = dict(r)
        try: d["source_article_ids"] = json.loads(d["source_article_ids"] or "[]")
        except Exception: d["source_article_ids"] = []
        try: d["evidence_quotes"] = json.loads(d["evidence_quotes"] or "[]")
        except Exception: d["evidence_quotes"] = []
        ev = conn.execute(
            """SELECT url, title, snippet, relevance, summary, retrieved_at
               FROM fact_check_evidence WHERE fact_check_id=? ORDER BY id""",
            (id,),
        ).fetchall()
        d["web_evidence"] = [dict(e) for e in ev]
        if d["source_article_ids"]:
            ph = ",".join("?" * len(d["source_article_ids"]))
            arts = conn.execute(
                f"SELECT id, title, published_date FROM articles "
                f"WHERE id IN ({ph}) AND language='EN'",
                d["source_article_ids"],
            ).fetchall()
            d["source_articles"] = [dict(a) for a in arts]
        return d
    finally:
        conn.close()


@mcp.tool()
def manifesto(
    status: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 30,
) -> dict:
    """Query Muizzu 2023 manifesto promises.

    status ∈ {delivered, in_progress, broken, modified, abandoned, unmentioned}.
    category ∈ {housing, infrastructure, economy, governance, health, education,
    tourism, fisheries, religion, foreign_policy, youth, sports, other}.
    q does substring match across DV/EN/subject.
    """
    sql = """SELECT id, section, promise_text_dv, promise_text_en, category, subject,
                    target_value, deadline_stated, delivery_status,
                    delivery_evidence_json
             FROM manifesto_promises WHERE published=1"""
    params: list = []
    if status:
        sql += " AND delivery_status=?"; params.append(status)
    if category:
        sql += " AND category=?"; params.append(category)
    if q:
        sql += " AND (promise_text_en LIKE ? OR subject LIKE ? OR promise_text_dv LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY id LIMIT ?"
    params.append(min(limit, 100))
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            try: d["delivery_evidence"] = json.loads(d.pop("delivery_evidence_json") or "{}")
            except Exception: d["delivery_evidence"] = {}
            items.append(d)
        by_status = {r[0]: r[1] for r in conn.execute(
            "SELECT delivery_status, COUNT(*) FROM manifesto_promises WHERE published=1 "
            "GROUP BY delivery_status"
        ).fetchall()}
        return {"count": len(items), "items": items, "totals_by_status": by_status}
    finally:
        conn.close()


@mcp.tool()
def get_article(article_id: int, include_factcards: bool = True) -> dict:
    """Get a single press release: body, claims, fact-checks referencing it,
    optional per-article fact card."""
    conn = _conn()
    try:
        r = conn.execute(
            """SELECT id, language, paired_id, category, title, body_text,
                      published_date, reference
               FROM articles WHERE id=? AND language='EN'""",
            (article_id,),
        ).fetchone()
        if not r:
            return {"error": f"article {article_id} not found"}
        out = dict(r)
        claims = conn.execute(
            """SELECT type, subject, value, deadline, actor_credited, quote
               FROM claims WHERE article_id=? AND language='EN'
                 AND type != 'no_specific_claims'""",
            (article_id,),
        ).fetchall()
        out["claims"] = [dict(c) for c in claims]
        # fact_checks that cite this article (parse JSON properly to avoid substring matches)
        fcs = conn.execute(
            """SELECT id, category, claim_date, claim, what_actually_happened,
                      source_article_ids
               FROM fact_checks WHERE published=1 AND source_article_ids LIKE ?""",
            (f"%{article_id}%",),
        ).fetchall()
        out["fact_checks"] = []
        for fc in fcs:
            try:
                ids = json.loads(fc["source_article_ids"] or "[]")
            except Exception:
                ids = []
            if article_id in ids:
                d = dict(fc)
                d["source_article_ids"] = ids
                out["fact_checks"].append(d)
        if include_factcards:
            card = conn.execute(
                "SELECT summary, severity, key_claims_json, history_check, web_evidence_json "
                "FROM article_fact_cards WHERE article_id=? AND language='EN' AND published=1",
                (article_id,),
            ).fetchone()
            if card:
                d = dict(card)
                try: d["key_claims"] = json.loads(d.pop("key_claims_json") or "[]")
                except Exception: d["key_claims"] = []
                try: d["web_evidence"] = json.loads(d.pop("web_evidence_json") or "[]")
                except Exception: d["web_evidence"] = []
                out["fact_card"] = d
        return out
    finally:
        conn.close()


@mcp.tool()
def recent_activity(days: int = 7, limit: int = 20) -> dict:
    """Get articles published in the last N days. Useful for 'what's he up to lately'."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, title, published_date, category, SUBSTR(body_text, 1, 280) AS snippet
               FROM articles WHERE language='EN' AND body_text IS NOT NULL AND body_text != ''
                 AND category IN ('press_release','speech','vp_speech')
                 AND published_date >= ?
               ORDER BY published_date DESC, id DESC LIMIT ?""",
            (since, min(limit, 100)),
        ).fetchall()
        return {"since": since, "count": len(rows), "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@mcp.tool()
def pipeline_run(budget_usd: float = 1.0) -> dict:
    """Trigger one pipeline cycle (scrape → extract → inspect → curate → verify → dv-compare).
    Gated by env KAHZAABU_MCP_ALLOW_PIPELINE=1 for safety."""
    if not os.environ.get("KAHZAABU_MCP_ALLOW_PIPELINE"):
        return {"error": "pipeline_run disabled — set KAHZAABU_MCP_ALLOW_PIPELINE=1 to enable"}
    if not _has_anthropic_key():
        return {"error": "ANTHROPIC_API_KEY not set"}
    from kahzaabu import pipeline as kp
    res = kp.run_pipeline(DB_PATH, daily_budget_usd=budget_usd)
    return res


# ---------- entry point ----------

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", type=int, default=0,
                        help="Run as HTTP/SSE on this port instead of stdio")
    args = parser.parse_args()
    if args.http:
        mcp.settings.port = args.http
        mcp.settings.host = "127.0.0.1"
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
