"""Phase 2: Heuristic + LLM curation of new fact-check items.

Inputs: phase1_claims.json (new claims), fact_check_master.json (existing 48 items),
        lies_raw.json, analysis.json (existing promise/delivery corpus)

Steps:
1. Group new claims by topic (housing, fiscal, infrastructure, etc.)
2. Heuristic flagging:
   - Deadlines that have expired without matching delivery
   - Numeric updates that conflict with previous numeric_updates on same subject
   - Credit claims for projects that match known previous-govt projects
3. LLM curation pass: per topic cluster, send the candidate evidence + existing
   master items in that topic, ask for new fact-check items in master schema.
4. Output: data/new_fact_checks.json (candidates) + delta report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic

DATA = Path(__file__).parent / "data"
MODEL = "claude-sonnet-4-6"
TODAY = "2026-05-18"

# Topic taxonomy with keyword patterns
TOPICS = {
    "housing": re.compile(r"\b(hous(?:e|ing)|flat|hectare|gulhifalhu|hulhumal|ras\s*mal|uthuruthilafalhu|reclamat|BML|land plot|residen)\b", re.I),
    "fiscal_debt": re.compile(r"\b(debt|deficit|budget|fiscal|sukuk|reserve|GDP|EXIM|loan|swap|austerity|MVR|USD\s*\d|\b\d+B\b|\b\d+M\b)\b", re.I),
    "infrastructure": re.compile(r"\b(airport|terminal|hospital|bridge|harbour|harbor|port|road|ferry|RTL|sewer|water|cold storage|Felivaru|Ihavandhippolhu)\b", re.I),
    "tourism": re.compile(r"\b(resort|tourism|bed[s]?|tourist|arrival)\b", re.I),
    "energy": re.compile(r"\b(MW(?:p|h)?|solar|electricity|fuel|oil|renewable|grid|power)\b", re.I),
    "diplomatic_india_china": re.compile(r"\b(india|china|EXIM|line of credit|bilateral|state visit|foreign military)\b", re.I),
    "social_education": re.compile(r"\b(school|education|student|university|teacher|mental health|Aasandha|Braille|Zakat)\b", re.I),
    "sports_youth": re.compile(r"\b(sports?|futsal|stadium|athletic|youth)\b", re.I),
    "governance_legal": re.compile(r"\b(Act No|Bill|decree|amendment|ratif|councils?|elections?|judic|court|legal)\b", re.I),
    "spokesperson_brief": re.compile(r"\b(Spokesperson|Spox|press briefing)\b", re.I),
}


def topic_for(text: str) -> list[str]:
    tags = [t for t, p in TOPICS.items() if p.search(text)]
    return tags or ["other"]


def parse_deadline(s: str | None) -> str | None:
    """Best-effort: turn 'this year' / 'within 6 months' / '2026' / 'mid-year' into an absolute date."""
    if not s:
        return None
    s = s.lower().strip()
    # Explicit year
    m = re.search(r"\b(20\d{2})\b", s)
    if m:
        return f"{m.group(1)}-12-31"
    # "this year" or "end of year"
    if "this year" in s or "end of year" in s or "end of the year" in s:
        return "2026-12-31"
    if "next year" in s:
        return "2027-12-31"
    if "mid-year" in s or "mid year" in s:
        return "2026-06-30"
    # "within X months"
    m = re.search(r"within\s+(?:the\s+next\s+)?(\d+)\s*month", s)
    if m:
        n = int(m.group(1))
        # Crude: add n months to today
        from datetime import date
        from dateutil.relativedelta import relativedelta
        try:
            d = date(2026, 5, 18) + relativedelta(months=n)
            return d.isoformat()
        except Exception:
            return None
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-llm", action="store_true", help="Skip LLM curation; just produce heuristic candidates")
    p.add_argument("--concurrency", type=int, default=4)
    args = p.parse_args()

    # Load inputs
    phase1 = json.loads((DATA / "phase1_claims.json").read_text())
    master = json.loads((DATA / "fact_check_master.json").read_text())
    print(f"Loaded: phase1={len(phase1)} articles  master={len(master)} existing fact-checks", file=sys.stderr)

    # Flatten claims with article context, tag with topics
    all_claims = []
    for art in phase1:
        for c in art.get("claims", []):
            subj = (c.get("subject") or "")
            quote = (c.get("quote") or "")
            topics = topic_for(subj + " " + quote)
            all_claims.append({
                "article_id": art["article_id"],
                "date": art["date"],
                "title": art.get("title", "")[:80],
                "category": art.get("category"),
                "topics": topics,
                **c,
            })
    print(f"Total claims: {len(all_claims)}", file=sys.stderr)

    # ---- HEURISTIC FLAGGING ----
    print("\n=== Heuristic candidates ===", file=sys.stderr)
    candidates = []

    # 1) Deadline promises with deadline now expired and no obvious delivery
    expired = []
    for c in all_claims:
        if c["type"] != "deadline_promise":
            continue
        deadline = parse_deadline(c.get("deadline")) or parse_deadline(c.get("value")) or parse_deadline(c.get("quote"))
        if deadline and deadline < TODAY:
            expired.append({**c, "_parsed_deadline": deadline})
    print(f"  expired deadlines (promise made before today, deadline now passed): {len(expired)}", file=sys.stderr)

    # 2) Numeric updates with conflicting numbers on similar subjects
    # Group by topic + subject keyword
    by_subject = defaultdict(list)
    for c in all_claims:
        if c["type"] in ("numeric_update", "numeric_promise"):
            key_words = re.findall(r"[a-zA-Z]+", (c.get("subject") or "").lower())
            key = " ".join(sorted(set(key_words)))[:80]
            by_subject[key].append(c)
    shifting = []
    for key, claims in by_subject.items():
        if len(claims) < 2:
            continue
        values = set((c.get("value") or "").strip() for c in claims if c.get("value"))
        if len(values) > 1:
            claims_sorted = sorted(claims, key=lambda c: c["date"])
            shifting.append({"subject_key": key, "claims": claims_sorted, "distinct_values": list(values)})
    print(f"  shifting numbers (same subject, different values within window): {len(shifting)}", file=sys.stderr)

    # 3) Credit claims that name infrastructure typically attributable to previous govts
    PREV_GOVT_MARKERS = re.compile(r"\b(stalled|halted|inherited|previously suspended|under (?:the )?previous (?:administration|government)|EXIM|line of credit|LoC)\b", re.I)
    credit_susp = []
    for c in all_claims:
        if c["type"] != "credit_claim":
            continue
        text = (c.get("subject") or "") + " " + (c.get("quote") or "")
        if PREV_GOVT_MARKERS.search(text):
            credit_susp.append(c)
    print(f"  credit claims with prev-govt funding markers: {len(credit_susp)}", file=sys.stderr)

    # 4) Comparisons to predecessor
    comparisons = [c for c in all_claims if c["type"] == "comparison_to_predecessor"]
    print(f"  comparisons to predecessor: {len(comparisons)}", file=sys.stderr)

    # 5) Denials
    denials = [c for c in all_claims if c["type"] == "denial"]
    print(f"  denials: {len(denials)}", file=sys.stderr)

    # 6) Boasts (often misleading superlatives)
    boasts = [c for c in all_claims if c["type"] == "boast"]
    print(f"  boasts: {len(boasts)}", file=sys.stderr)

    # Bundle by topic for LLM curation
    by_topic_claims = defaultdict(list)
    for c in all_claims:
        for t in c["topics"]:
            by_topic_claims[t].append(c)

    print("\n=== Claims by topic ===")
    for t, claims in sorted(by_topic_claims.items(), key=lambda x: -len(x[1])):
        types = defaultdict(int)
        for c in claims:
            types[c["type"]] += 1
        print(f"  {t:<28} n={len(claims):<4}  by-type={dict(sorted(types.items()))}")

    # Save heuristic candidates
    heuristic = {
        "expired_deadlines": expired,
        "shifting_numbers": shifting,
        "credit_with_prev_govt_markers": credit_susp,
        "comparisons": comparisons,
        "denials": denials,
        "boasts": boasts,
    }
    (DATA / "phase2_heuristic_candidates.json").write_text(
        json.dumps(heuristic, indent=2, ensure_ascii=False))
    print(f"\nwrote {DATA / 'phase2_heuristic_candidates.json'}")

    if args.no_llm:
        return

    # ---- LLM CURATION PASS ----
    # For each topic with substantial claim density, prepare a curation request
    print("\n=== LLM curation by topic ===", file=sys.stderr)
    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()

    # Build per-topic compact claim list and existing master subset
    def existing_in_topic(topic):
        return [m for m in master if topic_for(m.get("claim", "") + " " + m.get("what_actually_happened", ""))[0] == topic]

    def claims_compact(claims):
        # Compact representation to keep tokens down
        return [
            {
                "article_id": c["article_id"],
                "date": c["date"],
                "type": c["type"],
                "subject": c.get("subject"),
                "value": c.get("value"),
                "deadline": c.get("deadline"),
                "actor_credited": c.get("actor_credited"),
                "quote": (c.get("quote") or "")[:200],
            }
            for c in claims
        ]

    CURATION_SYSTEM = """You are curating new fact-check items for a Maldives Presidency archive.

