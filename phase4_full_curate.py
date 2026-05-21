"""Phase 4: Full Muizzu-administration cross-time fact-check curation.

Inputs:
  data/phase3_full_claims.json   - 8.9k claims across 3.1k articles
  data/fact_check_master.json    - 48 existing curated items
  data/lies_raw.json             - 65 known previous-govt (Solih) projects

Heuristics (free, deterministic):
  1. Expired deadline detection — deadline_promise with parseable deadline
     before today AND no matching delivery in subsequent articles.
  2. Shifting numbers — claims clustered by subject (keyword sets), flag
     groups with multiple distinct values across time.
  3. Credit-theft markers — credit_claim text against Solih-project subject
     keywords.
  4. Boast verification flags — superlatives that are concrete enough to check.

LLM curation pass:
  - Chunk by topic AND by time period for the larger topics.
  - Each chunk gets relevant existing-master items as anti-duplication context.
  - Output master-schema items.

Output: data/full_new_fact_checks.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import anthropic

DATA = Path(__file__).parent / "data"
MODEL = "claude-sonnet-4-6"
TODAY = "2026-05-18"
TODAY_DT = date(2026, 5, 18)

# Topic taxonomy — reuse from phase2 but expanded
TOPICS = {
    "housing": re.compile(r"\b(hous(?:e|ing)|flat|hectare|gulhifalhu|hulhumal|ras\s*mal|uthuruthilafalhu|reclamat|BML|land plot|Hiya|residen|Affordable)\b", re.I),
    "fiscal_debt": re.compile(r"\b(debt|deficit|budget|fiscal|sukuk|reserve|GDP|EXIM|loan|swap|austerity|MVR|USD\s*\d|currency)\b", re.I),
    "infrastructure": re.compile(r"\b(airport|terminal|hospital|bridge|harbour|harbor|port|road|ferry|RTL|sewer|water|cold storage|Felivaru|Ihavandhippolhu|Dharumavantha)\b", re.I),
    "tourism": re.compile(r"\b(resort|tourism|bed[s]?|tourist|arrival)\b", re.I),
    "energy": re.compile(r"\b(MW(?:p|h)?|solar|electricity|fuel|oil|renewable|grid|power|net-zero)\b", re.I),
    "diplomatic_india_china": re.compile(r"\b(india|china|EXIM|line of credit|bilateral|state visit|foreign military|UNGA|ICJ)\b", re.I),
    "social_education": re.compile(r"\b(school|education|student|university|teacher|mental health|Aasandha|Braille|Zakat|medical)\b", re.I),
    "sports_youth": re.compile(r"\b(sports?|futsal|stadium|athletic|youth|football)\b", re.I),
    "governance_legal": re.compile(r"\b(Act No|Bill|decree|amendment|ratif|councils?|elections?|judic|court|legal|referendum|terror)\b", re.I),
    "fisheries": re.compile(r"\b(fisher|tuna|MIFCO|fishing|fleet)\b", re.I),
    "spokesperson_brief": re.compile(r"\b(Spokesperson|Spox|press briefing)\b", re.I),
}

# Subject keyword sets for shifting-number clustering — manually curated for known
# high-yield topics that have appeared in previous fact-check items.
SUBJECT_KEYWORDS = {
    "ras_male_hectares": ["ras", "mal", "eco", "city", "hectares"],
    "uthuruthilafalhu_hectares": ["uthuru", "thilafalhu", "hectares"],
    "gulhifalhu_hectares": ["gulhifalhu", "hectares"],
    "hulhumale_phase3": ["hulhumal", "phase", "3", "iii"],
    "housing_units_total": ["housing", "units"],
    "felivaru_cold_storage": ["felivaru", "cold", "storage"],
    "sukuk_amount": ["sukuk"],
    "reserves_amount": ["reserves", "gross"],
    "deficit_value": ["deficit", "budget"],
    "debt_total": ["debt"],
    "bml_housing_units": ["bml", "housing"],
    "indian_loc": ["LoC", "line", "credit", "EXIM"],
    "currency_swap": ["currency", "swap"],
    "sports_projects_count": ["sports", "projects"],
    "stalled_projects": ["stalled", "projects", "revived"],
    "tourism_beds": ["beds", "resort", "tourist"],
    "airports_count": ["airport", "domestic"],
    "education_schools": ["schools", "education"],
    "tertiary_hospital_male": ["tertiary", "hospital", "vilimale", "malé", "male"],
    "mental_health_hospital": ["mental", "health", "hospital"],
    "development_bank": ["development", "bank"],
    "bunkering_port": ["bunkering", "port"],
    "media_village": ["media", "village"],
    "addu_bridge": ["addu", "bridge"],
    "rtl_ferry": ["rtl", "ferry"],
}


def topic_for(text: str) -> list[str]:
    tags = [t for t, p in TOPICS.items() if p.search(text)]
    return tags or ["other"]


def parse_deadline(claim: dict) -> str | None:
    """Best-effort date parsing from deadline/value/quote fields."""
    sources = []
    for k in ("deadline", "value", "quote"):
        v = claim.get(k)
        if v:
            sources.append(str(v))
    s = " ".join(sources).lower()
    if not s:
        return None
    # Year and explicit dates
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Specific month + year
    months = "(january|february|march|april|may|june|july|august|september|october|november|december)"
    m = re.search(rf"{months}\s+(20\d{{2}})", s)
    if m:
        month_num = ["january","february","march","april","may","june","july","august","september","october","november","december"].index(m.group(1).lower()) + 1
        return f"{m.group(2)}-{month_num:02d}-28"
    # Year alone
    m = re.search(r"\b(20\d{2})\b", s)
    if m:
        return f"{m.group(1)}-12-31"
    # Relative
    if "this year" in s and claim.get("date"):
        year = claim["date"][:4]
        return f"{year}-12-31"
    if "next year" in s and claim.get("date"):
        year = int(claim["date"][:4]) + 1
        return f"{year}-12-31"
    if "end of year" in s or "end of the year" in s:
        if claim.get("date"):
            return f"{claim['date'][:4]}-12-31"
    m = re.search(r"within\s+(?:the\s+next\s+)?(\d+)\s*month", s)
    if m and claim.get("date"):
        try:
            from datetime import date as date_
            from dateutil.relativedelta import relativedelta
            d = date_.fromisoformat(claim["date"][:10]) + relativedelta(months=int(m.group(1)))
            return d.isoformat()
        except Exception:
            return None
    m = re.search(r"within\s+(\d+)\s*year", s)
    if m and claim.get("date"):
        year = int(claim["date"][:4]) + int(m.group(1))
        return f"{year}-12-31"
    return None


def normalize_subject_key(subject: str, quote: str) -> list[str]:
    """Return list of subject_keyword cluster names this claim might belong to."""
    text = (subject or "") + " " + (quote or "")
    words = set(re.findall(r"[a-zA-Z]+", text.lower()))
    matches = []
    for cluster, keywords in SUBJECT_KEYWORDS.items():
        kw_lower = [k.lower() for k in keywords]
        # Require >=60% of keywords present
        present = sum(1 for k in kw_lower if any(k in w for w in words))
        if present >= max(2, len(kw_lower) * 0.6):
            matches.append(cluster)
    return matches


def build_solih_index(lies_raw: dict) -> list[dict]:
    """Build list of {keywords, original_text} from Solih-era project mentions."""
    items = []
    for entry in lies_raw.get("solih_projects", []):
        text = entry.get("text") or ""
        words = set(w.lower() for w in re.findall(r"[a-zA-Z]{4,}", text))
        items.append({"text": text, "date": entry.get("date"), "words": words})
    return items


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-chunk-claims", type=int, default=200)
    args = p.parse_args()

    phase3 = json.loads((DATA / "phase3_full_claims.json").read_text())
    master = json.loads((DATA / "fact_check_master.json").read_text())
    lies_raw = json.loads((DATA / "lies_raw.json").read_text())
    print(f"Loaded: {len(phase3)} articles, {len(master)} existing fact-checks, "
          f"{len(lies_raw.get('solih_projects', []))} Solih project mentions", file=sys.stderr)

    # Flatten claims with article context
    all_claims = []
    for art in phase3:
        for c in art.get("claims", []):
            subj = (c.get("subject") or "")
            quote = (c.get("quote") or "")
            topics = topic_for(subj + " " + quote)
            subj_clusters = normalize_subject_key(subj, quote)
            all_claims.append({
                "article_id": art["article_id"],
                "date": art["date"],
                "title": (art.get("title") or "")[:80],
                "category": art.get("category"),
                "topics": topics,
                "subj_clusters": subj_clusters,
                **c,
            })
    print(f"Total claims: {len(all_claims)}", file=sys.stderr)

    # ---- HEURISTIC FLAGGING ----
    print("\n=== Heuristic candidates ===", file=sys.stderr)

    # 1) Expired deadlines without confirmed delivery
    expired = []
    for c in all_claims:
        if c["type"] != "deadline_promise":
            continue
        deadline = parse_deadline(c)
        if deadline and deadline < TODAY:
            expired.append({**c, "_parsed_deadline": deadline})
    print(f"  expired deadlines (parsed and now past): {len(expired)}", file=sys.stderr)

    # 2) Shifting numbers across time, by subject cluster
    by_cluster = defaultdict(list)
    for c in all_claims:
        if c["type"] in ("numeric_update", "numeric_promise"):
            for cl in c["subj_clusters"]:
                by_cluster[cl].append(c)
    shifting = []
    for cl, claims in by_cluster.items():
        if len(claims) < 2:
            continue
        values = set((c.get("value") or "").strip() for c in claims if c.get("value"))
        if len(values) > 1:
            claims_sorted = sorted(claims, key=lambda c: c["date"])
            shifting.append({
                "subject_cluster": cl,
                "n_claims": len(claims),
                "distinct_values": list(values),
                "claims": claims_sorted,
            })
    print(f"  shifting numbers (subject clusters with >1 distinct value): {len(shifting)}", file=sys.stderr)

    # 3) Credit claims matching Solih project mentions
    solih_index = build_solih_index(lies_raw)
    cred_susp = []
    for c in all_claims:
        if c["type"] != "credit_claim":
            continue
        text = ((c.get("subject") or "") + " " + (c.get("quote") or "")).lower()
        words = set(re.findall(r"[a-zA-Z]{5,}", text))
        if not words:
            continue
        for s in solih_index:
            overlap = words & s["words"]
            # Substantial overlap: at least 3 content words
            if len(overlap) >= 3:
                cred_susp.append({**c, "_solih_match_text": s["text"][:150],
                                  "_overlap_words": list(overlap)[:10]})
                break
    print(f"  credit_claims overlapping Solih-era projects: {len(cred_susp)}", file=sys.stderr)

    # 4) Comparisons + denials + boasts (for review)
    comparisons = [c for c in all_claims if c["type"] == "comparison_to_predecessor"]
    denials = [c for c in all_claims if c["type"] == "denial"]
    boasts = [c for c in all_claims if c["type"] == "boast"]
    print(f"  comparisons={len(comparisons)}  denials={len(denials)}  boasts={len(boasts)}", file=sys.stderr)

    heuristic = {
        "expired_deadlines": expired,
        "shifting_numbers": shifting,
        "credit_with_solih_overlap": cred_susp,
        "comparisons": comparisons,
        "denials": denials,
        "boasts": boasts,
    }
    (DATA / "phase4_heuristic_candidates.json").write_text(
        json.dumps(heuristic, indent=2, ensure_ascii=False))
    print(f"\nwrote {DATA / 'phase4_heuristic_candidates.json'}")

    # Tally by topic
    by_topic = defaultdict(list)
    for c in all_claims:
        for t in c["topics"]:
            by_topic[t].append(c)
    print("\n=== Claims by topic ===")
    for t, claims in sorted(by_topic.items(), key=lambda x: -len(x[1])):
        types = Counter(c["type"] for c in claims)
        print(f"  {t:<28} n={len(claims):<5}  by-type={dict(sorted(types.items()))}")

    if args.no_llm:
        return

    # ---- LLM CURATION PASS, chunked by topic AND time window ----
    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()

    CURATION_SYSTEM = """You are curating new fact-check items for a Maldives Presidency archive.

