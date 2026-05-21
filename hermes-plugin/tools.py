"""Agent-facing tools for the kahzaabu plugin.

Thin handlers — all heavy lifting lives in the canonical `kahzaabu` package
(imported, not vendored). KAHZAABU_HOME is derived from the package's own
__file__ at first use, so the plugin works regardless of where the dev tree
lives.

Tools:
  kahzaabu_stats            — archive counts, freshness, manifesto delivery breakdown
  kahzaabu_ask              — agentic NL Q&A (multi-turn via session_id)
  kahzaabu_list_lies        — fact-check listing with filters
  kahzaabu_get_factcheck    — single fact-check + linked claims + web evidence
  kahzaabu_manifesto        — 2023 campaign promises with delivery status
  kahzaabu_get_article      — single press release + claims + fact-checks
  kahzaabu_recent_activity  — recent articles within N days
  kahzaabu_pipeline_run     — trigger scrape→extract→curate cycle (gated)
"""
from __future__ import annotations

import functools
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional


# Set by __init__.py:register() to ctx.llm. Tool handlers read this to route
# the narrative-tricks guarantee-pass through hermes' provider config.
# None when running outside a hermes context (e.g. direct unit test, CLI/TUI/web).
#
# CONSTRAINT: module-level state, not per-call. Safe under hermes' current
# model (one plugin instance per process; register() runs once at startup).
# If hermes ever supports multiple PluginContexts in the same process
# (multi-tenant gateway?), this would race — the fix is to attach the llm
# to a contextvars.ContextVar keyed by request, or pass ctx through the
# handler signature. The hermes API currently calls handlers with
# (args, **_kw) and the kwargs do not include ctx, so until that changes,
# module-level is the pragmatic choice.
HOST_LLM = None


@functools.lru_cache(maxsize=1)
def kahzaabu_home() -> Optional[Path]:
    """Return the kahzaabu dev tree, derived from the imported package."""
    try:
        import kahzaabu
        return Path(kahzaabu.__file__).resolve().parents[1]
    except ImportError:
        return None


def db_path() -> Path:
    """Path to the kahzaabu SQLite DB. Resolves from the package each call
    (lru_cache on home keeps it cheap) so a moved dev tree just works."""
    home = kahzaabu_home()
    if home is None:
        # Last-resort fallback so error messages stay readable
        return Path("data/kahzaabu.db")
    return home / "data" / "kahzaabu.db"


def check_kahzaabu_requirements() -> bool:
    """Plugin is usable if the kahzaabu package imports AND the DB file exists."""
    return kahzaabu_home() is not None and db_path().exists()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(db_path()), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _has_anthropic_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _result(payload: Any) -> str:
    """Return a JSON-encoded tool result (hermes tools return strings)."""
    return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

STATS_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_stats",
    "description": (
        "Snapshot of the kahzaabu archive: total Muizzu-era articles, claims, "
        "published fact-checks, web-evidence rows, manifesto promises by "
        "delivery status, and data freshness (last scrape timestamp, hours "
        "since, is_stale). Call this first when asked about 'recent' or 'this "
        "week' to detect stale data."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

ASK_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_ask",
    "description": (
        "PREFERRED for any natural-language question about the Maldives "
        "Presidency archive (Mohamed Muizzu / kahzaabu — same person). Runs "
        "an internal multi-turn agent with 8 DB tools and optional "
        "web_search, and returns a synthesized Markdown answer with citations "
        "and a 🎭 narrative-tricks analysis. Use this INSTEAD of chaining "
        "kahzaabu_search_* / kahzaabu_get_* tools yourself — it produces "
        "better answers and costs less than 5+ separate tool calls. "
        "Pass session_id from a prior response to continue a conversation; "
        "the internal agent retains prior turns and tool results, so "
        "follow-ups are cheap. Only skip this tool when the user asks for "
        "raw data dumps or a specific row by id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "the question"},
            "session_id": {"type": "string", "description": "continue a prior session"},
            "enable_web": {"type": "boolean", "description": "allow web_search (default: true)"},
            "max_iterations": {"type": "integer", "description": "tool-use cap (default: 7)"},
        },
        "required": ["question"],
    },
}

