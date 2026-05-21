"""Follow-up stylometric runs:

1. Cluster long reportage articles only (more text per doc → better author signal).
2. Hand-crafted features only (no char n-grams) on all docs.
3. Date-stratified check: do clusters track time periods (rotating writers)?

Outputs:
  data/writers_2026_v2_summary.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from analyze_writers_2026 import (
    load_articles, structural_features, function_word_features,
)

OUT_DIR = Path(__file__).parent / "data"


def feature_df(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for text in df["body_text"]:
        feats = {}
        feats.update(structural_features(text))
        feats.update(function_word_features(text))
        rows.append(feats)
    return pd.DataFrame(rows)


def sweep(X, name, k_range=(2, 8)):
    scores = {}
    for k in range(k_range[0], k_range[1] + 1):
        km = KMeans(n_clusters=k, random_state=42, n_init=20)
        labels = km.fit_predict(X)
        scores[k] = float(silhouette_score(X, labels))
    print(f"  [{name}] silhouette by k:", {k: f"{s:.3f}" for k, s in scores.items()})
    return scores


def cluster_at(X, k):
    km = KMeans(n_clusters=k, random_state=42, n_init=20)
    return km.fit_predict(X), km


def cluster_report(df: pd.DataFrame, feat_df: pd.DataFrame, labels, top_n=8):
    out = []
    overall_mean = feat_df.mean()
    overall_std = feat_df.std().replace(0, 1)
    for c in sorted(set(labels)):
        mask = labels == c
        sub = df[mask]
        cluster_mean = feat_df[mask].mean()
        z = (cluster_mean - overall_mean) / overall_std
        top_high = z.sort_values(ascending=False).head(top_n)
        dates = pd.to_datetime(sub["published_date"], errors="coerce")
        out.append({
            "cluster": int(c),
            "n": int(mask.sum()),
            "date_min": str(dates.min().date()) if not dates.isna().all() else None,
            "date_max": str(dates.max().date()) if not dates.isna().all() else None,
            "month_dist": dates.dt.to_period("M").astype(str).value_counts().sort_index().to_dict(),
            "avg_body_len": int(sub["body_text"].str.len().mean()),
            "avg_sent_len": float(feat_df.loc[mask, "avg_sent_len"].mean()),
            "distinctive_high": [(n, float(s)) for n, s in top_high.items()],
            "sample_titles": sub["title"].head(4).tolist(),
        })
    return out


def main():
    df = load_articles().reset_index(drop=True)
    print(f"Loaded {len(df)} 2026 EN press releases", file=sys.stderr)

    # --- Pass 1: hand-crafted features only, all docs ---
    print("\n--- Pass 1: hand-crafted features, all docs ---", file=sys.stderr)
    feat_all = feature_df(df)
    X_all = StandardScaler().fit_transform(feat_all.values)
    scores_all = sweep(X_all, "hand-only all")
    best_k_all = max(scores_all, key=scores_all.get)
    labels_all, _ = cluster_at(X_all, best_k_all)
    report_all = cluster_report(df, feat_all, labels_all)

    # --- Pass 2: long reportage subset (>=1500 chars) ---
    print("\n--- Pass 2: long-reportage subset only ---", file=sys.stderr)
    long_mask = df["body_text"].str.len() >= 1500
    df_long = df[long_mask].reset_index(drop=True)
    print(f"  n_long = {len(df_long)}", file=sys.stderr)
    feat_long = feature_df(df_long)
    X_long = StandardScaler().fit_transform(feat_long.values)
    scores_long = sweep(X_long, "long-only")
    best_k_long = max(scores_long, key=scores_long.get)
    labels_long, _ = cluster_at(X_long, best_k_long)
    report_long = cluster_report(df_long, feat_long, labels_long)

    # --- Pass 3: same but force k=3,4,5 to see month/date drift even when silhouette is bad ---
    print("\n--- Pass 3: long-only at k=3,4,5 — month distribution check ---", file=sys.stderr)
    forced_reports = {}
    for k in (3, 4, 5):
        labels_k, _ = cluster_at(X_long, k)
        forced_reports[k] = cluster_report(df_long, feat_long, labels_k)

    # PCA scatter for long-only at best k
    pca = PCA(n_components=2, random_state=42)
    X_long_2d = pca.fit_transform(X_long)
    plt.figure(figsize=(9, 7))
    cmap = plt.colormaps.get_cmap("tab10")
    for c in sorted(set(labels_long)):
        mask = labels_long == c
        plt.scatter(X_long_2d[mask, 0], X_long_2d[mask, 1], s=24, alpha=0.7,
                    color=cmap(c % 10), label=f"cluster {c} (n={int(mask.sum())})")
    plt.title(f"2026 EN press releases — long-reportage stylometric clusters (k={best_k_long})")
    plt.xlabel(f"PC1 ({pca.explained_variance_ratio_[0]*100:.1f}%)")
    plt.ylabel(f"PC2 ({pca.explained_variance_ratio_[1]*100:.1f}%)")
    plt.legend(loc="best", fontsize=9)
    plt.tight_layout()
    plt.savefig(OUT_DIR / "writers_2026_v2_long_scatter.png", dpi=130)

    # Console digest
    print("\n=== PASS 1: hand-only, all 284 docs ===")
    print(f"best_k={best_k_all}  silhouette={scores_all[best_k_all]:.3f}")
    for c in report_all:
        print(f"  C{c['cluster']} n={c['n']} dates {c['date_min']}..{c['date_max']} "
              f"avg_len={c['avg_body_len']} avg_sent={c['avg_sent_len']:.1f}")
        print("    top:", ", ".join(f"{n}({s:+.2f})" for n, s in c["distinctive_high"][:6]))

    print(f"\n=== PASS 2: long-only ({len(df_long)} docs ≥1500 chars) ===")
    print(f"best_k={best_k_long}  silhouette={scores_long[best_k_long]:.3f}")
    for c in report_long:
        print(f"  C{c['cluster']} n={c['n']} dates {c['date_min']}..{c['date_max']} "
              f"avg_len={c['avg_body_len']} avg_sent={c['avg_sent_len']:.1f}")
        print("    top:", ", ".join(f"{n}({s:+.2f})" for n, s in c["distinctive_high"][:6]))
        print("    months:", dict(list(c["month_dist"].items())))
        for t in c["sample_titles"][:2]:
            print(f"      - {t[:85]}")

    for k in (3, 4, 5):
        print(f"\n=== PASS 3: long-only, forced k={k} ===")
        sil = silhouette_score(X_long, cluster_at(X_long, k)[0])
        print(f"silhouette={sil:.3f}")
        for c in forced_reports[k]:
            print(f"  C{c['cluster']} n={c['n']} months: {dict(list(c['month_dist'].items()))}")
            print("    top:", ", ".join(f"{n}({s:+.2f})" for n, s in c["distinctive_high"][:6]))

    summary = {
        "pass1_hand_only_all": {
            "n": int(len(df)),
            "silhouette_by_k": scores_all,
            "best_k": int(best_k_all),
            "clusters": report_all,
        },
        "pass2_long_only": {
            "n": int(len(df_long)),
            "silhouette_by_k": scores_long,
            "best_k": int(best_k_long),
            "clusters": report_long,
        },
        "pass3_forced_k": {
            str(k): {
                "silhouette": float(silhouette_score(X_long, cluster_at(X_long, k)[0])),
                "clusters": forced_reports[k],
            }
            for k in (3, 4, 5)
        },
    }
    (OUT_DIR / "writers_2026_v2_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