Inputs:
- existing_fact_checks: items ALREADY in the master file (do NOT duplicate).
- heuristic_flags: pre-flagged candidates for this topic (expired deadlines, shifting numbers, credit theft).
- new_claims: structured claims to evaluate.

Today is 2026-05-18. Muizzu admin began 2023-11-17. Previous (Solih) admin: 2018-11-17 to 2023-11-17.

Output ONLY high-confidence, specific fact-check items where one of these is clearly true:
  - BROKEN DEADLINE : a specific deadline has passed without delivery (cross-reference with other claims to confirm no delivery)
  - SHIFTING NUMBERS : same subject given DIFFERENT numeric values across dates
  - CREDIT THEFT    : claim of credit for something started/funded by previous government (look for: "stalled", "inherited",
                       "EXIM", "Line of Credit", "previously suspended", projects predating 2023-11-17, or projects matching
                       known Solih-era project list)
  - CONTRADICTION   : one claim directly contradicts another
  - MISLEADING      : framing that misrepresents reality (metric switching, selective comparison, hidden basis change)
  - LIE             : a definite factual claim that is demonstrably false

Be CONSERVATIVE. DO NOT include vague rhetoric. DO NOT duplicate existing master items (check carefully — same date/topic/claim).
Each item MUST cite at least one article_id and a verbatim quote.

