"""Identify and characterize the stylistic outliers from the LLM pairwise run."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"

with open(DATA / "llm_pairwise_full.json") as f:
    full = json.load(f)

S = np.array(full["similarity_matrix"], dtype=float)
n = S.shape[0]
ids = full["sample_ids"]
titles = full["sample_titles"]
dates = full["sample_dates"]

# Per-doc mean similarity to all others (off-diagonal)
np.fill_diagonal(S, np.nan)
mean_sim = np.nanmean(S, axis=1)
np.fill_diagonal(S, 1.0)

df = pd.DataFrame({
    "idx": range(n),
    "id": ids,
    "date": dates,
    "title": titles,
    "mean_sim": mean_sim,
})
df = df.sort_values("mean_sim").reset_index(drop=True)

print("=== 8 most outlying docs (lowest mean similarity to the bulk) ===\n")
for _, r in df.head(8).iterrows():
    # Top-3 nearest neighbors of this doc
    i = r["idx"]
    sims = [(j, S[i, j]) for j in range(n) if j != i]
    sims.sort(key=lambda x: -x[1])
    top3 = sims[:3]
    print(f"[{r['id']}] mean_sim={r['mean_sim']:.3f}  {r['date'][:10]}")
    print(f"  Title: {r['title']}")
    print(f"  Nearest 3:")
    for j, s in top3:
        print(f"    {s:.2f}  [{ids[j]}] {dates[j][:10]} — {titles[j][:75]}")
    print()

print("=== 5 most central docs (highest mean similarity — the 'voice') ===\n")
for _, r in df.tail(5).iloc[::-1].iterrows():
    print(f"[{r['id']}] mean_sim={r['mean_sim']:.3f}  {r['date'][:10]}")
    print(f"  Title: {r['title']}")
    print()

# Pull the bodies of the top 3 outliers from DB
top_outlier_ids = df.head(3)["id"].astype(int).tolist()
conn = sqlite3.connect(str(DATA / "kahzaabu.db"))
print("=== Outlier excerpts (first 600 chars of body) ===\n")
for art_id in top_outlier_ids:
    row = conn.execute(
        "SELECT id, title, published_date, body_text FROM articles WHERE id=? AND language='EN'",
        (art_id,)
    ).fetchone()
    if row:
        print(f"--- [{row[0]}] {row[2][:10]} — {row[1][:80]} ---")
        print(row[3][:600].strip())
        print()

# And a central doc for comparison
central_id = int(df.tail(1)["id"].iloc[0])
row = conn.execute(
    "SELECT id, title, published_date, body_text FROM articles WHERE id=? AND language='EN'",
    (central_id,)
).fetchone()
print(f"=== Central (house-voice) example: [{row[0]}] {row[2][:10]} — {row[1][:80]} ===")
print(row[3][:600].strip())

# Pairwise probabilities involving the top outliers — let's grab the reasoning text
print("\n=== Sample LLM reasoning for low-scoring pairs involving outliers ===\n")
pairs = full["pairs"]
outlier_idxs = df.head(3)["idx"].astype(int).tolist()
for outlier_i in outlier_idxs:
    outlier_id = ids[outlier_i]
    # Find lowest-scoring pair involving this outlier
    candidates = []
    for p in pairs:
        if p["i"] == outlier_i or p["j"] == outlier_i:
            if p["prob_same"] is not None:
                candidates.append(p)
    candidates.sort(key=lambda p: p["prob_same"])
    print(f"--- Outlier [{outlier_id}] — lowest-similarity pairs (reasoning):")
    for p in candidates[:2]:
        other_idx = p["j"] if p["i"] == outlier_i else p["i"]
        print(f"  vs [{ids[other_idx]}] '{titles[other_idx][:55]}'  prob={p['prob_same']:.2f}")
        print(f"    \"{p['reason']}\"")
    print()
