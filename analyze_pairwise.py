"""Analyze the 780-pair LLM similarity matrix and estimate writer count."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering, SpectralClustering
from sklearn.metrics import silhouette_score

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DATA = Path(__file__).parent / "data"

with open(DATA / "llm_pairwise_full.json") as f:
    full = json.load(f)
with open(DATA / "llm_controls.json") as f:
    controls = json.load(f)

S = np.array(full["similarity_matrix"], dtype=float)
n = S.shape[0]
print(f"n_docs={n}  pairs={n*(n-1)//2}")

ids = full["sample_ids"]
titles = full["sample_titles"]
dates = full["sample_dates"]
months = [d[:7] for d in dates]

# Off-diagonal scores
iu = np.triu_indices(n, k=1)
scores = S[iu]
print(f"\nScore distribution across {len(scores)} 2026-vs-2026 pairs:")
print(f"  mean={scores.mean():.3f}  median={np.median(scores):.3f}  std={scores.std():.3f}")
print(f"  min={scores.min():.3f}  p10={np.percentile(scores,10):.3f}  p25={np.percentile(scores,25):.3f}  "
      f"p50={np.percentile(scores,50):.3f}  p75={np.percentile(scores,75):.3f}  p90={np.percentile(scores,90):.3f}  "
      f"max={scores.max():.3f}")

# Control baselines for reference
pos_mean = controls["pos_mean"]
neg_mean = controls["neg_mean"]
print(f"\nControl baselines: pos(same article)={pos_mean:.3f}  neg(different era/author)={neg_mean:.3f}")

# Buckets
print("\nBucketed distribution of within-2026 pair scores:")
bins = [0, 0.2, 0.4, 0.6, 0.7, 0.8, 0.9, 1.01]
labels = ["≤0.2 (likely diff)", "0.2-0.4", "0.4-0.6 (ambiguous)", "0.6-0.7", "0.7-0.8", "0.8-0.9 (likely same)", "≥0.9 (very likely same)"]
hist, _ = np.histogram(scores, bins=bins)
for lab, c in zip(labels, hist):
    bar = "█" * int(c / max(hist.max(), 1) * 40)
    print(f"  {lab:<28} n={c:<4} {bar}")

# Per-doc: how many "high" partners (prob_same >= 0.7) does each doc have?
HIGH = 0.7
print(f"\nPer-doc degree at threshold prob_same >= {HIGH}:")
high_neighbors = [(S[i] >= HIGH).sum() - 1 for i in range(n)]  # -1 for self
print(f"  mean={np.mean(high_neighbors):.1f}  median={np.median(high_neighbors):.1f}  "
      f"min={min(high_neighbors)}  max={max(high_neighbors)}")

# Clustering: try several methods/k values, look at silhouette
print("\n=== Clustering (distance = 1 - S) ===")
D = 1.0 - S
np.fill_diagonal(D, 0.0)

silhouettes = {}
for k in range(2, 9):
    ac = AgglomerativeClustering(n_clusters=k, metric="precomputed", linkage="average")
    labels = ac.fit_predict(D)
    sil = silhouette_score(D, labels, metric="precomputed")
    silhouettes[k] = sil
    sizes = Counter(labels)
    print(f"  agglom k={k}: silhouette={sil:.3f}  sizes={dict(sorted(sizes.items()))}")

best_k = max(silhouettes, key=silhouettes.get)
print(f"\nBest k by silhouette: {best_k} (score={silhouettes[best_k]:.3f})")

# Spectral clustering as cross-check (uses similarity directly)
print("\nSpectral clustering cross-check:")
S_pos = np.clip(S, 0, 1)
np.fill_diagonal(S_pos, 1.0)
for k in range(2, 7):
    sc = SpectralClustering(n_clusters=k, affinity="precomputed", random_state=42,
                            assign_labels="kmeans", n_init=20)
    labels = sc.fit_predict(S_pos)
    sil = silhouette_score(1 - S_pos, labels, metric="precomputed")
    sizes = Counter(labels)
    print(f"  spectral k={k}: silhouette={sil:.3f}  sizes={dict(sorted(sizes.items()))}")

# Connected components at multiple thresholds: count communities at increasing strictness
print("\n=== Connected components (community-style) ===")
from scipy.sparse.csgraph import connected_components
from scipy.sparse import csr_matrix
for thr in (0.6, 0.65, 0.7, 0.75, 0.8, 0.85):
    A = (S >= thr).astype(int)
    np.fill_diagonal(A, 0)
    n_components, comp_labels = connected_components(csr_matrix(A))
    sizes = sorted(Counter(comp_labels).values(), reverse=True)
    singletons = sum(1 for s in sizes if s == 1)
    big = [s for s in sizes if s >= 2]
    print(f"  threshold={thr}: components={n_components}  "
          f"non-singleton={len(big)}  largest={big[:6]}  singletons={singletons}")

# Pick best k from agglomerative; characterize clusters
ac_best = AgglomerativeClustering(n_clusters=best_k, metric="precomputed", linkage="average")
final_labels = ac_best.fit_predict(D)

print(f"\n=== Cluster characterisation at k={best_k} (agglomerative) ===")
for c in sorted(set(final_labels)):
    members = [i for i, l in enumerate(final_labels) if l == c]
    if not members: continue
    avg_internal = np.mean([S[i, j] for i in members for j in members if i != j]) if len(members) > 1 else float('nan')
    avg_external = np.mean([S[i, j] for i in members for j in range(n) if final_labels[j] != c])
    month_dist = Counter(months[i] for i in members)
    print(f"\nCluster {c}  n={len(members)}  internal_sim={avg_internal:.3f}  external_sim={avg_external:.3f}")
    print(f"  months: {dict(sorted(month_dist.items()))}")
    print(f"  sample titles:")
    for i in members[:5]:
        print(f"    [{ids[i]}] {dates[i][:10]} — {titles[i][:80]}")

# Save cluster heatmap (sorted by cluster)
order = np.argsort(final_labels)
S_sorted = S[np.ix_(order, order)]
fig, ax = plt.subplots(figsize=(8, 7))
im = ax.imshow(S_sorted, cmap="viridis", vmin=0, vmax=1, aspect="equal")
plt.colorbar(im, label="prob_same")
# Cluster boundary lines
boundaries = []
cur = final_labels[order[0]]
for i, idx in enumerate(order):
    if final_labels[idx] != cur:
        boundaries.append(i)
        cur = final_labels[idx]
for b in boundaries:
    ax.axhline(b - 0.5, color="white", lw=0.8)
    ax.axvline(b - 0.5, color="white", lw=0.8)
ax.set_title(f"Pairwise LLM similarity (n=40, sorted by k={best_k} clusters)")
plt.tight_layout()
plt.savefig(DATA / "llm_pairwise_heatmap.png", dpi=130)
print(f"\nwrote {DATA / 'llm_pairwise_heatmap.png'}")

# Summary verdict
print("\n=== VERDICT ===")
print(f"Sample: 40 long 2026 EN press releases, stratified across Jan-May.")
print(f"Mean within-2026 pair similarity: {scores.mean():.3f}  (vs same-article-control {pos_mean:.3f}, different-era-control {neg_mean:.3f})")
print(f"Best silhouette: k={best_k} at {silhouettes[best_k]:.3f}")
gap_high = scores.mean() - neg_mean
gap_low = pos_mean - scores.mean()
print(f"Within-2026 sits {gap_high:.2f} above neg-control and {gap_low:.2f} below pos-control")

# Save a structured summary
summary = {
    "n_docs": n,
    "n_pairs": int(n * (n - 1) / 2),
    "score_distribution": {
        "mean": float(scores.mean()),
        "median": float(np.median(scores)),
        "std": float(scores.std()),
        "percentiles": {p: float(np.percentile(scores, p)) for p in (10, 25, 50, 75, 90)},
        "buckets": {labels[i]: int(hist[i]) for i in range(len(labels))},
    },
    "control_baselines": {"pos_mean": pos_mean, "neg_mean": neg_mean},
    "silhouettes_agglom": {int(k): float(v) for k, v in silhouettes.items()},
    "best_k": int(best_k),
    "best_k_silhouette": float(silhouettes[best_k]),
    "clusters": [
        {
            "cluster": int(c),
            "n": int((final_labels == c).sum()),
            "internal_similarity": float(np.mean([S[i, j] for i in np.where(final_labels == c)[0]
                                                    for j in np.where(final_labels == c)[0] if i != j])) if (final_labels == c).sum() > 1 else None,
            "month_distribution": dict(Counter(months[i] for i in np.where(final_labels == c)[0])),
            "doc_ids": [int(ids[i]) for i in np.where(final_labels == c)[0]],
        }
        for c in sorted(set(final_labels))
    ],
}
(DATA / "llm_pairwise_analysis.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
print(f"wrote {DATA / 'llm_pairwise_analysis.json'}")
