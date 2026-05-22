# SPDX-License-Identifier: Apache-2.0
"""FTS5 BM25 retrieval over the fact_checks table.

Companion to `kahzaabu.constitution` which does the same for the
301 Constitution articles. Mirrors its pattern intentionally — same
module shape, same `_fts_sanitize()` helper, same BM25-weights
constant — so a future operator who learns one understands the other.

The FTS5 virtual table indexes three columns:

    claim                  — the actual fact-checked statement;
                              highest weight (3.0)
    topic                  — categorical bucket (housing, foreign,
                              judicial, etc.); medium weight (2.0)
    what_actually_happened — long-form explanation; lowest weight
                              (1.0)

Triggers keep the index in sync on insert/update/delete so callers
that mutate `fact_checks` via the normal `INSERT INTO`/`UPDATE`
paths don't need to touch the FTS table themselves.

The motivating use case is reverse cross-reference on the
Constitution browser: given a constitutional article, find
fact-checks whose claim text matches its body. The earlier
implementation used `LIKE %longest_title_token%` which over-
matched on common tokens like "Constitution" or "President"
(see ADR-worthy follow-up). BM25 ranks by relevance and respects
multi-token coverage, so a query for `"Judicial Service Commission"`
preferentially ranks fact-checks that mention all three tokens
over fact-checks that mention just one.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fact_checks_fts
USING fts5(
    fact_check_id UNINDEXED,
    claim,
    topic,
    what_actually_happened
);
"""

# Triggers keep fact_checks_fts in sync with fact_checks. The
# AFTER UPDATE trigger uses a DELETE+INSERT instead of an
# UPDATE-on-FTS because FTS5 row identity is row id, and we want
# the indexed snapshot to fully reflect post-update column values.
TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS fact_checks_fts_ai
AFTER INSERT ON fact_checks BEGIN
    INSERT INTO fact_checks_fts
        (fact_check_id, claim, topic, what_actually_happened)
    VALUES
        (new.id, COALESCE(new.claim, ''),
                  COALESCE(new.topic, ''),
                  COALESCE(new.what_actually_happened, ''));
END;
CREATE TRIGGER IF NOT EXISTS fact_checks_fts_au
AFTER UPDATE OF claim, topic, what_actually_happened
ON fact_checks BEGIN
    DELETE FROM fact_checks_fts WHERE fact_check_id = old.id;
    INSERT INTO fact_checks_fts
        (fact_check_id, claim, topic, what_actually_happened)
    VALUES
        (new.id, COALESCE(new.claim, ''),
                  COALESCE(new.topic, ''),
                  COALESCE(new.what_actually_happened, ''));
END;
CREATE TRIGGER IF NOT EXISTS fact_checks_fts_ad
AFTER DELETE ON fact_checks BEGIN
    DELETE FROM fact_checks_fts WHERE fact_check_id = old.id;
