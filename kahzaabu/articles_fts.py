# SPDX-License-Identifier: Apache-2.0
"""FTS5 BM25 retrieval over the articles table.

Companion to `kahzaabu.factcheck_search` (Slice 13) and
`kahzaabu.constitution.lookup` (Slice 11). Mirrors their pattern
intentionally — same module shape, same `_fts_sanitize()` helper,
same BM25-weights constant — so a future operator who learns one
understands the rest.

The motivating use case is **few-shot exemplar selection for the
press-office-style translator (Slice 16, ADR 0016)**. Given a piece
of input text, we want to find 3 articles whose body is topically
similar so we can use them as in-context examples for the LLM. BM25
ranks by multi-token coverage, so a query about "judicial service
commission" preferentially returns articles that mention all three
tokens (and likely have the corresponding DV text in the paired
article).

The virtual table indexes two columns:

    title (weight 3.0)  — short and high-signal
    body  (weight 1.0)  — long-form context

EN-only by default. The DV paired article is found via the
articles.paired_id join, NOT a separate Thaana FTS5 index. (FTS5's
default tokenizer doesn't understand Thaana word boundaries
anyway; a Thaana-aware tokenizer would be a separate slice.)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts
USING fts5(
    article_id UNINDEXED,
    language UNINDEXED,
    title,
    body
);
"""

# Sync triggers. INSERT/UPDATE/DELETE on articles propagate into
# articles_fts so the index stays current with the canonical table.
# Pattern lifted from factcheck_search.py.
#
# Note: we index BOTH languages (EN + DV) because the trigger fires
# regardless of language. Callers that want only EN apply a
# `WHERE language = 'EN'` filter on the joined articles row.
TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS articles_fts_ai
AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts (article_id, language, title, body)
    VALUES (new.id, new.language,
            COALESCE(new.title, ''),
            COALESCE(new.body_text, ''));
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_au
AFTER UPDATE OF title, body_text ON articles BEGIN
    DELETE FROM articles_fts
        WHERE article_id = old.id AND language = old.language;
    INSERT INTO articles_fts (article_id, language, title, body)
    VALUES (new.id, new.language,
            COALESCE(new.title, ''),
            COALESCE(new.body_text, ''));
END;
CREATE TRIGGER IF NOT EXISTS articles_fts_ad
AFTER DELETE ON articles BEGIN
    DELETE FROM articles_fts
        WHERE article_id = old.id AND language = old.language;
END;
"""

# title (3.0) outweighs body (1.0) — titles are short + high-signal.
_BM25_WEIGHTS = (3.0, 1.0)


def init_articles_fts(conn: sqlite3.Connection) -> bool:
    """Create the FTS5 virtual table and the sync triggers.
    Idempotent. Returns True iff FTS5 is available."""
    try:
        conn.executescript(FTS_SQL)
    except sqlite3.OperationalError as e:
        logger.info("articles FTS5 unavailable (%s)", e)
        return False
    conn.executescript(TRIGGERS_SQL)
    conn.commit()
    return True


def backfill_articles_fts(conn: sqlite3.Connection,
                            progress_cb=None) -> int:
    """Populate articles_fts from existing rows. Idempotent: clears
    the FTS table first, so re-running is a clean rebuild.

    On a 20k-article DB this takes ~30s — invoked on first init via
    init_claims_schema's lazy block, or manually via the CLI."""
    try:
        conn.execute("DELETE FROM articles_fts")
    except sqlite3.OperationalError:
        return 0
    total = conn.execute(
        "SELECT COUNT(*) FROM articles "
        "WHERE body_text IS NOT NULL AND body_text != ''"
    ).fetchone()[0]
    if total == 0:
        return 0
    BATCH = 500
    offset = 0
    written = 0
    while True:
        rows = conn.execute(
            "SELECT id, language, COALESCE(title, '') AS title, "
            "       COALESCE(body_text, '') AS body "
            "FROM articles "
            "WHERE body_text IS NOT NULL AND body_text != '' "
            "ORDER BY id LIMIT ? OFFSET ?",
            (BATCH, offset),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            conn.execute(
                "INSERT INTO articles_fts (article_id, language, title, body) "
                "VALUES (?, ?, ?, ?)",
                (r[0], r[1], r[2], r[3]),
            )
            written += 1
            if progress_cb is not None and written % 1000 == 0:
                progress_cb(written, total)
        conn.commit()
        offset += BATCH
    if progress_cb is not None:
        progress_cb(written, total)
    return written


def _fts_sanitize(query: str) -> str:
    """Quote each token so FTS5 operator chars (AND/OR/NEAR/", etc.)
    don't blow up the MATCH clause. Same approach as the sibling
    constitution + factcheck modules."""
    tokens = re.findall(r"[A-Za-z0-9']+", query)
    if not tokens:
        return '""'
    return " ".join(f'"{t}"' for t in tokens)


def search_articles(
    conn: sqlite3.Connection,
    query: str,
    *,
    language: str = "EN",
    limit: int = 10,
    require_paired: bool = False,
    recency_days: Optional[int] = None,
) -> list[dict]:
    """BM25 search over articles for the given language.

    `require_paired=True` filters to articles that have a paired DV
    (or EN) counterpart — used by the translator's few-shot selector
    which needs paired exemplars.

    `recency_days` (if set) additionally filters to articles
    published in that window. Combined with the topic-similarity
    score, this is what makes the hybrid (topic + recency) selection
    in the slice-16 plan work.

    Returns rows with article_id + BM25 rank (negative; smaller =
    more relevant) so callers can apply a threshold."""
    if not query or not query.strip():
        return []
    weights_sql = ", ".join(str(w) for w in _BM25_WEIGHTS)
    clauses = ["a.language = ?"]
    params: list = [language]
    if require_paired:
        clauses.append("a.paired_id IS NOT NULL")
    if recency_days is not None and recency_days > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=recency_days)
                  ).strftime("%Y-%m-%d")
        clauses.append("a.published_date >= ?")
        params.append(cutoff)
    where_extra = " AND " + " AND ".join(clauses) if clauses else ""
    sql = f"""
        SELECT a.id AS article_id, a.language, a.paired_id,
               a.title, a.body_text, a.published_date,
               a.category,
               bm25(articles_fts, {weights_sql}) AS rank
        FROM articles_fts f
        JOIN articles a ON a.id = f.article_id AND a.language = f.language
        WHERE articles_fts MATCH ?
          {where_extra}
        ORDER BY rank
        LIMIT ?
    """
    rows = conn.execute(sql, [_fts_sanitize(query), *params, limit]).fetchall()
    cols = ("article_id", "language", "paired_id", "title", "body_text",
             "published_date", "category", "rank")
    return [{cols[i]: r[i] for i in range(len(cols))} for r in rows]
