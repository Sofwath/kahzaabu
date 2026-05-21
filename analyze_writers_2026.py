"""Stylometric clustering of 2026 EN press releases to estimate distinct writers.

One-off analysis. Outputs:
  data/writers_2026_summary.json     - cluster summary + per-doc assignments
  data/writers_2026_scatter.png      - PCA 2D scatter coloured by cluster
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DB_PATH = Path(__file__).parent / "data" / "kahzaabu.db"
OUT_DIR = Path(__file__).parent / "data"

# Mosteller-Wallace-style function words: topic-independent, author-revealing.
FUNCTION_WORDS = [
    "the", "of", "to", "and", "a", "in", "is", "it", "you", "that", "he",
    "was", "for", "on", "are", "with", "as", "his", "they", "be", "at",
    "one", "have", "this", "from", "or", "had", "by", "but", "some", "what",
    "there", "we", "can", "out", "other", "were", "all", "your", "when",
    "up", "how", "said", "each", "she", "which", "do", "their", "time",
    "if", "will", "way", "about", "many", "then", "them", "would", "like",
    "so", "these", "her", "long", "make", "see", "him", "two", "has", "more",
    "could", "did", "such", "also", "no", "not", "any", "only", "very",
    "much", "during", "while", "before", "after", "between", "through",
    "however", "therefore", "furthermore", "moreover", "additionally",
    "nonetheless", "notwithstanding", "regarding", "concerning", "pursuant",
    "whereas", "hereby", "accordingly", "thus", "hence", "indeed",
]

# Content/style flags worth tracking — verbs commonly used in govt prose.
ATTRIBUTION_VERBS = [
    "said", "stated", "expressed", "emphasized", "emphasised", "highlighted",
    "reiterated", "underscored", "noted", "added", "remarked", "observed",
    "declared", "announced", "affirmed", "reaffirmed", "acknowledged",
    "extended", "conveyed",
]


def load_articles() -> pd.DataFrame:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, body_text, published_date
           FROM articles
           WHERE category = 'press_release'
             AND language = 'EN'
             AND published_date LIKE '2026-%'
             AND body_text IS NOT NULL AND body_text != ''
           ORDER BY published_date, id"""
    ).fetchall()
    conn.close()
    df = pd.DataFrame([dict(r) for r in rows])
    return df


WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def structural_features(text: str) -> dict:
    words = WORD_RE.findall(text)
    n_words = max(len(words), 1)
    lower_words = [w.lower() for w in words]
    sentences = [s for s in SENT_SPLIT_RE.split(text) if s.strip()]
    n_sents = max(len(sentences), 1)
    sent_lens = [len(WORD_RE.findall(s)) for s in sentences]
    word_lens = [len(w) for w in words]

    feats = {
        "avg_word_len": float(np.mean(word_lens)) if word_lens else 0.0,
        "avg_sent_len": float(np.mean(sent_lens)),
        "std_sent_len": float(np.std(sent_lens)) if len(sent_lens) > 1 else 0.0,
        "pct_long_sents": sum(1 for s in sent_lens if s > 25) / n_sents,
        "type_token_ratio": len(set(lower_words)) / n_words,
        "comma_per_word": text.count(",") / n_words,
        "semicolon_per_1k": text.count(";") / n_words * 1000,
        "emdash_per_1k": (text.count("—") + text.count("--")) / n_words * 1000,
        "quote_per_1k": (text.count("“") + text.count("”") + text.count('"')) / n_words * 1000,
        "ly_adverb_pct": sum(1 for w in lower_words if w.endswith("ly")) / n_words,
        "passive_marker_pct": sum(1 for w in lower_words if w in {"was", "were", "been", "being"}) / n_words,
    }
    return feats


def function_word_features(text: str) -> dict:
    words = [w.lower() for w in WORD_RE.findall(text)]
    n = max(len(words), 1)
    c = Counter(words)
    feats = {f"fw_{w}": c.get(w, 0) / n for w in FUNCTION_WORDS}
    feats.update({f"av_{w}": c.get(w, 0) / n for w in ATTRIBUTION_VERBS})
    return feats


def build_feature_matrix(df: pd.DataFrame):
    rows = []
    for text in df["body_text"]:
        feats = {}
        feats.update(structural_features(text))
        feats.update(function_word_features(text))
        rows.append(feats)
    feat_df = pd.DataFrame(rows)
    feat_names = list(feat_df.columns)
    X_hand = StandardScaler().fit_transform(feat_df.values)

    # Character 3-grams capture morphological / spelling habits (very strong for style)
    char_vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 4),
                               max_features=1500, min_df=5, sublinear_tf=True)
    X_char = char_vec.fit_transform(df["body_text"]).toarray()
    X_char = StandardScaler(with_mean=False).fit_transform(X_char)

    # Concatenate. Hand-crafted features get a modest weight boost so they aren't drowned
    # by 1500 char-ngram dims.
    X = np.hstack([X_hand * 3.0, X_char])
    return X, feat_df, feat_names, char_vec


def pick_k(X, k_range=(2, 8)):
    scores = {}
    inertia = {}
    for k in range(k_range[0], k_range[1] + 1):
        km = KMeans(n_clusters=k, random_state=42, n_init=20)
        labels = km.fit_predict(X)
        scores[k] = silhouette_score(X, labels)
        inertia[k] = km.inertia_
    return scores, inertia


