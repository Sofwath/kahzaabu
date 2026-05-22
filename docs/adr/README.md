# Architecture Decision Records

ADRs capture non-obvious architectural decisions made for kahzaabu, with their context, the alternatives considered, and the consequences accepted. We use Michael Nygard's lightweight format.

Read these when:
- A piece of code looks weird and you want to know why.
- You're considering changing a foundational choice (polarity taxonomy, label systems, schema shape) and need to understand what depended on it.
- You're citing kahzaabu as a reference project.

Each file is numbered chronologically (`NNNN-short-name.md`). Once accepted, ADRs are **append-only**: superseding a decision means writing a new ADR that links back, not editing the old one.

## Index

| # | Title | Status |
|---|---|---|
| 0001 | V2 architecture overview | Accepted |
| 0002 | Polarity taxonomy — 6 labels | Accepted |
| 0003 | Canonical claim matching | Accepted |
| 0004 | Contradiction verdict — 4-way | Accepted |
| 0005 | Dual labeling — AVeriTeC + PolitiFact | Accepted |
| 0006 | ClaimReview JSON-LD export | Accepted |
| 0007 | Embedding provider abstraction | Accepted (supersedes the embedding model choice in 0003) |
| 0008 | Quality evaluation methodology | Accepted |
| 0009 | OSS readiness, methodology cards, backup | Accepted |
| 0010 | Reproducibility manifest, observability, audit CLIs | Accepted |
| 0011 | Public-sector entity registry — external-reference trust anchor | Accepted |
| 0012 | mvlaw.gov.mv: link-out, not scrape | Accepted |
| 0013 | No in-app authentication; web UI is read-only public | Accepted |
| 0014 | Hermes ambient `pre_llm_call` hook + sticky-session persistence | Accepted |
