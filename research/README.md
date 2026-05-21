# research/

One-shot scripts and historical phase tools. **Not imported by the package.**

These are kept for reproducibility of past analyses — they ran once, produced
findings, and were superseded by the production modules in `kahzaabu/`. If you
re-run them, expect to update paths and re-fetch dependencies.

| File | Origin | Superseded by |
|---|---|---|
| `phase0_inventory.py` | Inventory pass over the early corpus | `kahzaabu/scraper.py` |
| `phase1_extract.py` | First-pass claim extraction prototype | `kahzaabu/extractor.py` |
| `phase2_curate.py` | First curation prototype (heuristic + LLM) | `kahzaabu/curator.py` |
| `phase3_full_extract.py` | Full re-extract after schema change | `kahzaabu/extractor.py` (incremental) |
| `phase4_full_curate.py` | Full re-curate after schema change | `kahzaabu/curator.py` (incremental) |
| `analyze_pairwise.py` | Pairwise comparison of writer-style features | — |
| `analyze_writers_2026.py` | KMeans + silhouette over 2026 articles | — |
| `analyze_writers_2026_v2.py` | Same, longer feature window | — |
| `find_outliers.py` | Stylometric outlier detection | — |
| `find_subsets.py` | Cluster-subset finder | — |
| `llm_pairwise.py` | LLM-judged pairwise stylometry (with calibration) | — |
| `migrate_json_to_db.py` | One-time JSON → SQLite ETL (already executed) | — |

If you need any of these for current work, lift the relevant function into the
`kahzaabu` package rather than calling these directly from production code.
