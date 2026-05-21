"""V2 — Embedding provider abstraction (ADR 0007).

Kahzaabu is an open-source project. Foundational features like claim
matching must not require a single proprietary API. This module
exposes a unified `EmbeddingProvider` ABC and three concrete
implementations:

  - LocalEmbedder      sentence-transformers, no API key, no ongoing cost
  - OpenAIEmbedder     text-embedding-3-small (literature default)
  - VoyageEmbedder     voyage-3 (Anthropic-recommended ecosystem)

Selection priority (first available wins):

  1. Explicit: KAHZAABU_EMBED_PROVIDER env var ∈ {local, openai, voyage}
  2. Auto-detect: prefer local if installed, else openai if key, else voyage
  3. Hard-fail with a clear message naming the install options

Each provider declares its `model`, `dimension`, and `price_per_m_tokens`
so the matching pipeline can budget-cap and dedupe correctly. The
matcher persists `model` per embedding row, so re-runs with a
different provider don't poison cosine-similarity comparisons —
heterogeneous-model rows are skipped during candidate scoring.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger("kahzaabu")


@dataclass
class EmbedBatch:
    """Result of one batch embed call."""
    vectors: list[list[float]]
    tokens: int            # total tokens billed (0 for local)
    cost_usd: float        # dollars billed for this batch
    model: str             # specific model id used
    dim: int               # vector dimension


class EmbeddingProvider(ABC):
    """ABC for embedding providers. All subclasses must declare their
    model identifier, vector dimension, and dollar-per-million-token
    price (0.0 for local models)."""

    model: str
    dim: int
    price_per_m: float

    @abstractmethod
    def embed(self, texts: list[str]) -> EmbedBatch: ...

    @classmethod
    @abstractmethod
    def is_available(cls) -> tuple[bool, str]:
        """Return (available, reason). Reason explains the gap if not
        available (missing key, missing dep, etc.). Cheap to call —
        no API hits."""


# ──────────────────────────────────────────────────────────────────────
# Local (sentence-transformers) — the default for an OSS project
# ──────────────────────────────────────────────────────────────────────

class LocalEmbedder(EmbeddingProvider):
    """sentence-transformers — no API key, no ongoing cost. Default."""
    model = "sentence-transformers/all-MiniLM-L6-v2"
    dim = 384
    price_per_m = 0.0   # local CPU/GPU; no API billing

    def __init__(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "LocalEmbedder needs the [ml-local] extra: "
                "`pip install kahzaabu[ml-local]`"
            ) from e
        # Cache the model on the instance — initial load is ~5s
        self._model = SentenceTransformer(self.model)

    def embed(self, texts: list[str]) -> EmbedBatch:
        vecs = self._model.encode(texts, convert_to_numpy=False,
                                    show_progress_bar=False)
        # Convert to plain python lists for downstream packing
        out = [list(map(float, v)) for v in vecs]
        return EmbedBatch(vectors=out, tokens=0, cost_usd=0.0,
                           model=self.model, dim=self.dim)

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import sentence_transformers  # noqa: F401
            return True, "ok"
        except ImportError:
            return (False, "sentence-transformers not installed — "
                           "`pip install kahzaabu[ml-local]`")


# ──────────────────────────────────────────────────────────────────────
# OpenAI — text-embedding-3-small (literature default)
# ──────────────────────────────────────────────────────────────────────

class OpenAIEmbedder(EmbeddingProvider):
    model = "text-embedding-3-small"
    dim = 1536
    price_per_m = 0.02

    def __init__(self):
        if "OPENAI_API_KEY" not in os.environ:
            raise RuntimeError(
                "OpenAIEmbedder needs OPENAI_API_KEY in the environment"
            )
        try:
            import openai
        except ImportError as e:
            raise RuntimeError(
                "OpenAIEmbedder needs the [ml-openai] extra: "
                "`pip install kahzaabu[ml-openai]`"
            ) from e
        self._client = openai.OpenAI()

    def embed(self, texts: list[str]) -> EmbedBatch:
        r = self._client.embeddings.create(model=self.model, input=texts)
        vecs = [d.embedding for d in r.data]
        tokens = r.usage.total_tokens
        cost = tokens / 1e6 * self.price_per_m
        return EmbedBatch(vectors=vecs, tokens=tokens, cost_usd=cost,
                           model=self.model, dim=self.dim)

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        if "OPENAI_API_KEY" not in os.environ:
            return False, "OPENAI_API_KEY not set"
        try:
            import openai  # noqa: F401
            return True, "ok"
        except ImportError:
            return (False, "openai SDK not installed — "
                           "`pip install kahzaabu[ml-openai]`")


# ──────────────────────────────────────────────────────────────────────
# Voyage AI — voyage-3 (Anthropic-recommended)
# ──────────────────────────────────────────────────────────────────────

class VoyageEmbedder(EmbeddingProvider):
    model = "voyage-3"
    dim = 1024
    price_per_m = 0.06

    def __init__(self):
        if "VOYAGE_API_KEY" not in os.environ:
            raise RuntimeError(
                "VoyageEmbedder needs VOYAGE_API_KEY in the environment"
            )
        try:
            import voyageai
        except ImportError as e:
            raise RuntimeError(
                "VoyageEmbedder needs the [ml-voyage] extra: "
                "`pip install kahzaabu[ml-voyage]`"
            ) from e
        self._client = voyageai.Client()

    def embed(self, texts: list[str]) -> EmbedBatch:
        r = self._client.embed(texts, model=self.model)
        # voyageai returns r.embeddings (list of lists) and r.total_tokens
        vecs = r.embeddings
        tokens = getattr(r, "total_tokens", 0) or 0
        cost = tokens / 1e6 * self.price_per_m
        return EmbedBatch(vectors=vecs, tokens=tokens, cost_usd=cost,
                           model=self.model, dim=self.dim)

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        if "VOYAGE_API_KEY" not in os.environ:
            return False, "VOYAGE_API_KEY not set"
        try:
            import voyageai  # noqa: F401
            return True, "ok"
        except ImportError:
            return (False, "voyageai SDK not installed — "
                           "`pip install kahzaabu[ml-voyage]`")


# ──────────────────────────────────────────────────────────────────────
# Selection
# ──────────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    "local":   LocalEmbedder,
    "openai":  OpenAIEmbedder,
    "voyage":  VoyageEmbedder,
}


def get_provider(name: str | None = None,
                  env: dict | None = None) -> EmbeddingProvider:
    """Return an instantiated `EmbeddingProvider`. Selection priority:

      1. `name` argument (caller override)
      2. KAHZAABU_EMBED_PROVIDER env var
      3. Auto: first of [local, openai, voyage] that is_available()

    Raises RuntimeError with a clear message if no provider works.
    """
    env = env if env is not None else os.environ
    explicit = name or env.get("KAHZAABU_EMBED_PROVIDER", "").strip().lower()

    if explicit:
        if explicit not in PROVIDERS:
            raise RuntimeError(
                f"Unknown KAHZAABU_EMBED_PROVIDER={explicit!r}; "
                f"valid: {sorted(PROVIDERS)}"
            )
        ok, reason = PROVIDERS[explicit].is_available()
        if not ok:
            raise RuntimeError(
                f"{explicit} provider unavailable: {reason}"
            )
        return PROVIDERS[explicit]()

    # Auto-detect
    for pname in ("local", "openai", "voyage"):
        ok, _ = PROVIDERS[pname].is_available()
        if ok:
            logger.info("embeddings: auto-selected provider %r", pname)
            return PROVIDERS[pname]()

    raise RuntimeError(
        "No embedding provider available. Install at least one extra:\n"
        "  pip install kahzaabu[ml-local]   (no API key needed)\n"
        "  pip install kahzaabu[ml-openai]  (needs OPENAI_API_KEY)\n"
        "  pip install kahzaabu[ml-voyage]  (needs VOYAGE_API_KEY)\n"
        "Or set KAHZAABU_EMBED_PROVIDER explicitly."
    )


def report_availability(env: dict | None = None) -> dict:
    """For `kahzaabu doctor`-style introspection. No instantiation;
    just probes is_available() on each provider."""
    env = env if env is not None else os.environ
    out = {"selected": None, "providers": {}}
    requested = env.get("KAHZAABU_EMBED_PROVIDER", "").strip().lower() or None
    out["requested"] = requested
    for name, cls in PROVIDERS.items():
        ok, reason = cls.is_available()
        out["providers"][name] = {
            "available": ok,
            "reason": reason,
            "model": cls.model,
            "dim": cls.dim,
            "price_per_m_tokens": cls.price_per_m,
        }
    # Mirror get_provider's selection logic
    if requested and out["providers"].get(requested, {}).get("available"):
        out["selected"] = requested
    else:
        for name in ("local", "openai", "voyage"):
            if out["providers"][name]["available"]:
                out["selected"] = name
                break
    return out
