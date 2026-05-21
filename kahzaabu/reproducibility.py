# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 12 — Reproducibility manifest (ADR 0010).

For a given `fact_check_id`, assemble the complete provenance trace —
the curation_run that produced it, the underlying claims with their
extraction_runs, the verification evidence with its run + model, the
decomposition questions, contradiction-pair details, claimreview JSON-LD,
and the git commit at publication time.

Everything but `git_sha_at_publication` already lives in the DB; this
module just joins it together.

Use:
    from kahzaabu.reproducibility import get_manifest, current_git_sha

    manifest = get_manifest(conn, fact_check_id=87)
    # → JSON-serialisable dict; web endpoint dumps it directly

CLI: `kahzaabu reproducibility <fact_check_id>` prints the JSON.
Web:  GET /api/reproducibility/{id}.json returns it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("kahzaabu")


def current_git_sha() -> Optional[str]:
    """Return the current HEAD SHA, or None if not in a git work tree.

    Used at publish time to stamp `fact_checks.git_sha_at_publication`.
    Failures are silent — provenance is best-effort, not required.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return None


def stamp_git_sha(conn: sqlite3.Connection, fact_check_id: int) -> Optional[str]:
    """Idempotently stamp git_sha_at_publication on a fact_check row.

    Returns the stamped SHA (the existing value if already set, else
    the current HEAD if available, else None).
    """
    row = conn.execute(
        "SELECT git_sha_at_publication FROM fact_checks WHERE id = ?",
        (fact_check_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    sha = current_git_sha()
    if sha:
        conn.execute(
            "UPDATE fact_checks SET git_sha_at_publication = ? WHERE id = ?",
            (sha, fact_check_id),
        )
        conn.commit()
    return sha


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()} if row else {}


def get_manifest(conn: sqlite3.Connection,
                  fact_check_id: int) -> Optional[dict]:
    """Assemble the full reproducibility manifest for one fact_check.

    Returns None if the fact_check_id does not exist.
    """
    conn.row_factory = sqlite3.Row

    fc = conn.execute(
        "SELECT * FROM fact_checks WHERE id = ?", (fact_check_id,)
    ).fetchone()
    if fc is None:
        return None

    # ── produced_by: curation_run details (best-effort)
    produced_by: dict = {}
    if fc["curation_run_id"]:
        crow = conn.execute(
            "SELECT id, started_at, finished_at, tokens_in, tokens_out, "
            "       cost_usd, status FROM curation_runs WHERE id = ?",
            (fc["curation_run_id"],),
        ).fetchone()
        if crow:
            produced_by = {
                "curation_run_id": crow["id"],
                "started_at":      crow["started_at"],
                "finished_at":     crow["finished_at"],
                "tokens_in":       crow["tokens_in"],
                "tokens_out":      crow["tokens_out"],
                "cost_usd":        crow["cost_usd"],
                "status":          crow["status"],
                "curator_model":   "claude-sonnet-4-6",  # current stage default
            }

    # ── supporting_claims: parse the JSON article-id list, then join
    #     claims for each article that share the fact_check's topic
    supporting_claims: list[dict] = []
    try:
        article_ids = json.loads(fc["source_article_ids"] or "[]")
    except (json.JSONDecodeError, TypeError):
        article_ids = []
    if article_ids:
        placeholders = ",".join("?" * len(article_ids))
        # Pull a few representative claims per article so we keep the
        # manifest tractable on long press releases.
        claims = conn.execute(
            f"""
            SELECT c.id AS claim_id, c.article_id, c.language,
                   c.type, c.polarity, c.is_checkable,
                   c.canonical_claim_id, c.extraction_run_id,
                   c.quote, a.title AS article_title
            FROM claims c
            LEFT JOIN articles a
                   ON a.id = c.article_id AND a.language = c.language
            WHERE c.article_id IN ({placeholders})
              AND c.is_checkable = 1
            ORDER BY c.article_id, c.id
            LIMIT 60
            """,
            article_ids,
        ).fetchall()
        supporting_claims = [
            {
                "claim_id":            r["claim_id"],
                "article_id":          r["article_id"],
                "language":            r["language"],
                "type":                r["type"],
                "polarity":            r["polarity"],
                "canonical_claim_id":  r["canonical_claim_id"],
                "extraction_run_id":   r["extraction_run_id"],
                "quote":               (r["quote"] or "")[:280],
                "article_title":       r["article_title"],
            }
            for r in claims
        ]

    # ── verification_evidence: rows from fact_check_evidence + runs
    evidence_rows = conn.execute(
        """
        SELECT e.id, e.url, e.title, e.snippet, e.relevance, e.summary,
               e.retrieved_at, e.verification_run_id,
               e.authoritative_entity_id,
               vr.tokens_in, vr.tokens_out, vr.web_searches,
               vr.cost_usd AS verification_cost_usd
        FROM fact_check_evidence e
        LEFT JOIN verification_runs vr ON vr.id = e.verification_run_id
        WHERE e.fact_check_id = ?
        ORDER BY e.id
        """,
        (fact_check_id,),
    ).fetchall()
    verification_evidence = [
        {
            "evidence_id":             r["id"],
            "url":                     r["url"],
            "title":                   r["title"],
            "snippet":                 (r["snippet"] or "")[:280],
            "relevance":               r["relevance"],
            "summary":                 r["summary"],
            "retrieved_at":            r["retrieved_at"],
            "verification_run_id":     r["verification_run_id"],
            "authoritative_entity_id": r["authoritative_entity_id"],
            "verifier_model":          "claude-haiku-4-5",
            "verification_cost_usd":   r["verification_cost_usd"],
        }
        for r in evidence_rows
    ]

    # ── decomposition_questions: for the (canonical) claims that
    #     feed this fact-check
    decomposition: list[dict] = []
    canonical_ids = [c["claim_id"] for c in supporting_claims] + [
        c["canonical_claim_id"] for c in supporting_claims
        if c["canonical_claim_id"]
    ]
    if canonical_ids:
        seen = set(canonical_ids)
        placeholders = ",".join("?" * len(seen))
        qrows = conn.execute(
            f"""
            SELECT q.claim_id, q.question, q.answer_type, q.source_medium,
                   q.decomposition_run_id
            FROM claim_questions q
            WHERE q.claim_id IN ({placeholders})
            ORDER BY q.claim_id, q.id
            LIMIT 80
            """,
            list(seen),
        ).fetchall()
        decomposition = [
            {
                "claim_id":              r["claim_id"],
                "question":              r["question"],
                "answer_type":           r["answer_type"],
                "source_medium":         r["source_medium"],
                "decomposition_run_id":  r["decomposition_run_id"],
            }
            for r in qrows
        ]

    # ── contradiction pair (when this fact-check originated from one)
    contradiction_pair: Optional[dict] = None
    if fc["contradiction_pair_id"]:
        cp = conn.execute(
            "SELECT * FROM contradiction_pairs WHERE id = ?",
            (fc["contradiction_pair_id"],),
        ).fetchone()
        if cp:
            cp_dict = _row_to_dict(cp)
            # Keep raw JSON for reasoning_chain; clients parse.
            contradiction_pair = {
                "id":             cp_dict.get("id"),
                "claim_a_id":     cp_dict.get("claim_a_id"),
                "claim_b_id":     cp_dict.get("claim_b_id"),
                "verdict":        cp_dict.get("verdict"),
                "confidence":     cp_dict.get("confidence"),
                "reasoning_chain": cp_dict.get("reasoning_chain"),
                "finder_run_id":  cp_dict.get("finder_run_id"),
            }

    # ── claimreview JSON-LD (cached payload, parsed if present)
    claimreview = None
    if fc["claimreview_jsonld"]:
        try:
            claimreview = json.loads(fc["claimreview_jsonld"])
        except (json.JSONDecodeError, TypeError):
            claimreview = None

    return {
        "fact_check_id":           fact_check_id,
        "category":                fc["category"],
        "claim":                   fc["claim"],
        "claim_date":              fc["claim_date"],
        "topic":                   fc["topic"],
        "speaker":                 fc["speaker"],
        "confidence":              fc["confidence"],
        "verdict_label":           fc["verdict_label"],
        "truth_score":             fc["truth_score"],
        "truth_score_label":       fc["truth_score_label"],
        "reasoning_chain":         (
            json.loads(fc["reasoning_chain"])
            if fc["reasoning_chain"] else None
        ),
        "produced_by":             produced_by or None,
        "supporting_claims":       supporting_claims,
        "verification_evidence":   verification_evidence,
        "decomposition_questions": decomposition,
        "contradiction_pair":      contradiction_pair,
        "claimreview_jsonld":      claimreview,
        "git_sha_at_publication":  fc["git_sha_at_publication"],
        "_schema_version":         "v2.12",
    }


def get_manifest_json(conn: sqlite3.Connection,
                       fact_check_id: int,
                       indent: int = 2) -> Optional[str]:
    """Convenience wrapper: returns JSON-serialised manifest, or None."""
    m = get_manifest(conn, fact_check_id)
    if m is None:
        return None
    return json.dumps(m, indent=indent, default=str)
