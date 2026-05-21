"""LLM-pairwise stylometric analysis of 2026 EN press releases.

Step 1 (this run by default): controls only.
  - Positive controls: same article, first half vs second half. Model should score HIGH.
  - Negative controls: 2026 PO article vs old (2009) PO press release. Different era,
    high likelihood of different writers. Model should score LOWER than positives.
  - Negative controls 2: 2026 PO article vs a synthetic clearly-different style passage.

If the gap between positive-mean and negative-mean is wide enough, we run the full batch.
Run with --full to skip controls and execute the 40-doc x 780-pair batch.
"""
from __future__ import annotations

import argparse
import itertools
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
import numpy as np
import pandas as pd

DB_PATH = Path(__file__).parent / "data" / "kahzaabu.db"
OUT_DIR = Path(__file__).parent / "data"
MODEL = "claude-sonnet-4-6"
MAX_SNIPPET_CHARS = 1800   # plenty of style signal; keeps tokens modest

# Synthetic "clearly not a Maldivian govt PR writer" sample for harsh negative control.
# A passage from a 19th-century literary work — different register, different era, different person.
SYNTHETIC_NEG = (
    "It is a truth universally acknowledged, that a single man in possession of a "
    "good fortune, must be in want of a wife. However little known the feelings or "
    "views of such a man may be on his first entering a neighbourhood, this truth "
    "is so well fixed in the minds of the surrounding families, that he is considered "
    "as the rightful property of some one or other of their daughters. 'My dear Mr. "
    "Bennet,' said his lady to him one day, 'have you heard that Netherfield Park is "
    "let at last?' Mr. Bennet replied that he had not. 'But it is,' returned she; "
    "'for Mrs. Long has just been here, and she told me all about it.'"
)


def load_2026_long() -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, body_text, published_date
           FROM articles
           WHERE category='press_release' AND language='EN'
             AND published_date LIKE '2026-%'
             AND body_text IS NOT NULL AND LENGTH(body_text) >= 1500
           ORDER BY published_date, id"""
    ).fetchall()
    conn.close()
    return pd.DataFrame([dict(r) for r in rows])


def load_old_press() -> pd.DataFrame:
    """Pre-2015 press releases as negative-control source: different era, different writers."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, body_text, published_date
           FROM articles
           WHERE category='press_release' AND language='EN'
             AND SUBSTR(published_date,1,4) IN ('2011','2012','2013','2014')
             AND body_text IS NOT NULL AND LENGTH(body_text) >= 1500
           ORDER BY RANDOM() LIMIT 50"""
    ).fetchall()
    conn.close()
    return pd.DataFrame([dict(r) for r in rows])


def snippet(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    # Take first ~limit chars, cutting cleanly at a sentence boundary if possible
    cut = text[:limit]
    last_period = cut.rfind(". ")
    if last_period > limit * 0.7:
        return cut[: last_period + 1]
    return cut


JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_judgment(text: str) -> dict:
    m = JSON_RE.search(text)
    if not m:
        return {"prob_same": None, "reason": text[:200], "_parse_error": True}
    try:
        d = json.loads(m.group(0))
        if "prob_same" in d:
            d["prob_same"] = float(d["prob_same"])
        return d
    except Exception as e:
        return {"prob_same": None, "reason": text[:200], "_parse_error": True, "_err": str(e)}


SYSTEM_PROMPT = """You are a stylometric judge. Given two short text passages, decide whether they were most likely written by the SAME author or by DIFFERENT authors.

IGNORE topic, subject matter, names, and dates. Focus ONLY on stylistic markers:
- Sentence rhythm and length variation
- Function-word habits (e.g. preference for "however" vs "but", "moreover" vs "also")
- Punctuation habits (comma frequency, semicolons, em-dashes, parenthetical use)
- Lexical preferences (e.g. "noted" vs "said" vs "remarked"; "emphasised" vs "emphasized" spelling)
- Attribution patterns (how quotes/statements are introduced)
- Connective tissue between clauses
- Idiosyncrasies that suggest a particular author

Government press releases share an institutional register, so you must look hard for SUBTLE individual fingerprints, not the shared house style.

Output strictly a JSON object:
{
  "prob_same": 0.0-1.0,   // probability both passages are by the same author
  "reason": "..."         // one sentence citing specific style features that pushed the score
}

Calibration:
- 0.9+ : strong evidence (very specific shared idiosyncrasies)
- 0.6-0.8 : leaning same (multiple consistent markers, no strong contradictions)
- 0.4-0.6 : genuinely ambiguous
- 0.2-0.4 : leaning different
- < 0.2 : strong evidence of different authors
"""

USER_TEMPLATE = """Passage A:
\"\"\"
{a}
\"\"\"

