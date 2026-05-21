"""Phase 1: extract structured claims from each post-2026-02-05 article using Claude.

Output schema per article:
{
  "article_id": int,
  "date": "YYYY-MM-DD",
  "claims": [
    {
      "type": "numeric_promise" | "deadline_promise" | "credit_claim" | "numeric_update" |
              "policy_assertion" | "denial" | "boast" | "comparison_to_predecessor",
      "subject": "what the claim is about (e.g. 'Uthuruthilafalhu reclamation')",
      "value": "specific number/quantity/qualifier or null",
      "deadline": "stated deadline if any (e.g. 'this year', '2026-12') or null",
      "actor_credited": "Muizzu admin | previous govt | unattributed | other (string)",
      "quote": "verbatim snippet from article (<=200 chars)"
    }
  ]
}

Only claims that are SPECIFIC and CHECKABLE. Skip pure rhetoric like 'committed to development'.

Usage:
  python phase1_extract.py --sample 5      # test on 5 articles, print results
  python phase1_extract.py                  # full run (212 articles)
"""
from __future__ import annotations

import argparse
import json
import os
import random
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
CUTOFF = "2026-02-06"
MAX_BODY_CHARS = 4000  # speeches can be long; truncate

SYSTEM = """You are a forensic fact-extraction analyst working on Maldives Presidency press releases.

For each article, extract SPECIFIC, CHECKABLE claims that could later be verified, contradicted, or compared. Skip pure rhetoric.

Claim types:
- "numeric_promise"        : a number+subject the govt commits to (e.g. "12,940 housing units this year")
- "deadline_promise"       : something promised by a specific date or timeframe (e.g. "completed by end of 2026")
- "numeric_update"         : reporting a current status number (e.g. "reserves at record high", "92 projects completed")
- "credit_claim"           : taking credit for delivering / inaugurating / completing something
- "policy_assertion"       : a definite factual claim about state of policy/economy/diplomacy (e.g. "debt is at 119B MVR")
- "denial"                 : explicit denial / refutation of an allegation
- "boast"                  : superlative comparison ("first time in 5 years", "largest ever", "lowest since...")
- "comparison_to_predecessor" : framing about what previous govt did or didn't do

For each claim include:
  "type", "subject", "value" (string or null), "deadline" (string or null),
  "actor_credited" (string: "Muizzu admin" | "previous govt" | "unattributed" | other name),
  "quote" (verbatim snippet, <=200 chars)

Return STRICT JSON: an object with a "claims" array. Empty array if no specific claims.

Be conservative — vague aspirations like "committed to economic prosperity" are NOT claims; skip them.
Be liberal on numbers — every specific number with a subject is a claim worth recording."""

USER_TEMPLATE = """Article ID: {id}
Date: {date}
Category: {category}
Title: {title}

Body:
\"\"\"
{body}
\"\"\"

Extract claims as JSON: {{"claims": [...]}}.
Return ONLY the JSON object."""

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def load_window(cutoff: str):
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, category, title, body_text, published_date
           FROM articles
           WHERE language='EN' AND published_date >= ?
             AND category IN ('press_release','speech','vp_speech')
             AND body_text IS NOT NULL AND body_text != ''
           ORDER BY published_date, id""",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def trim_body(text: str, limit: int = MAX_BODY_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_period = cut.rfind(". ")
    return cut[: last_period + 1] if last_period > limit * 0.7 else cut


def extract_one(client, article, retries=3):
    body = trim_body(article["body_text"])
    user = USER_TEMPLATE.format(
        id=article["id"],
        date=article["published_date"][:10],
        category=article["category"],
        title=article["title"],
        body=body,
    )
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = r.content[0].text
            m = JSON_RE.search(text)
            if not m:
                return {"article_id": article["id"], "date": article["published_date"][:10],
                        "claims": [], "_parse_error": True, "_raw": text[:300]}
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
    p.add_argument("--sample", type=int, default=0, help="Test on N random articles instead of full run")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--out", default=str(OUT / "phase1_claims.json"))
    args = p.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()

    articles = load_window(CUTOFF)
    print(f"Window: {len(articles)} articles (>= {CUTOFF})", file=sys.stderr)

    if args.sample > 0:
        rng = random.Random(42)
        # Mix of dense and random
        articles = sorted(articles, key=lambda a: -len(a["body_text"]))[:args.sample * 2]
        articles = rng.sample(articles, k=min(args.sample, len(articles)))
        print(f"Sample mode: {len(articles)} articles", file=sys.stderr)

    results = [None] * len(articles)
    cost_in = cost_out = 0
    completed = 0
    start = time.time()

    def worker(idx):
        return idx, extract_one(client, articles[idx])

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(worker, i) for i in range(len(articles))]
        for fut in as_completed(futures):
            idx, r = fut.result()
            results[idx] = r
            completed += 1
            cost_in += r.get("_in_tokens") or 0
            cost_out += r.get("_out_tokens") or 0
            if completed % 10 == 0 or completed == len(articles) or args.sample > 0:
                elapsed = time.time() - start
                rate = completed / elapsed
                eta = (len(articles) - completed) / rate if rate > 0 else 0
                print(f"  {completed}/{len(articles)} rate={rate:.1f}/s eta={eta:.0f}s "
                      f"tokens_in={cost_in} out={cost_out}", flush=True)

    # Summary
    n_claims = sum(len(r["claims"]) for r in results if r)
    n_errors = sum(1 for r in results if r.get("_error") or r.get("_parse_error"))
    print(f"\nTotal claims extracted: {n_claims}  ({n_errors} articles with errors)")

    type_counts = {}
    for r in results:
        for c in r.get("claims", []):
            type_counts[c.get("type", "?")] = type_counts.get(c.get("type", "?"), 0) + 1
    print(f"By type: {type_counts}")

    cost = cost_in / 1e6 * 3.0 + cost_out / 1e6 * 15.0
    print(f"Tokens: in={cost_in} out={cost_out}  cost=${cost:.3f}")

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"wrote {args.out}")

    if args.sample > 0:
        # Print a few example claims
        print("\n=== Sample extracted claims ===")
        for r in results[:3]:
            print(f"\n[{r['article_id']}] {r['date']} — {r.get('title','')[:80]}")
            for c in r.get("claims", [])[:5]:
                print(f"  - [{c.get('type')}] subj={c.get('subject')!r} val={c.get('value')!r} "
                      f"deadline={c.get('deadline')!r} actor={c.get('actor_credited')!r}")
                if c.get("quote"):
                    print(f"      \"{c['quote'][:120]}\"")


if __name__ == "__main__":
    main()
