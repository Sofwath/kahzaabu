"""One-time migration: import existing JSON outputs into the new DB tables.

Inputs:
  data/phase3_full_claims.json    - 8.9k claims across 3.1k articles
  data/fact_check_master.json     - 48 existing curated items (source='existing_master')
  data/new_fact_checks.json       - 26 phase-2 items (source='phase2')
  data/full_new_fact_checks.json  - 108 phase-4 items (source='phase4')

Safe to re-run: claims insertion is bulk INSERT (could create dupes — see --safe).
fact_checks dedupe via UNIQUE(fingerprint) constraint.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kahzaabu import claims_db, db

DATA = Path(__file__).parent / "data"


def migrate_claims(conn, *, safe: bool = True) -> int:
    if safe:
        n_existing = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        if n_existing > 0:
            print(f"  claims table already has {n_existing} rows; skipping (use --reset to override)")
            return 0

    p = DATA / "phase3_full_claims.json"
    if not p.exists():
        print(f"  {p} not found; skipping claim migration")
        return 0
    print(f"  loading {p} ...")
    articles = json.loads(p.read_text())

    # Synthesize an extraction_run row to attribute these to
    run_id = claims_db.start_extraction_run(conn)
    total_in = sum(a.get("_in_tokens") or 0 for a in articles)
    total_out = sum(a.get("_out_tokens") or 0 for a in articles)
    total_cost = total_in / 1e6 * 3.0 + total_out / 1e6 * 15.0

    n_claims = 0
    n_articles = 0
    for art in articles:
        aid = art.get("article_id")
        if aid is None:
            continue
        # Look up language from articles table (default EN)
        row = conn.execute(
            "SELECT language FROM articles WHERE id = ? LIMIT 1", (aid,)
        ).fetchone()
        lang = row[0] if row else "EN"
        cs = art.get("claims") or []
        if not cs:
            # Insert sentinel so future re-extraction doesn't pick it up
            cs = [{"type": "no_specific_claims", "subject": None, "value": None,
                   "deadline": None, "actor_credited": None, "quote": None}]
        n_claims += claims_db.insert_claims(conn, run_id, aid, lang, cs)
        n_articles += 1
        if n_articles % 500 == 0:
            print(f"    {n_articles} articles migrated...")

    claims_db.finish_extraction_run(
        conn, run_id, articles_processed=n_articles, claims_extracted=n_claims,
        errors=0, tokens_in=total_in, tokens_out=total_out,
        cost_usd=total_cost, status="completed",
    )
    # Backdate the synthesized run so the historical cost doesn't count as "today's spend"
    conn.execute(
        "UPDATE extraction_runs SET started_at='2026-04-04T00:00:00+00:00', "
        "finished_at='2026-04-04T01:00:00+00:00' WHERE id = ?", (run_id,),
    )
    conn.commit()
    print(f"  inserted {n_claims} claims across {n_articles} articles "
          f"(synthesized extraction_run #{run_id}, ${total_cost:.2f} historical cost)")
    return n_claims


def migrate_fact_checks(conn) -> dict:
    counts = {"existing_master": 0, "phase2": 0, "phase4": 0, "dupes": 0}

    sources = [
        ("existing_master", DATA / "fact_check_master.json"),
        ("phase2", DATA / "new_fact_checks.json"),
        ("phase4", DATA / "full_new_fact_checks.json"),
    ]

    # Use a single synthesized curation_run for the historical import
    run_id = claims_db.start_curation_run(conn)
    total_new = 0

    for source, path in sources:
        if not path.exists():
            print(f"  {path} not found; skipping")
            continue
        items = json.loads(path.read_text())
        if not isinstance(items, list):
            print(f"  {path}: unexpected shape, skipping")
            continue
        print(f"  importing {len(items)} from {path.name} (source={source})")
        for item in items:
            # Normalize: existing_master uses 'date', others may too. Some use 'claim_date'.
            # Map 'source_article_ids' / 'evidence_quotes' if present; otherwise empty.
            normalized = {
                "category": item.get("category", "UNCLASSIFIED"),
                "claim_date": item.get("date") or item.get("claim_date") or "",
                "claim": item.get("claim", ""),
                "what_actually_happened": item.get("what_actually_happened"),
                "type": item.get("type"),
                "source_article_ids": item.get("source_article_ids", []),
                "evidence_quotes": item.get("evidence_quotes", []),
                "topic": item.get("_topic") or item.get("topic"),
            }
            new_id = claims_db.insert_fact_check(conn, normalized, run_id=run_id, source=source)
            if new_id is None:
                counts["dupes"] += 1
            else:
                counts[source] += 1
                total_new += 1

    claims_db.finish_curation_run(
        conn, run_id, chunks_processed=0, new_items=total_new,
        tokens_in=0, tokens_out=0, cost_usd=0.0, status="completed",
    )
    conn.execute(
        "UPDATE curation_runs SET started_at='2026-04-04T00:00:00+00:00', "
        "finished_at='2026-04-04T01:00:00+00:00' WHERE id = ?", (run_id,),
    )
    conn.commit()
    print(f"  inserted: existing_master={counts['existing_master']} "
          f"phase2={counts['phase2']} phase4={counts['phase4']} "
          f"dupes={counts['dupes']}")
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Drop and recreate claims/fact_checks/runs tables before migrating")
    parser.add_argument("--db", default=str(DATA / "kahzaabu.db"))
    args = parser.parse_args()

    conn = db.get_connection(Path(args.db))
    db.init_db(conn)
    claims_db.init_claims_schema(conn)

    if args.reset:
        confirm = input("DROP tables claims, extraction_runs, fact_checks, curation_runs? [yes/N] ")
        if confirm.strip().lower() == "yes":
            for tbl in ("claims", "extraction_runs", "fact_checks", "curation_runs"):
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            conn.commit()
            claims_db.init_claims_schema(conn)
            print("Tables reset.")
        else:
            print("Aborted.")
            return

    print("\n=== Migrating claims ===")
    migrate_claims(conn, safe=not args.reset)

    print("\n=== Migrating fact_checks ===")
    migrate_fact_checks(conn)

    print("\n=== Final stats ===")
    s = claims_db.stats(conn)
    for k, v in s.items():
        if k.startswith("last_"):
            continue
        print(f"  {k}: {v}")

    conn.close()


if __name__ == "__main__":
    main()