Input:
- existing_fact_checks: items already in the master file (avoid duplicating them).
- new_claims: structured claims extracted from articles dated 2026-02-06 onward.

Today is 2026-05-18.

Output ONLY high-confidence, specific fact-check items where one of these is clearly true:
  - BROKEN DEADLINE : a stated deadline has passed without delivery (cross-reference with other claims)
  - SHIFTING NUMBERS : the same subject is given DIFFERENT numeric values across claims/dates
  - CREDIT THEFT    : a claim of credit for something that was provably started/funded by a previous government
                       (look for words: stalled, inherited, EXIM, Line of Credit, projects begun before 2023-11-17)
  - CONTRADICTION   : one claim directly contradicts another
  - MISLEADING      : framing that misrepresents reality (e.g. switching metric to hide deterioration)
  - LIE             : a definite factual claim that is demonstrably false

DO NOT include vague rhetorical assertions. DO NOT duplicate existing master items (check carefully).
Each item must cite at least one article_id and a verbatim quote.

Return JSON: {"new_items": [{
  "category": "LIE" | "MISLEADING" | "BROKEN DEADLINE" | "CREDIT THEFT" | "SHIFTING NUMBERS" | "CONTRADICTION",
  "date": "YYYY-MM-DD",
  "claim": "concise summary of the stated claim",
  "what_actually_happened": "evidence-based explanation citing article_ids/dates/quotes",
  "type": "...",
  "source_article_ids": [...],
  "evidence_quotes": ["...verbatim...", ...]
}]}

