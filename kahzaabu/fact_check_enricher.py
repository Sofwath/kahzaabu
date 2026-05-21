"""V2 Slice 5 — enrich fact_checks with verdict_label / truth_score /
truth_score_label / reasoning_chain / contradiction_pair_id / speaker /
canonical_url (ADR 0005, 0006).

ALL DETERMINISTIC — no LLM calls. Pure SQL + Python derivation from
data the earlier slices already populated:

- verdict_label / truth_score / truth_score_label
    come from truth_score.derive_all(category, confidence).

- reasoning_chain
    assembled from claim_questions of the supporting claims (the
    fact_checks.source_article_ids JSON points to articles → claims
    → claim_questions). RAGAR Chain-of-RAG shape.

- contradiction_pair_id
    set when the fact-check was born from a contradiction_pair
    (Slice 4). NULL otherwise.

- speaker, canonical_url
    ADR 0006 placeholders for the ClaimReview JSON-LD export
    (Slice 6). Speaker defaults to "Mohamed Muizzu"; canonical_url
    stays NULL until a public deploy lands.

Idempotent: re-runs only touch fact_checks where verdict_label IS NULL.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from . import claims_db
from . import truth_score

logger = logging.getLogger("kahzaabu")

# Max number of Q&A entries to include in the reasoning_chain. Bound the
# size so the JSON column stays readable (the full corpus has up to ~30
# questions per fact-check across the source articles; we keep top-N).
MAX_REASONING_CHAIN = 8


def _supporting_claim_ids(conn, fact_check_id: int) -> list[int]:
    """Walk fact_checks.source_article_ids → claims that came from those
    articles. Returns claim ids in deterministic order."""
    r = conn.execute(
        "SELECT source_article_ids FROM fact_checks WHERE id = ?",
        (fact_check_id,),
    ).fetchone()
    if not r:
        return []
    raw = r[0] if not hasattr(r, "keys") else r["source_article_ids"]
    try:
        article_ids = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not article_ids:
        return []
    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"""SELECT id FROM claims
            WHERE article_id IN ({placeholders})
              AND language = 'EN'
              AND type != 'no_specific_claims'
            ORDER BY id""",
        article_ids,
    ).fetchall()
    return [r[0] for r in rows]


def _assemble_reasoning_chain(conn, fact_check_id: int,
                                contradiction_pair_id: Optional[int]
                                ) -> list[dict]:
    """Build a RAGAR-shaped reasoning chain.

    Priority:
      1. If contradiction_pair_id is set, use that pair's reasoning_chain
         (the LLM verifier already produced a defensible chain).
      2. Otherwise, gather claim_questions of the supporting claims;
         each (question, answer, source_medium, source_url) becomes one
         step in the chain. Trimmed to MAX_REASONING_CHAIN entries.
    """
    if contradiction_pair_id:
        r = conn.execute(
            "SELECT reasoning_chain FROM contradiction_pairs WHERE id = ?",
            (contradiction_pair_id,),
        ).fetchone()
        if r and (r[0] if not hasattr(r, "keys") else r["reasoning_chain"]):
            raw = r[0] if not hasattr(r, "keys") else r["reasoning_chain"]
            try:
                chain = json.loads(raw)
                if isinstance(chain, list):
                    return chain
            except (TypeError, json.JSONDecodeError):
                pass

    claim_ids = _supporting_claim_ids(conn, fact_check_id)
    if not claim_ids:
        return []
    placeholders = ",".join("?" * len(claim_ids))
    rows = conn.execute(
        f"""SELECT question, answer, answer_type, source_medium, source_url
            FROM claim_questions
            WHERE claim_id IN ({placeholders})
            ORDER BY id
            LIMIT ?""",
        claim_ids + [MAX_REASONING_CHAIN],
    ).fetchall()
    chain = []
    for r in rows:
        chain.append({
            "question":      r[0] if not hasattr(r, "keys") else r["question"],
            "answer":        r[1] if not hasattr(r, "keys") else r["answer"],
            "answer_type":   r[2] if not hasattr(r, "keys") else r["answer_type"],
            "source_medium": r[3] if not hasattr(r, "keys") else r["source_medium"],
            "source_url":    r[4] if not hasattr(r, "keys") else r["source_url"],
        })
    return chain


def enrich_fact_check(conn, fact_check_id: int) -> dict:
    """Enrich one fact_check by id. Returns the new field values.
    Idempotent — overwrites whatever was there."""
    r = conn.execute(
        "SELECT category, confidence FROM fact_checks WHERE id = ?",
        (fact_check_id,),
    ).fetchone()
    if not r:
        return {}
    category = r[0] if not hasattr(r, "keys") else r["category"]
    confidence = r[1] if not hasattr(r, "keys") else r["confidence"]

    derived = truth_score.derive_all(category, confidence)
    # contradiction_pair_id may already be set (Slice 4 promotion).
    pid_row = conn.execute(
        "SELECT contradiction_pair_id FROM fact_checks WHERE id = ?",
        (fact_check_id,),
    ).fetchone()
    pid = pid_row[0] if pid_row else None

    chain = _assemble_reasoning_chain(conn, fact_check_id, pid)

    conn.execute(
        """UPDATE fact_checks
           SET verdict_label     = ?,
               truth_score       = ?,
               truth_score_label = ?,
               reasoning_chain   = ?
           WHERE id = ?""",
        (derived["verdict_label"], derived["truth_score"],
         derived["truth_score_label"], json.dumps(chain), fact_check_id),
    )
    conn.commit()
    return {**derived, "reasoning_chain_steps": len(chain),
            "contradiction_pair_id": pid}


def promote_contradictions_to_factchecks(conn) -> int:
    """Slice 4 produces contradiction_pairs with verdict='CONTRADICTION'.
    Promote each to a fact_check (category='CONTRADICTION') so the
    public layer surfaces it. Idempotent — pairs already promoted are
    skipped.

    Returns the number of new fact_checks created."""
    from datetime import datetime, timezone
    rows = conn.execute(
        """SELECT cp.id, cp.claim_a_id, cp.claim_b_id, cp.subject,
                  cp.confidence, cp.reasoning_chain, cp.detected_at,
                  ca.quote AS quote_a, cb.quote AS quote_b,
                  aa.id AS article_a_id, ab.id AS article_b_id,
                  ca.subject_normalized
           FROM contradiction_pairs cp
           JOIN claims ca ON ca.id = cp.claim_a_id
           JOIN claims cb ON cb.id = cp.claim_b_id
           JOIN articles aa ON aa.id = ca.article_id AND aa.language = ca.language
           JOIN articles ab ON ab.id = cb.article_id AND ab.language = cb.language
           LEFT JOIN fact_checks fc ON fc.contradiction_pair_id = cp.id
           WHERE cp.verdict = 'CONTRADICTION'
             AND fc.id IS NULL"""
    ).fetchall()
    n = 0
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else dict(zip([
            "id","claim_a_id","claim_b_id","subject","confidence",
            "reasoning_chain","detected_at","quote_a","quote_b",
            "article_a_id","article_b_id","subject_normalized"], r))
        claim_text = (f'{d["subject"]}: {d["quote_a"]!r} ↔ {d["quote_b"]!r}')[:500]
        source_ids = json.dumps([d["article_a_id"], d["article_b_id"]])
        fingerprint = f"contradiction:{d['id']}"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO fact_checks
               (category, claim_date, claim, what_actually_happened,
                topic, source_article_ids, evidence_quotes, confidence,
                source, fingerprint, created_at,
                contradiction_pair_id, published)
               VALUES ('CONTRADICTION', ?, ?, ?, ?, ?, '[]', 'auto',
                       'auto', ?, ?, ?, 0)""",
            (d["detected_at"][:10], claim_text,
             "See reasoning_chain for the LLM verifier's analysis",
             d["subject_normalized"] or d["subject"], source_ids,
             fingerprint, now, d["id"]),
        )
        if conn.total_changes:
            n += 1
    conn.commit()
    return n


