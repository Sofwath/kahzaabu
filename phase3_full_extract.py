"""Phase 3: full Muizzu-administration claim extraction.

Processes 2023-11-17 → 2026-02-05 (the window NOT already covered by phase1_claims.json).
Reuses the same schema/prompt as phase 1 for uniformity.

Outputs:
  data/phase3_pre_feb5_claims.json  - new claims from the pre-Feb 5 window
  data/phase3_full_claims.json      - merged full Muizzu-era claim set
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

DB = Path(__file__).parent / "data" / "kahzaabu.db"
OUT = Path(__file__).parent / "data"
MODEL = "claude-sonnet-4-6"
START = "2023-11-17"
END_EXCL = "2026-02-06"  # phase 1 covers >=2026-02-06

# Truncation: longer for speeches (Presidential Addresses run 8-12k chars)
TRUNC_PR = 4000
TRUNC_SPEECH = 8000

# Re-use phase 1 prompt verbatim for schema consistency
sys.path.insert(0, str(Path(__file__).parent))
from phase1_extract import SYSTEM, USER_TEMPLATE, JSON_RE  # noqa: E402


def trim_body(text: str, category: str) -> str:
    limit = TRUNC_SPEECH if category in ("speech", "vp_speech") else TRUNC_PR
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_period = cut.rfind(". ")
    return cut[: last_period + 1] if last_period > limit * 0.7 else cut


def load_window():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, category, title, body_text, published_date
           FROM articles
           WHERE language='EN' AND published_date >= ? AND published_date < ?
             AND category IN ('press_release','speech','vp_speech')
             AND body_text IS NOT NULL AND body_text != ''
           ORDER BY published_date, id""",
        (START, END_EXCL),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def extract_one(client, article, retries=3):
    body = trim_body(article["body_text"], article["category"])
    user = USER_TEMPLATE.format(
        id=article["id"], date=article["published_date"][:10],
        category=article["category"], title=article["title"], body=body,
    )
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=2500, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = r.content[0].text
            m = JSON_RE.search(text)
            if not m:
                return {"article_id": article["id"], "date": article["published_date"][:10],
                        "claims": [], "_parse_error": True}
            d = json.loads(m.group(0))
            return {"article_id": article["id"], "date": article["published_date"][:10],
                    "title": article["title"], "category": article["category"],
                    "claims": d.get("claims", []),
                    "_in_tokens": r.usage.input_tokens, "_out_tokens": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"article_id": article["id"], "date": article["published_date"][:10],
                        "claims": [], "_error": str(e)[:200]}
            time.sleep(2 ** attempt)
    return {"article_id": article["id"], "claims": [], "_error": "exhausted retries"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="Cap to N articles (testing)")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--checkpoint-every", type=int, default=100)
    args = p.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()

    articles = load_window()
    print(f"Pre-Feb-5 window: {len(articles)} articles", file=sys.stderr)
    if args.limit:
        articles = articles[: args.limit]

    # Resume-capable: load existing checkpoint if present
    checkpoint_path = OUT / "phase3_pre_feb5_claims.json"
    done_ids = set()
    existing = []
    if checkpoint_path.exists():
        existing = json.loads(checkpoint_path.read_text())
        done_ids = {r["article_id"] for r in existing}
        print(f"  resuming: {len(done_ids)} already done", file=sys.stderr)

    todo = [a for a in articles if a["id"] not in done_ids]
    print(f"  to process: {len(todo)}", file=sys.stderr)

    results = list(existing)
    cost_in = sum(r.get("_in_tokens") or 0 for r in existing)
    cost_out = sum(r.get("_out_tokens") or 0 for r in existing)
    completed = 0
    start_t = time.time()

    def worker(idx):
        return idx, extract_one(client, todo[idx])

    def checkpoint():
        checkpoint_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(worker, i) for i in range(len(todo))]
        for fut in as_completed(futures):
            idx, r = fut.result()
            results.append(r)
            completed += 1
            cost_in += r.get("_in_tokens") or 0
            cost_out += r.get("_out_tokens") or 0
            if completed % 25 == 0 or completed == len(todo):
                elapsed = time.time() - start_t
                rate = completed / elapsed
                eta = (len(todo) - completed) / rate if rate > 0 else 0
                cost = cost_in / 1e6 * 3.0 + cost_out / 1e6 * 15.0
                print(f"  {completed}/{len(todo)} rate={rate:.1f}/s eta={eta:.0f}s "
                      f"tokens_in={cost_in} out={cost_out} cost=${cost:.2f}", flush=True)
            if completed % args.checkpoint_every == 0:
                checkpoint()

    checkpoint()
    print(f"\nwrote {checkpoint_path}")

    # Build merged full corpus
    phase1 = json.loads((OUT / "phase1_claims.json").read_text())
    merged = list(results) + list(phase1)
    merged.sort(key=lambda r: (r.get("date") or "", r.get("article_id") or 0))
    (OUT / "phase3_full_claims.json").write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    total_claims = sum(len(r.get("claims", [])) for r in merged)
    print(f"wrote {OUT / 'phase3_full_claims.json'}  ({len(merged)} articles, {total_claims} claims)")

    type_counts = {}
    for r in merged:
        for c in r.get("claims", []):
            type_counts[c.get("type", "?")] = type_counts.get(c.get("type", "?"), 0) + 1
    print(f"By type: {dict(sorted(type_counts.items(), key=lambda x: -x[1]))}")

    cost = cost_in / 1e6 * 3.0 + cost_out / 1e6 * 15.0
    print(f"This run tokens: in={cost_in} out={cost_out}  cost=${cost:.2f}")


if __name__ == "__main__":
    main()