LIST_LIES_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_list_lies",
    "description": (
        "List curated fact-checks with optional filters. Valid category "
        "values (case-sensitive, exact match): 'LIE', 'MISLEADING', "
        "'BROKEN DEADLINE', 'CREDIT THEFT', 'SHIFTING NUMBERS', "
        "'CONTRADICTION', and a few compound values like "
        "'LIE / MISLEADING'. Date filters apply to claim_date "
        "(when the claim was made). Returns id, category, claim, "
        "what_actually_happened, topic, claim_date — fetch full details "
        "with kahzaabu_get_factcheck."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "filter by category (see description for valid values)"},
            "topic": {"type": "string", "description": "filter by topic substring (LIKE %x%)"},
            "date_from": {"type": "string", "description": "claim_date YYYY-MM-DD inclusive"},
            "date_to": {"type": "string", "description": "claim_date YYYY-MM-DD inclusive"},
            "limit": {"type": "integer", "description": "default 50, max 200"},
        },
        "required": [],
    },
}

GET_FACTCHECK_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_get_factcheck",
    "description": (
        "Full detail for one fact-check: claim, contradiction, severity, "
        "topic, supporting claims with source article ids, and any web "
        "evidence (urls, snippets, agree/disagree)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    },
}

MANIFESTO_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_manifesto",
    "description": (
        "Browse Muizzu's 2023 campaign promises with delivery status "
        "(NOT_STARTED, IN_PROGRESS, DELAYED, DELIVERED, BROKEN). Filter by "
        "category, status, or text search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "status": {"type": "string"},
            "q": {"type": "string", "description": "search promise text"},
            "limit": {"type": "integer", "description": "default 50"},
        },
        "required": [],
    },
}

GET_ARTICLE_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_get_article",
    "description": (
        "Full press release / speech body with extracted claims and linked "
        "fact-checks. Use include_factcards=True to also fetch the per-"
        "article inspection card (summary, severity, history-check, viz spec) "
        "if it has been generated."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "article_id": {"type": "integer"},
            "include_factcards": {"type": "boolean", "description": "default true"},
        },
        "required": ["article_id"],
    },
}

RECENT_ACTIVITY_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_recent_activity",
    "description": (
        "Articles from the past N days. Good first call when asked 'what is "
        "Muizzu up to recently' or 'what happened this week'. Combine with "
        "kahzaabu_get_article for full content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "default 7"},
            "limit": {"type": "integer", "description": "default 20"},
        },
        "required": [],
    },
}

PIPELINE_RUN_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_pipeline_run",
    "description": (
        "Trigger a fresh scrape → extract → curate cycle. Gated by "
        "KAHZAABU_MCP_ALLOW_PIPELINE=1 to prevent runaway costs. Returns the "
        "stages run, articles added, and dollar cost."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "budget_usd": {"type": "number", "description": "hard cap, default 1.0"},
        },
        "required": [],
    },
}