def cluster_distinctive_features(feat_df: pd.DataFrame, labels: np.ndarray, top_n: int = 8):
    """For each cluster, find features whose mean is most elevated vs the rest."""
    out = {}
    overall_mean = feat_df.mean()
    overall_std = feat_df.std().replace(0, 1)
    for c in sorted(set(labels)):
        mask = labels == c
        cluster_mean = feat_df[mask].mean()
        z = (cluster_mean - overall_mean) / overall_std
        top_high = z.sort_values(ascending=False).head(top_n)
        top_low = z.sort_values(ascending=True).head(top_n)
        out[c] = {
            "high": [(name, float(score)) for name, score in top_high.items()],
            "low": [(name, float(score)) for name, score in top_low.items()],
        }
    return out


def main():
    print("Loading 2026 EN press releases...", file=sys.stderr)
    df = load_articles()
    print(f"  n={len(df)} articles", file=sys.stderr)

    print("Building feature matrix...", file=sys.stderr)
    X, feat_df, feat_names, char_vec = build_feature_matrix(df)
    print(f"  shape={X.shape}", file=sys.stderr)

    print("Sweeping k=2..8 for silhouette...", file=sys.stderr)
    scores, inertia = pick_k(X)
    for k, s in scores.items():
        print(f"  k={k}  silhouette={s:.4f}  inertia={inertia[k]:.0f}", file=sys.stderr)
    best_k = max(scores, key=scores.get)
    print(f"  best k by silhouette: {best_k} (score={scores[best_k]:.4f})", file=sys.stderr)

    # Cluster at best k
    km = KMeans(n_clusters=best_k, random_state=42, n_init=20)
    labels = km.fit_predict(X)

    # Distinctive features per cluster (on hand-crafted features only, char-ngrams are too noisy to name)
    distinctive = cluster_distinctive_features(feat_df, labels)

    # Per-cluster summary
    cluster_summary = []
    for c in sorted(set(labels)):
        mask = labels == c
        sub = df[mask]
        dates = pd.to_datetime(sub["published_date"], errors="coerce")
        cluster_summary.append({
            "cluster": int(c),
            "n_articles": int(mask.sum()),
            "date_min": str(dates.min().date()) if not dates.isna().all() else None,
            "date_max": str(dates.max().date()) if not dates.isna().all() else None,
            "month_distribution": dates.dt.to_period("M").astype(str).value_counts().sort_index().to_dict(),
            "distinctive_high": distinctive[c]["high"],
            "distinctive_low": distinctive[c]["low"],
            "sample_titles": sub["title"].head(5).tolist(),
            "avg_body_len": int(sub["body_text"].str.len().mean()),
        })

    # PCA scatter
    print("Building PCA scatter...", file=sys.stderr)
    pca = PCA(n_components=2, random_state=42)
    X2 = pca.fit_transform(X)
    plt.figure(figsize=(9, 7))
    cmap = plt.colormaps.get_cmap("tab10")
    for c in sorted(set(labels)):
        mask = labels == c
        plt.scatter(X2[mask, 0], X2[mask, 1], s=18, alpha=0.7,
                    color=cmap(c % 10), label=f"cluster {c} (n={int(mask.sum())})")
    plt.title(f"2026 EN press releases — stylometric clusters (k={best_k})")
    plt.xlabel(f"PC1 ({pca.explained_variance_ratio_[0]*100:.1f}%)")
    plt.ylabel(f"PC2 ({pca.explained_variance_ratio_[1]*100:.1f}%)")
    plt.legend(loc="best", fontsize=9)
    plt.tight_layout()
    out_png = OUT_DIR / "writers_2026_scatter.png"
    plt.savefig(out_png, dpi=130)
    print(f"  wrote {out_png}", file=sys.stderr)

    # Write JSON summary
    out_json = OUT_DIR / "writers_2026_summary.json"
    summary = {
        "n_articles": int(len(df)),
        "date_range": [str(df["published_date"].min()), str(df["published_date"].max())],
        "silhouette_by_k": {int(k): float(v) for k, v in scores.items()},
        "best_k": int(best_k),
        "clusters": cluster_summary,
        "doc_assignments": [
            {"id": int(r["id"]), "title": r["title"], "date": r["published_date"], "cluster": int(l)}
            for r, l in zip(df.to_dict("records"), labels)
        ],
    }
    out_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"  wrote {out_json}", file=sys.stderr)

    # Console digest
    print("\n=== CLUSTER DIGEST ===")
    print(f"n={len(df)}  best_k={best_k}  silhouette={scores[best_k]:.3f}")
    for c in cluster_summary:
        print(f"\nCluster {c['cluster']}  n={c['n_articles']}  dates {c['date_min']}..{c['date_max']}  "
              f"avg_len={c['avg_body_len']}")
        print("  distinctive ↑:", ", ".join(f"{n}({s:+.2f})" for n, s in c["distinctive_high"][:6]))
        print("  distinctive ↓:", ", ".join(f"{n}({s:+.2f})" for n, s in c["distinctive_low"][:6]))
        print("  sample titles:")
        for t in c["sample_titles"][:3]:
            print(f"    - {t[:90]}")


if __name__ == "__main__":
    main()