Passage B:
\"\"\"
{b}
\"\"\"

Return only the JSON object."""


def judge_pair(client: anthropic.Anthropic, a: str, b: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": USER_TEMPLATE.format(a=a, b=b)}],
            )
            text = r.content[0].text
            d = parse_judgment(text)
            d["_in_tokens"] = r.usage.input_tokens
            d["_out_tokens"] = r.usage.output_tokens
            return d
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except anthropic.APIError as e:
            if attempt == retries - 1:
                return {"prob_same": None, "_error": str(e)}
            time.sleep(2 ** attempt)
    return {"prob_same": None, "_error": "exhausted retries"}


def split_halves(text: str):
    """Split a long article at the midpoint sentence boundary."""
    text = text.strip()
    mid = len(text) // 2
    # Find nearest sentence break after mid
    cut = text.find(". ", mid)
    if cut == -1 or cut > mid + 400:
        cut = mid
    return text[:cut + 1].strip(), text[cut + 1:].strip()


def run_controls(client: anthropic.Anthropic, df_2026: pd.DataFrame, df_old: pd.DataFrame,
                 n_pos: int = 6, n_neg_era: int = 6, n_neg_synth: int = 3, seed: int = 42):
    rng = random.Random(seed)
    pos_candidates = df_2026[df_2026["body_text"].str.len() >= 2400].sample(n=n_pos, random_state=seed)
    neg_2026 = df_2026.sample(n=n_neg_era + n_neg_synth, random_state=seed + 1)

    results = []

    # Positive: same article, two halves
    for _, row in pos_candidates.iterrows():
        a, b = split_halves(row["body_text"])
        a, b = snippet(a), snippet(b)
        r = judge_pair(client, a, b)
        r["kind"] = "pos_same_article"
        r["src_id"] = int(row["id"])
        results.append(r)
        print(f"  POS  id={row['id']}  prob_same={r.get('prob_same')}  in={r.get('_in_tokens')} out={r.get('_out_tokens')}")

    # Negative: 2026 vs old-era PO press release
    olds = df_old.sample(n=n_neg_era, random_state=seed + 2).reset_index(drop=True)
    for i, (_, row_a) in enumerate(neg_2026.head(n_neg_era).iterrows()):
        row_b = olds.iloc[i]
        a, b = snippet(row_a["body_text"]), snippet(row_b["body_text"])
        r = judge_pair(client, a, b)
        r["kind"] = "neg_era"
        r["src_a"] = int(row_a["id"])
        r["src_b"] = int(row_b["id"])
        r["date_a"] = row_a["published_date"]
        r["date_b"] = row_b["published_date"]
        results.append(r)
        print(f"  NEG-era  a={row_a['id']}({row_a['published_date'][:7]}) b={row_b['id']}({row_b['published_date'][:7]})  prob_same={r.get('prob_same')}")

    # Negative: 2026 vs synthetic clearly-different style
    for _, row in neg_2026.tail(n_neg_synth).iterrows():
        a = snippet(row["body_text"])
        r = judge_pair(client, a, SYNTHETIC_NEG)
        r["kind"] = "neg_synth"
        r["src_a"] = int(row["id"])
        results.append(r)
        print(f"  NEG-synth  id={row['id']}  prob_same={r.get('prob_same')}")

    return results


def summarize_controls(results):
    df = pd.DataFrame(results)
    by_kind = df.groupby("kind")["prob_same"].agg(["count", "mean", "std", "min", "max"])
    print("\n=== CONTROL SUMMARY ===")
    print(by_kind)
    pos = df[df["kind"] == "pos_same_article"]["prob_same"].dropna()
    neg = df[df["kind"].str.startswith("neg")]["prob_same"].dropna()
    if len(pos) and len(neg):
        print(f"\npositive mean: {pos.mean():.3f}  |  negative mean: {neg.mean():.3f}  |  gap: {pos.mean() - neg.mean():.3f}")
        return float(pos.mean() - neg.mean()), float(pos.mean()), float(neg.mean())
    return None, None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Skip controls; run full 780-pair batch")
    parser.add_argument("--sample-size", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--out-suffix", default="")
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        sys.exit("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()

    df_2026 = load_2026_long()
    print(f"Loaded {len(df_2026)} long 2026 PR articles", file=sys.stderr)
    df_old = load_old_press()
    print(f"Loaded {len(df_old)} old-era PR articles for negative controls", file=sys.stderr)

    if not args.full:
        print("\n--- RUNNING CONTROLS ---")
        results = run_controls(client, df_2026, df_old)
        gap, pos_mean, neg_mean = summarize_controls(results)
        out = OUT_DIR / f"llm_controls{args.out_suffix}.json"
        out.write_text(json.dumps({"results": results, "pos_mean": pos_mean, "neg_mean": neg_mean,
                                    "gap": gap}, indent=2, ensure_ascii=False))
        print(f"\nwrote {out}")
        if gap is not None:
            if gap >= 0.3:
                print("\n✓ CONTROLS PASS — gap >= 0.30 — safe to run --full")
            elif gap >= 0.15:
                print("\n⚠ CONTROLS WEAK — gap 0.15-0.30 — usable but noisier than ideal")
            else:
                print("\n✗ CONTROLS FAIL — gap < 0.15 — model cannot discriminate; do not proceed")
        return

    # Full run (only if --full)
    print("\n--- FULL PAIRWISE RUN ---")
    # Stratified sample by month
    df = df_2026.copy()
    df["month"] = df["published_date"].str[:7]
    rng = np.random.RandomState(42)
    months = sorted(df["month"].unique())
    per_month = max(1, args.sample_size // len(months))
    chosen = []
    for m in months:
        sub = df[df["month"] == m]
        chosen.append(sub.sample(n=min(per_month, len(sub)), random_state=rng))
    sample = pd.concat(chosen).head(args.sample_size).reset_index(drop=True)
    print(f"Sample size: {len(sample)} (by month: {sample['month'].value_counts().sort_index().to_dict()})")

    pairs = list(itertools.combinations(range(len(sample)), 2))
    print(f"Total pairs to judge: {len(pairs)}")

    snippets = [snippet(t) for t in sample["body_text"]]
    judgments = [None] * len(pairs)

    def worker(idx):
        i, j = pairs[idx]
        r = judge_pair(client, snippets[i], snippets[j])
        return idx, r

    start = time.time()
    completed = 0
    cost_in_tokens = 0
    cost_out_tokens = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = [ex.submit(worker, idx) for idx in range(len(pairs))]
        for fut in as_completed(futures):
            idx, r = fut.result()
            judgments[idx] = r
            completed += 1
            cost_in_tokens += r.get("_in_tokens") or 0
            cost_out_tokens += r.get("_out_tokens") or 0
            if completed % 20 == 0 or completed == len(pairs):
                elapsed = time.time() - start
                rate = completed / elapsed
                eta = (len(pairs) - completed) / rate if rate > 0 else 0
                print(f"  {completed}/{len(pairs)}  rate={rate:.1f}/s  eta={eta:.0f}s  "
                      f"tokens in={cost_in_tokens} out={cost_out_tokens}")

    # Build similarity matrix
    n = len(sample)
    S = np.full((n, n), np.nan)
    for (i, j), r in zip(pairs, judgments):
        p = r.get("prob_same")
        if p is not None:
            S[i, j] = S[j, i] = p
    np.fill_diagonal(S, 1.0)

    # Cluster: agglomerative on distance = 1 - S
    from sklearn.cluster import AgglomerativeClustering
    D = 1.0 - np.nan_to_num(S, nan=0.5)
    # Try a few k
    cluster_results = {}
    for k in (2, 3, 4, 5, 6):
        ac = AgglomerativeClustering(n_clusters=k, metric="precomputed", linkage="average")
        labels = ac.fit_predict(D)
        cluster_results[k] = labels.tolist()

    out = {
        "sample_ids": sample["id"].astype(int).tolist(),
        "sample_titles": sample["title"].tolist(),
        "sample_dates": sample["published_date"].tolist(),
        "pairs": [
            {"i": i, "j": j, "id_i": int(sample.iloc[i]["id"]), "id_j": int(sample.iloc[j]["id"]),
             "prob_same": r.get("prob_same"), "reason": r.get("reason")}
            for (i, j), r in zip(pairs, judgments)
        ],
        "similarity_matrix": S.tolist(),
        "cluster_assignments_by_k": {str(k): v for k, v in cluster_results.items()},
        "total_input_tokens": int(cost_in_tokens),
        "total_output_tokens": int(cost_out_tokens),
    }
    out_path = OUT_DIR / f"llm_pairwise_full{args.out_suffix}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\nwrote {out_path}")
    print(f"\nTokens: in={cost_in_tokens} out={cost_out_tokens}")
    cost = cost_in_tokens / 1e6 * 3.0 + cost_out_tokens / 1e6 * 15.0
    print(f"Approx cost: ${cost:.2f}")


if __name__ == "__main__":
    main()