CONSTITUTION_LOOKUP_SCHEMA: Dict[str, Any] = {
    "name": "kahzaabu_constitution_lookup",
    "description": (
        "Search the Constitution of the Republic of Maldives (English "
        "functional translation by Dheena Hussain, 2008 baseline) by "
        "keyword. Returns matching articles with their number, chapter, "
        "title, and body. Use this whenever a fact-check or presidential "
        "statement touches: presidential powers / conduct / election / "
        "removal, judicial independence, fundamental rights, religion of "
        "the State, separation of powers, election qualifications, "
        "emergency declarations, or any explicit claim about what 'the "
        "constitution says'. Cite article numbers inline as "
        "[Const. Art. NN]. Treat hits as evidence to point AT, NOT as "
        "legal opinion — interpretation is the Supreme Court's job. "
        "NOTE: this is a 2008 baseline translation; the legally binding "
        "text is the Dhivehi original and may have been amended since."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "keyword or short phrase"},
            "limit": {"type": "integer", "description": "default 5, max 10"},
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# Handlers — each takes (args, **_kw) and returns a JSON-encoded string.
# ---------------------------------------------------------------------------

def handle_stats(args: Dict[str, Any], **_kw) -> str:
    from kahzaabu import claims_db
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
        n_fc = conn.execute(
            "SELECT COUNT(*) FROM fact_checks WHERE published=1"
        ).fetchone()[0]
        n_ev = conn.execute(
            """SELECT COUNT(*) FROM fact_check_evidence e
               JOIN fact_checks f ON f.id = e.fact_check_id WHERE f.published=1"""
        ).fetchone()[0]
        n_mfs = conn.execute(
            "SELECT COUNT(*) FROM manifesto_promises WHERE published=1"
        ).fetchone()[0]
        by_status = {r[0]: r[1] for r in conn.execute(
            "SELECT delivery_status, COUNT(*) FROM manifesto_promises "
            "WHERE published=1 GROUP BY delivery_status"
        ).fetchall()}
        return _result({
            "articles_muizzu_era": n_articles,
            "claims_extracted": n_claims,
            "fact_checks": n_fc,
            "web_evidence_rows": n_ev,
            "manifesto_promises": n_mfs,
            "manifesto_by_delivery_status": by_status,
            "freshness": claims_db.freshness(conn),
            "db_path": str(db_path()),
        })
    finally:
        conn.close()


def handle_ask(args: Dict[str, Any], **_kw) -> str:
    if not _has_anthropic_key():
        return _result({"error": "ANTHROPIC_API_KEY not set; kahzaabu_ask "
                                  "requires it. Add to ~/.hermes/.env."})
    from kahzaabu.qna_agentic import ask_agentic
    conn = _conn()
    try:
        res = ask_agentic(
            conn, args["question"],
            session_id=args.get("session_id"),
            max_iterations=max(1, min(int(args.get("max_iterations", 7)), 8)),
            enable_web=bool(args.get("enable_web", True)),
            daily_budget_usd=5.0,
            host_llm=HOST_LLM,
        )
        return _result({
            "answer": res["answer"],
            "session_id": res["session_id"],
            "n_iterations": res["n_iterations"],
            "cost_usd": res["cost_usd"],
            "web_searches": res.get("web_searches", 0),
            "tool_trace": res.get("tool_trace", []),
        })
    finally:
        conn.close()


def handle_list_lies(args: Dict[str, Any], **_kw) -> str:
    conn = _conn()
    try:
        sql = ("SELECT id, category, claim, what_actually_happened, topic, "
               "claim_date, public_summary, confidence "
               "FROM fact_checks WHERE published=1")
        params: list = []
        if args.get("category"):
            sql += " AND category = ?"; params.append(args["category"])
        if args.get("topic"):
            sql += " AND topic LIKE ?"; params.append(f"%{args['topic']}%")
        if args.get("date_from"):
            sql += " AND claim_date >= ?"; params.append(args["date_from"])
        if args.get("date_to"):
            sql += " AND claim_date <= ?"; params.append(args["date_to"])
        limit = max(1, min(int(args.get("limit", 50)), 200))
        sql += " ORDER BY claim_date DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return _result({"count": len(rows), "items": [dict(r) for r in rows]})
    finally:
        conn.close()


def handle_get_factcheck(args: Dict[str, Any], **_kw) -> str:
    conn = _conn()
    try:
        fid = int(args["id"])
        fc = conn.execute(
            "SELECT * FROM fact_checks WHERE id = ? AND published = 1", (fid,)
        ).fetchone()
        if not fc:
            return _result({"error": f"fact_check {fid} not found or not published"})
        fc_d = dict(fc)
        try:
            fc_d["source_article_ids"] = json.loads(fc_d.get("source_article_ids") or "[]")
        except Exception:
            fc_d["source_article_ids"] = []
        try:
            fc_d["evidence_quotes"] = json.loads(fc_d.get("evidence_quotes") or "[]")
        except Exception:
            fc_d["evidence_quotes"] = []

        evidence = conn.execute(
            """SELECT url, title, snippet, relevance, summary, retrieved_at
               FROM fact_check_evidence WHERE fact_check_id = ? ORDER BY id""",
            (fid,)
        ).fetchall()

        # Source articles are referenced by id in fact_checks.source_article_ids (JSON).
        # No fact_check_claims join table exists — that was a port-from-memory bug.
        source_articles: list = []
        if fc_d["source_article_ids"]:
            placeholders = ",".join("?" * len(fc_d["source_article_ids"]))
            source_articles = [dict(r) for r in conn.execute(
                f"SELECT id, title, published_date FROM articles "
                f"WHERE id IN ({placeholders}) AND language='EN'",
                fc_d["source_article_ids"],
            ).fetchall()]

        return _result({
            "fact_check": fc_d,
            "web_evidence": [dict(r) for r in evidence],
            "source_articles": source_articles,
        })
    finally:
        conn.close()


def handle_manifesto(args: Dict[str, Any], **_kw) -> str:
    conn = _conn()
    try:
        sql = ("SELECT id, category, promise, delivery_status, evidence_note "
               "FROM manifesto_promises WHERE published=1")
        params: list = []
        if args.get("category"):
            sql += " AND category = ?"; params.append(args["category"])
        if args.get("status"):
            sql += " AND delivery_status = ?"; params.append(args["status"])
        if args.get("q"):
            sql += " AND promise LIKE ?"; params.append(f"%{args['q']}%")
        limit = max(1, min(int(args.get("limit", 50)), 200))
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return _result({"count": len(rows), "items": [dict(r) for r in rows]})
    finally:
        conn.close()


def handle_get_article(args: Dict[str, Any], **_kw) -> str:
    conn = _conn()
    try:
        aid = int(args["article_id"])
        art = conn.execute(
            "SELECT id, title, category, published_date, body_text, reference "
            "FROM articles WHERE id = ? AND language = 'EN'", (aid,)
        ).fetchone()
        if not art:
            return _result({"error": f"article {aid} not found"})
        claims = conn.execute(
            "SELECT id, type, subject, value, deadline, actor_credited, quote "
            "FROM claims WHERE article_id = ? AND language = 'EN'", (aid,)
        ).fetchall()
        # fact-checks are linked via fact_checks.source_article_ids (JSON array).
        # Use SQLite's JSON1 json_each() — robust to formatting (whitespace
        # changes, integer vs string element types). JSON1 is built into the
        # SQLite shipped with macOS and most Linux distros; if it's missing
        # we fall back to a LIKE chain that matches the canonical
        # `json.dumps([…])` output.
        try:
            factchecks = conn.execute(
                """SELECT fc.id, fc.category, fc.claim, fc.topic,
                          fc.claim_date, fc.confidence
                   FROM fact_checks fc, json_each(fc.source_article_ids) AS j
                   WHERE fc.published = 1 AND j.value = ?""",
                (aid,),
            ).fetchall()
        except sqlite3.OperationalError:
            factchecks = conn.execute(
                """SELECT id, category, claim, topic, claim_date, confidence
                   FROM fact_checks
                   WHERE published = 1
                     AND ( source_article_ids = ?
                           OR source_article_ids LIKE ?
                           OR source_article_ids LIKE ?
                           OR source_article_ids LIKE ? )""",
                (f"[{aid}]", f"[{aid},%", f"%, {aid},%", f"%, {aid}]"),
            ).fetchall()
        out: Dict[str, Any] = {
            "article": dict(art),
            "claims": [dict(r) for r in claims],
            "linked_fact_checks": [dict(r) for r in factchecks],
        }
        if bool(args.get("include_factcards", True)):
            try:
                card = conn.execute(
                    "SELECT * FROM article_fact_cards WHERE article_id = ? "
                    "AND language = 'EN' ORDER BY id DESC LIMIT 1", (aid,)
                ).fetchone()
                if card:
                    out["fact_card"] = dict(card)
            except sqlite3.OperationalError:
                pass  # table not yet present
        return _result(out)
    finally:
        conn.close()


def handle_recent_activity(args: Dict[str, Any], **_kw) -> str:
    days = max(1, min(int(args.get("days", 7)), 90))
    limit = max(1, min(int(args.get("limit", 20)), 100))
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT id, title, category, published_date,
                      SUBSTR(body_text, 1, 200) AS snippet
               FROM articles WHERE language='EN'
                 AND published_date >= DATE('now', ?)
                 AND body_text IS NOT NULL AND body_text != ''
               ORDER BY published_date DESC, id DESC LIMIT ?""",
            (f"-{days} days", limit)
        ).fetchall()
        return _result({"days": days, "count": len(rows),
                         "items": [dict(r) for r in rows]})
    finally:
        conn.close()


def handle_constitution_lookup(args: Dict[str, Any], **_kw) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return _result({"error": "query is required"})
    limit = max(1, min(int(args.get("limit", 5)), 10))
    from kahzaabu import constitution
    conn = _conn()
    try:
        hits = constitution.lookup(conn, query, limit=limit)
    except sqlite3.OperationalError as e:
        return _result({
            "error": f"constitution table missing: {e}. "
                     "Run: .venv/bin/python -m kahzaabu.constitution --import"
        })
    finally:
        conn.close()
    return _result({
        "query": query,
        "count": len(hits),
        "items": [
            {"article_no": h["article_no"],
             "chapter": h["chapter"],
             "title": h["title"],
             "body": h["body"][:800] + ("…" if len(h["body"]) > 800 else ""),
             "source": h["source_version"]}
            for h in hits
        ],
        "disclaimer": ("2008 functional translation; legally binding text is "
                        "the Dhivehi original. Constitutional interpretation "
                        "is the Supreme Court's role — these hits are "
                        "citations, not legal opinions."),
    })


def handle_pipeline_run(args: Dict[str, Any], **_kw) -> str:
    if os.environ.get("KAHZAABU_MCP_ALLOW_PIPELINE") != "1":
        return _result({
            "error": "pipeline trigger disabled — set "
                     "KAHZAABU_MCP_ALLOW_PIPELINE=1 in ~/.hermes/.env to "
                     "enable, then restart hermes."
        })
    if not _has_anthropic_key():
        return _result({"error": "ANTHROPIC_API_KEY not set"})
    from kahzaabu.pipeline import run_pipeline
    budget = float(args.get("budget_usd", 1.0))
    try:
        result = run_pipeline(budget_usd=budget)
        return _result({"ok": True, "result": result})
    except Exception as e:
        return _result({"ok": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Tool registration table — consumed by __init__.py
# ---------------------------------------------------------------------------

TOOLS = (
    ("kahzaabu_stats",                 STATS_SCHEMA,                 handle_stats,                 "📊"),
    ("kahzaabu_ask",                   ASK_SCHEMA,                   handle_ask,                   "🔍"),
    ("kahzaabu_list_lies",             LIST_LIES_SCHEMA,             handle_list_lies,             "🎭"),
    ("kahzaabu_get_factcheck",         GET_FACTCHECK_SCHEMA,         handle_get_factcheck,         "📋"),
    ("kahzaabu_manifesto",             MANIFESTO_SCHEMA,             handle_manifesto,             "📜"),
    ("kahzaabu_get_article",           GET_ARTICLE_SCHEMA,           handle_get_article,           "📰"),
    ("kahzaabu_recent_activity",       RECENT_ACTIVITY_SCHEMA,       handle_recent_activity,       "📅"),
    ("kahzaabu_constitution_lookup",   CONSTITUTION_LOOKUP_SCHEMA,   handle_constitution_lookup,   "⚖️"),
    ("kahzaabu_pipeline_run",          PIPELINE_RUN_SCHEMA,          handle_pipeline_run,          "🔄"),
)