END;
"""

# Column-order weights for bm25(); must match FTS_SQL excluding the
# leading UNINDEXED column.
_BM25_WEIGHTS = (3.0, 2.0, 1.0)


def init_fact_checks_fts(conn: sqlite3.Connection) -> bool:
    """Create the FTS5 virtual table and the sync triggers.
    Idempotent. Returns True iff FTS5 is available in this SQLite
    build — callers that need FTS5 should branch on the return value
    and fall back to LIKE."""
    try:
        conn.executescript(FTS_SQL)
    except sqlite3.OperationalError as e:
        logger.info("fact_checks FTS5 unavailable (%s)", e)
        return False
    # Triggers are unrelated to FTS5 the feature — they're just SQL —
    # but they're useless without the virtual table existing first.
    conn.executescript(TRIGGERS_SQL)
    conn.commit()
    return True


def backfill_fact_checks_fts(conn: sqlite3.Connection) -> int:
    """Populate fact_checks_fts from existing fact_checks rows.

    Safe to call repeatedly: clears the FTS table before inserting,
    so re-running picks up any schema changes (e.g., a future column
    added to the index). Called automatically by `init_claims_schema`
    when the FTS table is first created.

    Returns the row count after backfill (0 if FTS5 unavailable)."""
    try:
        conn.execute("DELETE FROM fact_checks_fts")
    except sqlite3.OperationalError:
        return 0
    rows = conn.execute(
        "SELECT id, COALESCE(claim, '') AS claim, "
        "       COALESCE(topic, '') AS topic, "
        "       COALESCE(what_actually_happened, '') AS what_actually_happened "
        "FROM fact_checks"
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT INTO fact_checks_fts "
            "(fact_check_id, claim, topic, what_actually_happened) "
            "VALUES (?, ?, ?, ?)",
            (r[0], r[1], r[2], r[3])
        )
    conn.commit()
    return len(rows)


def _fts_sanitize(query: str) -> str:
    """Same approach as kahzaabu.constitution._fts_sanitize: tokenize
    and double-quote each token so FTS5 special operators (AND, OR,
    NOT, NEAR, *, ", :) don't blow up the MATCH clause."""
    tokens = re.findall(r"[A-Za-z0-9']+", query)
    if not tokens:
        return '""'
    return " ".join(f'"{t}"' for t in tokens)


def search_fact_checks(conn: sqlite3.Connection,
                        query: str,
                        limit: int = 10,
                        published_only: bool = True) -> list[dict]:
    """BM25 search over fact_checks with a substring-fallback for
    the zero-results case.

    Returns rows with fc fields + a `rank` field. `rank` is BM25
    (negative; lower = more relevant) when FTS5 found hits, OR a
    negative count-based pseudo-score when the substring fallback
    fired. Either way, smaller is better — so callers can apply
    "rank < threshold" to drop weak matches and the meaning is
    consistent.

    The substring fallback addresses a real FTS5 brittleness:
    "Judicial Service Commission" won't FTS5-match a claim that
    says "JSC" (the acronym), but it WILL substring-match. We
    score the fallback by counting how many of the query's 4+
    char tokens appear in the claim/topic/explanation — multi-
    token coverage rather than single-token LIKE, so an article
    on "Composition of People's Majlis" preferentially returns
    fact-checks that mention both "Majlis" AND "composition"
    over fact-checks that mention just one."""
    if not query or not query.strip():
        return []

    pub_clause = " AND fc.published = 1" if published_only else ""

    # ── Path 1: FTS5 BM25 ──────────────────────────────────────
    weights_sql = ", ".join(str(w) for w in _BM25_WEIGHTS)
    try:
        rows = conn.execute(
            f"""SELECT fc.id, fc.category, fc.claim, fc.topic,
                       fc.verdict_label, fc.truth_score_label,
                       fc.claim_date,
                       bm25(fact_checks_fts, {weights_sql}) AS rank
               FROM fact_checks_fts f
               JOIN fact_checks fc ON fc.id = f.fact_check_id
               WHERE fact_checks_fts MATCH ?
                 {pub_clause}
               ORDER BY rank
               LIMIT ?""",
            (_fts_sanitize(query), limit),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        pass  # FTS5 missing — drop into the substring path

    # ── Path 2: multi-token LIKE-OR with match-count rank ──────
    # Tokens 4+ chars only (drops "the", "of", etc.); dedup,
    # cap at 12 to keep the SQL OR-chain bounded.
    tokens = list(dict.fromkeys(
        t.lower() for t in re.findall(r"[A-Za-z0-9']+", query)
        if len(t) >= 4
    ))[:12]
    if not tokens:
        return []
    # Build a CASE expression that counts how many tokens appear
    # in the indexed text (claim + topic + what_actually_happened),
    # then order by that count DESC + claim_date DESC for ties.
    case_parts = []
    params: list = []
    for tok in tokens:
        case_parts.append(
            "(CASE WHEN LOWER(fc.claim) LIKE ? OR LOWER(fc.topic) LIKE ? "
            "OR LOWER(COALESCE(fc.what_actually_happened, '')) LIKE ? "
            "THEN 1 ELSE 0 END)"
        )
        params += [f"%{tok}%"] * 3
    case_sum = " + ".join(case_parts)
    where_any = " OR ".join(
        "LOWER(fc.claim) LIKE ? OR LOWER(fc.topic) LIKE ? "
        "OR LOWER(COALESCE(fc.what_actually_happened, '')) LIKE ?"
        for _ in tokens
    )
    params_where: list = []
    for tok in tokens:
        params_where += [f"%{tok}%"] * 3
    rows = conn.execute(
        f"""SELECT fc.id, fc.category, fc.claim, fc.topic,
                   fc.verdict_label, fc.truth_score_label,
                   fc.claim_date,
                   -({case_sum}) AS rank
           FROM fact_checks fc
           WHERE ({where_any})
             {pub_clause}
           ORDER BY rank, fc.claim_date DESC
           LIMIT ?""",
        params + params_where + [limit],
    ).fetchall()
    return [dict(r) for r in rows]
