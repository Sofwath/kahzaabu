"""Identify institutional sub-streams in 2026 EN press releases by title/body patterns.

No API calls — just title/keyword filters against the DB.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pandas as pd

DB = Path(__file__).parent / "data" / "kahzaabu.db"

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row
df = pd.DataFrame([dict(r) for r in conn.execute(
    """SELECT id, title, body_text, published_date
       FROM articles
       WHERE category='press_release' AND language='EN'
         AND published_date LIKE '2026-%'
         AND body_text IS NOT NULL AND body_text != ''
       ORDER BY published_date""").fetchall()])
print(f"Total 2026 EN press releases: {len(df)}\n")

# Subset definitions: title or body markers
SUBSETS = {
    "Presidential decrees": {
        "title": re.compile(r"\b(decree|ratifies|ratification|ratifying)\b", re.I),
        "body": re.compile(r"\bPresidential Decree No\.?|under Act No\.\s?\d", re.I),
    },
    "Spokesperson briefings (Spox)": {
        "title": re.compile(r"\b(spokesperson|spox)\b", re.I),
        "body": re.compile(r"Chief Government Spokesperson|Presser with the Spox", re.I),
    },
    "Cabinet committee briefings": {
        "title": re.compile(r"\b(cabinet committee|special cabinet)\b", re.I),
        "body": re.compile(r"Special Cabinet Committee|Cabinet meeting today", re.I),
    },
    "Speech/address writeups": {
        "title": re.compile(r"\b(speech|address|delivered|inaugurat)\b", re.I),
        "body": re.compile(r"In his (?:speech|address)|delivered (?:a|the) (?:keynote|speech|address)", re.I),
    },
    "Diplomatic / congratulatory": {
        "title": re.compile(r"\b(condolence|congratulat|state visit|reaffirms commitment|extends|conveys)\b", re.I),
        "body": re.compile(r"(?:extended|conveyed) his (?:condolences|congratulations|sincere|deepest)", re.I),
    },
}


def label_subset(row):
    labels = []
    for name, pats in SUBSETS.items():
        if pats["title"].search(row["title"]) or pats["body"].search(row["body_text"]):
            labels.append(name)
    return labels


df["labels"] = df.apply(label_subset, axis=1)
df["primary_label"] = df["labels"].apply(lambda L: L[0] if L else "Other/general")
df["any_label"] = df["labels"].apply(lambda L: ", ".join(L) if L else "Other/general")

# Counts
print("=== Subset counts (a doc may match multiple — primary label is the first match) ===\n")
counts = df["primary_label"].value_counts()
for label, n in counts.items():
    pct = n / len(df) * 100
    print(f"  {label:<35} n={n:<4} ({pct:.1f}%)")

print(f"\nMulti-label rows: {(df['labels'].apply(len) > 1).sum()}")
print(f"Unlabeled (general house voice): {(df['labels'].apply(len) == 0).sum()}")

# Detailed views
print("\n\n=== DETAIL by subset ===")
for name in SUBSETS:
    sub = df[df["labels"].apply(lambda L: name in L)]
    if len(sub) == 0:
        continue
    print(f"\n--- {name}  (n={len(sub)}) ---")
    print(f"  date span: {sub['published_date'].min()[:10]} → {sub['published_date'].max()[:10]}")
    print(f"  monthly: {dict(sorted(sub['published_date'].str[:7].value_counts().items()))}")
    print(f"  avg body length: {int(sub['body_text'].str.len().mean())} chars")
    print(f"  sample titles:")
    for _, r in sub.head(7).iterrows():
        print(f"    [{r['id']}] {r['published_date'][:10]} — {r['title'][:90]}")

# Cross-reference: which subsets contain the original 40-doc sample's outliers?
print("\n\n=== Where did our 3 outliers from the LLM run fall? ===")
outliers = [36621, 36090, 36466]
for art_id in outliers:
    row = df[df["id"] == art_id]
    if len(row):
        r = row.iloc[0]
        print(f"  [{art_id}] '{r['title'][:70]}'")
        print(f"      labels: {r['any_label']}")

# Save the subset assignments for downstream pairwise within-subset
out_df = df[["id", "title", "published_date", "any_label", "primary_label"]].copy()
out_df.to_csv(Path(__file__).parent / "data" / "subsets_2026.csv", index=False)
print(f"\nwrote data/subsets_2026.csv")