If no qualifying items in this topic, return {"new_items": []}.
"""

    def curate_topic(topic, claims, existing):
        compact = claims_compact(claims)[:120]  # cap per topic
        user = (
            f"Topic: {topic}\n\n"
            f"EXISTING master fact-checks in this topic ({len(existing)}):\n"
            f"{json.dumps(existing, ensure_ascii=False)[:8000]}\n\n"
            f"NEW claims to evaluate ({len(compact)}):\n"
            f"{json.dumps(compact, ensure_ascii=False)[:30000]}\n\n"
            f"Return the JSON object now."
        )
        for attempt in range(3):
            try:
                r = client.messages.create(
                    model=MODEL, max_tokens=4000, system=CURATION_SYSTEM,
                    messages=[{"role": "user", "content": user}],
                )
                text = r.content[0].text
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if not m:
                    return {"topic": topic, "new_items": [], "_raw": text[:300]}
                d = json.loads(m.group(0))
                return {"topic": topic, "new_items": d.get("new_items", []),
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            except anthropic.RateLimitError:
                time.sleep(2 ** attempt * 2)
            except Exception as e:
                if attempt == 2:
                    return {"topic": topic, "new_items": [], "_error": str(e)[:200]}
                time.sleep(2 ** attempt)

    # Curate topics with >= 5 claims; skip "other" (too generic)
    topics_to_run = [t for t in by_topic_claims if t != "other" and len(by_topic_claims[t]) >= 5]
    print(f"  topics for LLM curation: {topics_to_run}", file=sys.stderr)

    results = []
    cost_in = cost_out = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(curate_topic, t, by_topic_claims[t], existing_in_topic(t)): t
                   for t in topics_to_run}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            cost_in += res.get("_in") or 0
            cost_out += res.get("_out") or 0
            print(f"  [{res['topic']:<26}] new_items={len(res['new_items'])}  in={res.get('_in')} out={res.get('_out')}",
                  flush=True)

    all_new = []
    for res in results:
        for item in res["new_items"]:
            item["_topic"] = res["topic"]
            all_new.append(item)

    cost = cost_in / 1e6 * 3.0 + cost_out / 1e6 * 15.0
    print(f"\nLLM curation total: {len(all_new)} new items proposed across {len(results)} topics", file=sys.stderr)
    print(f"Tokens in={cost_in} out={cost_out}  cost=${cost:.2f}", file=sys.stderr)

    (DATA / "new_fact_checks.json").write_text(json.dumps(all_new, indent=2, ensure_ascii=False))
    print(f"wrote {DATA / 'new_fact_checks.json'}")

    # Summarise by category
    cat_counts = defaultdict(int)
    for item in all_new:
        cat_counts[item.get("category", "?")] += 1
    print("\n=== New fact-check items by category ===")
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:<22} n={n}")


if __name__ == "__main__":
    main()