Return STRICT JSON: {"new_items": [{
  "category": "LIE" | "MISLEADING" | "BROKEN DEADLINE" | "CREDIT THEFT" | "SHIFTING NUMBERS" | "CONTRADICTION",
  "date": "YYYY-MM-DD",
  "claim": "concise summary of the stated claim (<=200 chars)",
  "what_actually_happened": "evidence-based explanation citing article_ids/dates/quotes",
  "type": "type tag matching master schema",
  "source_article_ids": [int, ...],
  "evidence_quotes": ["...verbatim...", ...]
}]}

If no qualifying items in this chunk, return {"new_items": []}.
"""

    def existing_in_topic(topic):
        return [m for m in master if topic_for((m.get("claim") or "") + " " + (m.get("what_actually_happened") or ""))[0] == topic]

    def claims_compact(claims):
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

    def heuristics_for_topic(topic):
        """Pick the heuristic items most relevant to this topic."""
        relevant_expired = [c for c in expired if topic in topic_for((c.get("subject") or "") + " " + (c.get("quote") or ""))]
        relevant_shifts = [s for s in shifting if any(topic in topic_for((c.get("subject") or "") + " " + (c.get("quote") or "")) for c in s["claims"])]
        relevant_solih = [c for c in cred_susp if topic in topic_for((c.get("subject") or "") + " " + (c.get("quote") or ""))]
        return {
            "expired_deadlines": relevant_expired[:30],
            "shifting_numbers": relevant_shifts[:20],
            "credit_theft_candidates": relevant_solih[:20],
        }

    def chunk_topic(topic, claims):
        """Split a topic's claims into manageable chunks by time window."""
        # Sort by date and split into chunks
        claims_sorted = sorted(claims, key=lambda c: c["date"])
        chunks = []
        current = []
        for c in claims_sorted:
            current.append(c)
            if len(current) >= args.max_chunk_claims:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)
        return chunks

    def curate(topic, chunk_idx, chunk_claims, existing, heur_flags):
        compact = claims_compact(chunk_claims)
        compact_heur = {k: claims_compact(v) if isinstance(v, list) and v and isinstance(v[0], dict) and "claims" not in v[0]
                        else v for k, v in heur_flags.items()}
        # For shifting numbers, simplify
        if "shifting_numbers" in compact_heur:
            compact_heur["shifting_numbers"] = [
                {
                    "subject_cluster": s["subject_cluster"],
                    "distinct_values": s["distinct_values"],
                    "claims": claims_compact(s["claims"])[:20],
                }
                for s in heur_flags["shifting_numbers"]
            ]

        user = (
            f"Topic: {topic} (chunk {chunk_idx + 1})\n"
            f"Date range: {chunk_claims[0]['date']} → {chunk_claims[-1]['date']}\n\n"
            f"EXISTING master fact-checks in this topic ({len(existing)}):\n"
            f"{json.dumps(existing, ensure_ascii=False)[:10000]}\n\n"
            f"HEURISTIC FLAGS (auto-detected candidates for this topic):\n"
            f"{json.dumps(compact_heur, ensure_ascii=False)[:15000]}\n\n"
            f"NEW claims in this chunk ({len(compact)}):\n"
            f"{json.dumps(compact, ensure_ascii=False)[:30000]}\n\n"
            f"Return the JSON object now."
        )
        for attempt in range(3):
            try:
                r = client.messages.create(
                    model=MODEL, max_tokens=6000, system=CURATION_SYSTEM,
                    messages=[{"role": "user", "content": user}],
                )
                text = r.content[0].text
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if not m:
                    return {"topic": topic, "chunk": chunk_idx, "new_items": [], "_raw": text[:300]}
                d = json.loads(m.group(0))
                return {"topic": topic, "chunk": chunk_idx, "new_items": d.get("new_items", []),
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            except anthropic.RateLimitError:
                time.sleep(2 ** attempt * 2)
            except Exception as e:
                if attempt == 2:
                    return {"topic": topic, "chunk": chunk_idx, "new_items": [], "_error": str(e)[:200]}
                time.sleep(2 ** attempt)

    # Build curation tasks
    tasks = []
    for topic in by_topic:
        if topic == "other":
            continue
        if len(by_topic[topic]) < 5:
            continue
        chunks = chunk_topic(topic, by_topic[topic])
        for i, chunk in enumerate(chunks):
            tasks.append((topic, i, chunk))
    print(f"\n=== LLM curation: {len(tasks)} chunks across {len({t[0] for t in tasks})} topics ===", file=sys.stderr)

    results = []
    cost_in = cost_out = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(curate, t, i, c, existing_in_topic(t), heuristics_for_topic(t)): (t, i)
                   for t, i, c in tasks}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            cost_in += res.get("_in") or 0
            cost_out += res.get("_out") or 0
            print(f"  [{res['topic']:<26} ch{res['chunk']}] new={len(res['new_items'])} "
                  f"in={res.get('_in')} out={res.get('_out')}", flush=True)

    all_new = []
    for res in results:
        for item in res["new_items"]:
            item["_topic"] = res["topic"]
            item["_chunk"] = res["chunk"]
            all_new.append(item)

    cost = cost_in / 1e6 * 3.0 + cost_out / 1e6 * 15.0
    print(f"\nLLM curation total: {len(all_new)} items across {len(results)} chunks", file=sys.stderr)
    print(f"Tokens in={cost_in} out={cost_out}  cost=${cost:.2f}", file=sys.stderr)

    (DATA / "full_new_fact_checks.json").write_text(json.dumps(all_new, indent=2, ensure_ascii=False))
    print(f"wrote {DATA / 'full_new_fact_checks.json'}")

    cat_counts = Counter(item.get("category", "?") for item in all_new)
    print("\n=== Full-corpus new fact-check items by category ===")
    for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:<22} n={n}")


if __name__ == "__main__":
    main()