def run_enrichment(conn, *, limit: Optional[int] = None,
                    only_unset: bool = True,
                    progress_cb=None) -> dict:
    """Enrich all fact_checks. Backfill mode (only_unset=True) skips
    rows that already have verdict_label.

    Returns counts + a verdict distribution."""
    claims_db.init_claims_schema(conn)

    n_promoted = promote_contradictions_to_factchecks(conn)
    if n_promoted:
        logger.info("promoted %d contradiction(s) to fact_checks", n_promoted)

    sql = "SELECT id FROM fact_checks"
    if only_unset:
        sql += " WHERE verdict_label IS NULL"
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    ids = [r[0] for r in conn.execute(sql)]

    by_verdict: dict[str, int] = {}
    by_truth: dict[int, int] = {}
    for i, fcid in enumerate(ids):
        d = enrich_fact_check(conn, fcid)
        v = d.get("verdict_label", "?")
        t = d.get("truth_score", 0)
        by_verdict[v] = by_verdict.get(v, 0) + 1
        by_truth[t] = by_truth.get(t, 0) + 1
        if progress_cb and (i % 50 == 0 or i == len(ids) - 1):
            progress_cb(i + 1, len(ids))

    return {
        "promoted_contradictions": n_promoted,
        "enriched": len(ids),
        "by_verdict_label": by_verdict,
        "by_truth_score": by_truth,
    }
