# ADR 0007 — Embedding provider abstraction

**Status**: Accepted (2026-05-21). Supersedes the "OpenAI text-embedding-3-small or equivalent" line in ADR 0003.

## Context

Kahzaabu is being open-sourced as a reference civic-tech project. ADR 0003 specified OpenAI's `text-embedding-3-small` as the canonical embedding model. That choice was right for the literature (RAGAR, AVeriTeC reference systems use it) but wrong for an OSS project: it requires a paid OpenAI account before any of the canonical-claim-matching work runs, which makes the project unreproducible for anyone without that key.

The first attempt to run Slice 3 hit `OpenAI 429 insufficient_quota` — confirming the operational fragility. We need a design where:

- An OSS user can clone the repo and run the full pipeline with zero API keys.
- Researchers comparing to the literature can still use the canonical OpenAI model.
- Anthropic-ecosystem users can pick Voyage AI (Anthropic's recommended embedding partner) without leaving their stack.
- Switching providers must not corrupt prior embeddings — heterogeneous-model cosine is meaningless.

## Decision

Introduce a provider abstraction (`kahzaabu/embeddings.py`):

```
EmbeddingProvider (ABC)
   ├── LocalEmbedder      sentence-transformers/all-MiniLM-L6-v2  dim 384   $0     ← default
   ├── OpenAIEmbedder     text-embedding-3-small                  dim 1536  $0.02/M
   └── VoyageEmbedder     voyage-3                                dim 1024  $0.06/M
```

Each declares `model`, `dim`, `price_per_m`, and an `is_available()` classmethod for cheap introspection (no API hit).

**Selection priority** (`get_provider()`):

1. Caller override (argument)
2. `KAHZAABU_EMBED_PROVIDER` env var ∈ {local, openai, voyage}
3. Auto: first of [local, openai, voyage] that `is_available()` returns ok

**Cross-provider safety**: `claim_embeddings.model` is part of the row. The matcher's candidate-pool query filters to `ce.model = ?` — embeddings from different providers never compare via cosine. If the user switches providers, prior embeddings stay queryable for legacy purposes but new embeddings establish a separate matching graph.

**Packaging**: split the `[ml]` extra into granular subextras:

```
[ml-local]   sentence-transformers + numpy   ← the OSS default
[ml-openai]  openai + numpy
[ml-voyage]  voyageai + numpy
[ml]         alias for [ml-local]            ← `pip install kahzaabu[ml]` works offline
```

`[all]` aggregates `[web, tui, mcp, ml]` = web + tui + mcp + ml-local. Users who want OpenAI install `kahzaabu[ml-openai]` explicitly.

## Alternatives considered

- **Hard-pin OpenAI** (original ADR 0003 stance). Rejected — fails the OSS reproducibility test.
- **Local-only**. Rejected — researchers comparing to RAGAR/AVeriTeC papers need to be able to use the literature's embedding.
- **Implicit fallback chain at runtime (try OpenAI, fall back to local on 429).** Rejected — silent fallback corrupts the cross-model safety invariant. Better to fail loudly and let the user pick.
- **Pluggable via separate plugin packages** (e.g. `kahzaabu-embeddings-openai`). Overkill for three providers; pyproject extras handle this cleanly.

## Consequences

**Positive.**

- The project clones-and-runs with `pip install kahzaabu[ml]` (≈ 200 MB sentence-transformers download on first use, no API keys).
- Researchers reproducing the literature baseline install `kahzaabu[ml-openai]` + set `OPENAI_API_KEY` + `KAHZAABU_EMBED_PROVIDER=openai`. The literature numbers become reproducible.
- The abstraction is the right shape for future providers — `kahzaabu[ml-cohere]`, local LLM providers, etc. Each is a 30-line class.

**Negative.**

- The matcher's candidate pool excludes other-model embeddings, so a single corpus with mixed providers fragments. Switching providers mid-life means either backfilling all embeddings under the new provider OR accepting that older claims with the old model live in a separate similarity neighborhood. Acceptable; documented.
- Vector dimension is no longer a compile-time constant (`EMBED_DIM` constant retained for tests that need a specific dim but the production schema stores `dim` per row).
- The `[ml-local]` default pulls a ~150 MB Python dependency (sentence-transformers) and downloads a ~80 MB model on first use. Acceptable for an OSS user; if it's too heavy in CI a `KAHZAABU_EMBED_PROVIDER=openai` env var skips the sentence-transformers code path entirely (and the dep can be omitted from the CI install).

## Notes on the LLM tiebreaker

The LLM that breaks ties when embedding matches but entities don't is currently hardcoded to Anthropic Haiku 4.5. That's a separate provider decision and will get its own ADR if/when the project needs to support OpenAI / local LLMs for the tiebreaker too. For now, kahzaabu's LLM dependency is unified on Anthropic across the pipeline; only the embedding layer is provider-agnostic.
