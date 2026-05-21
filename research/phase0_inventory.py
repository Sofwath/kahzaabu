"""Phase 0: inventory of post-2026-02-05 articles for incremental fact-check update.

Free — no API calls. Builds:
  - data/post_feb5_corpus.json : list of articles in the window with metadata
  - prints summary breakdowns
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).parent / "data" / "kahzaabu.db"
OUT = Path(__file__).parent / "data"

CUTOFF = "2026-02-06"  # day after Presidential Address 2026 (last curated date)

# Topic markers for quick visibility into what's being claimed
TOPIC_PATTERNS = {
    "housing": re.compile(r"\b(hous(?:e|ing)|flat|reclamat|hectare|ras\s*mal|gulhifalhu|uthuruthilafalhu|hulhumal)\b", re.I),
    "debt/fiscal": re.compile(r"\b(debt|deficit|budget|fiscal|sukuk|reserve|GDP|EXIM|loan|swap|austerity)\b", re.I),
    "india/diplomacy": re.compile(r"\b(india|china|EXIM|line of credit|state visit|bilateral|diplomatic)\b", re.I),
    "infrastructure": re.compile(r"\b(airport|port|bridge|harbour|terminal|RTL|ferry|road|inaugurat|groundbreak|hospital|school)\b", re.I),
    "tourism": re.compile(r"\b(resort|tourism|tourist|bed[s]?\b)", re.I),
    "credit-claim": re.compile(r"\b(previously stalled|inherited|under (?:the|this) administration|since taking office|in less than|first time in)\b", re.I),
    "deadlines": re.compile(r"\b(this year|next year|by (?:the )?end of|within (?:the )?(?:next )?\d+\s*(?:months?|years?|weeks?)|will be (?:completed|delivered|inaugurated))\b", re.I),
    "numbers": re.compile(r"\b\d{1,3}(?:,\d{3})+|\b\d{2,}\s*(?:units?|projects?|islands?|hectares?|beds?|tonnes?|million|billion|MVR|USD|%)\b", re.I),
    "spokesperson": re.compile(r"\b(Chief Government Spokesperson|Presser with the Spox|Mohamed Hussain Shareef)\b", re.I),
    "cabinet-committee": re.compile(r"Special Cabinet Committee", re.I),
    "Middle East tensions": re.compile(r"\b(Middle East|tensions|ceasefire|Gulf|food security|fuel security)\b", re.I),
}


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, category, title, body_text, published_date
           FROM articles
           WHERE language = 'EN'
             AND published_date >= ?
             AND category IN ('press_release','speech','vp_speech')
             AND body_text IS NOT NULL AND body_text != ''
           ORDER BY published_date, id""",
        (CUTOFF,),
    ).fetchall()
    conn.close()

    articles = [dict(r) for r in rows]
    print(f"Total post-{CUTOFF} EN articles: {len(articles)}")
    by_cat = Counter(a["category"] for a in articles)
    print(f"  by category: {dict(by_cat)}")

    months = Counter(a["published_date"][:7] for a in articles)
    print(f"  by month: {dict(sorted(months.items()))}")

    print("\n=== Articles tagged by topic markers ===\n")
    topic_counts = {}
    for tag, pat in TOPIC_PATTERNS.items():
        matched = [a for a in articles if pat.search(a["title"]) or pat.search(a["body_text"])]
        topic_counts[tag] = len(matched)
        print(f"  {tag:<24} n={len(matched)}")

    # Multi-topic (articles touching 3+ topics — likely the most claim-dense)
    rich_articles = []
    for a in articles:
        tags = [t for t, p in TOPIC_PATTERNS.items() if p.search(a["title"]) or p.search(a["body_text"])]
        if len(tags) >= 3:
            rich_articles.append({**a, "topic_tags": tags})

    print(f"\nClaim-dense (≥3 topic tags): {len(rich_articles)}")
    rich_articles.sort(key=lambda a: -len(a["topic_tags"]))
    print("\n=== Top 15 most claim-dense articles ===\n")
    for a in rich_articles[:15]:
        tags_s = ",".join(a["topic_tags"])
        print(f"  [{a['id']}] {a['published_date'][:10]} ({a['category']:<13}) tags={tags_s}")
        print(f"      {a['title'][:100]}")

    # Sample the speeches (highest claim density typically)
    speeches = [a for a in articles if a["category"] in ("speech", "vp_speech")]
    print(f"\n=== All speeches/vp_speeches in window (n={len(speeches)}) ===\n")
    for a in speeches:
        print(f"  [{a['id']}] {a['published_date'][:10]} ({a['category']:<10}) {a['title'][:90]}")

    # Save corpus for phase 1 input
    out = {
        "cutoff": CUTOFF,
        "total_articles": len(articles),
        "topic_counts": topic_counts,
        "articles": [{"id": a["id"], "category": a["category"], "title": a["title"],
                       "published_date": a["published_date"], "body_len": len(a["body_text"])}
                      for a in articles],
    }
    out_path = OUT / "post_feb5_inventory.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nwrote {out_path}")

    # Cost projection
    total_chars = sum(a["body_len"] for a in articles)
    approx_tokens = total_chars / 4  # rough chars→tokens
    print(f"\nTotal body characters: {total_chars:,}  (~{int(approx_tokens):,} input tokens raw)")
    # If we extract claims at ~1.5x input/0.5x output overhead per article
    proj_in = int(approx_tokens * 1.2)
    proj_out = int(approx_tokens * 0.3)
    cost = proj_in / 1e6 * 3.0 + proj_out / 1e6 * 15.0
    print(f"Projected LLM cost (Sonnet, structured extraction): ~${cost:.2f}")


if __name__ == "__main__":
    main()
