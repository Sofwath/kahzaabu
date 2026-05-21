"""Export fact_checks from DB back to JSON files for sharing / archival."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def export_fact_checks(conn: sqlite3.Connection, out_path: Path) -> dict:
    """Write the current fact_checks table to JSON, with web evidence joined in."""
    rows = conn.execute(
        """SELECT id, category, claim_date, claim, what_actually_happened, type,
                  source_article_ids, evidence_quotes, topic, source,
                  curation_run_id, confidence, created_at
           FROM fact_checks
           ORDER BY claim_date DESC, id"""
    ).fetchall()

    items = []
    n_with_evidence = 0
    for r in rows:
        fc_id = r[0]
        item = {
            "id": fc_id,
            "category": r[1],
            "date": r[2],
            "claim": r[3],
            "what_actually_happened": r[4],
            "type": r[5],
        }
        try:
            item["source_article_ids"] = json.loads(r[6]) if r[6] else []
        except Exception:
            item["source_article_ids"] = []
        try:
            item["evidence_quotes"] = json.loads(r[7]) if r[7] else []
        except Exception:
            item["evidence_quotes"] = []
        item["topic"] = r[8]
        item["_source"] = r[9]
        item["_confidence"] = r[11]

        # Attach web evidence if any
        ev_rows = conn.execute(
            """SELECT source_type, url, title, snippet, relevance, summary, retrieved_at
               FROM fact_check_evidence
               WHERE fact_check_id = ? ORDER BY id""",
            (fc_id,),
        ).fetchall()
        if ev_rows:
            item["web_evidence"] = [
                {
                    "source_type": e[0], "url": e[1], "title": e[2],
                    "snippet": e[3], "relevance": e[4], "summary": e[5],
                    "retrieved_at": e[6],
                }
                for e in ev_rows
            ]
            n_with_evidence += 1
        items.append(item)

    out_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    return {
        "out": str(out_path),
        "n_items": len(items),
        "n_with_web_evidence": n_with_evidence,
        "by_category": dict(Counter(it["category"] for it in items)),
        "by_source": dict(Counter(it.get("_source") for it in items)),
    }


def export_claims(conn: sqlite3.Connection, out_path: Path, *,
                  exclude_sentinels: bool = True) -> dict:
    """Write all claims to JSON (one record per article with claims array)."""
    sql = """SELECT c.article_id, c.language, c.type, c.subject, c.value,
                    c.deadline, c.actor_credited, c.quote,
                    a.title, a.published_date, a.category
             FROM claims c
             LEFT JOIN articles a ON c.article_id = a.id AND c.language = a.language"""
    if exclude_sentinels:
        sql += " WHERE c.type != 'no_specific_claims'"
    sql += " ORDER BY a.published_date, c.article_id, c.id"
    rows = conn.execute(sql).fetchall()

    by_article = {}
    for r in rows:
        key = (r[0], r[1])
        if key not in by_article:
            by_article[key] = {
                "article_id": r[0],
                "language": r[1],
                "title": r[8],
                "date": (r[9] or "")[:10],
                "category": r[10],
                "claims": [],
            }
        by_article[key]["claims"].append({
            "type": r[2], "subject": r[3], "value": r[4],
            "deadline": r[5], "actor_credited": r[6], "quote": r[7],
        })

    items = list(by_article.values())
    out_path.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    return {"out": str(out_path), "n_articles": len(items),
            "n_claims": sum(len(a["claims"]) for a in items)}


def export_all(db_path: Path, out_dir: Path) -> dict:
    """Write fact_checks + claims to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fc = export_fact_checks(conn, out_dir / f"fact_checks_{stamp}.json")
    # also write a stable "latest" copy
    fc_latest = export_fact_checks(conn, out_dir / "fact_checks_latest.json")
    claims = export_claims(conn, out_dir / "claims_latest.json")
    conn.close()
    return {"fact_checks": fc, "fact_checks_latest": fc_latest, "claims_latest": claims}
