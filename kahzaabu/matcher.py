# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 3 — canonical claim matching (ADR 0003).

Two-phase pipeline:

1. EMBED — every checkable claim gets a vector via OpenAI's
   text-embedding-3-small. Cached as a BLOB in claim_embeddings.
   Cost: ~$0.02 per 1M input tokens; ~9,000 claims × ~80 tokens
   ≈ $0.015. Negligible.

2. MATCH — for each newly-embedded claim, compare against all prior
   claims in the same subject_normalized bucket (cheap SQL pre-
   filter). Cosine similarity ≥ 0.85 is a candidate. Entity overlap
   ≥ 0.6 confirms; otherwise an LLM tiebreaker fires (handles
   paraphrase with novel entities, e.g. "5,000 flats in Malé" vs
   "5,000 flats in Hulhumalé" — same shape, different specific).

Idempotent — re-running picks up only unembedded / unmatched claims.

Run:
    .venv/bin/kahzaabu match [--limit N] [--budget X]
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from . import claims_db
from .embeddings import get_provider, EmbeddingProvider

logger = logging.getLogger("kahzaabu")

# Embedding model + dim are now provider-supplied (ADR 0007).
# We keep these constants for legacy tests that need a known dim;
# production code asks the active provider.
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBED_DIM = 384

# Back-compat names (some tests import these directly):
EMBED_MODEL = DEFAULT_EMBED_MODEL
EMBED_DIM = DEFAULT_EMBED_DIM

LLM_MODEL = "claude-haiku-4-5"
LLM_PRICE_IN_PER_M = 1.0
LLM_PRICE_OUT_PER_M = 5.0

COSINE_THRESHOLD = 0.85
ENTITY_OVERLAP_THRESHOLD = 0.60


# ────────────────────────────────────────────────────────────────────
# Vector ops (numpy-free fallback, but use numpy when available)
# ────────────────────────────────────────────────────────────────────

def pack_vector(vec) -> bytes:
    """Pack a float32 vector (list, tuple, or numpy array) into bytes."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(b: bytes) -> list[float]:
    """Unpack a float32 BLOB back into a Python list."""
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


def cosine(a, b) -> float:
    """Cosine similarity. Works on lists OR numpy arrays."""
    try:
        import numpy as np
        a = np.asarray(a, dtype=np.float32)
        b = np.asarray(b, dtype=np.float32)
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
    except ImportError:
        # Pure Python fallback
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0


# ────────────────────────────────────────────────────────────────────
# Entity extraction (regex-based; sufficient for our domain)
# ────────────────────────────────────────────────────────────────────

# Catches: numbers ("5,000"), atoll/island names (capitalized words ≥4 chars),
# dates ("2025", "by 2028", "March 2026"), MVR/USD amounts, key actor terms.
_ENTITY_PATTERNS = [
    re.compile(r"\b\d{1,3}(?:[,]\d{3})+(?:\.\d+)?\b"),       # 5,000 / 12,940 / 1,234,567
    re.compile(r"\b\d{4,}\b"),                                # 2025, 9175
    re.compile(r"\b(?:MVR|USD|US\$|\$)\s?\d+(?:[.,]\d+)*\s?(?:million|billion|m|bn)?\b", re.I),
    # Capitalized phrases — Unicode-aware so 'Hulhumalé' / 'Malé' / etc.
    # are caught. [^\W\d_] = "any Unicode letter".
    re.compile(r"\b[A-Z][^\W\d_]{2,}(?:\s+[A-Z][^\W\d_]+)*\b", re.UNICODE),
    re.compile(r"\b\d+\s*(?:months?|years?|weeks?|days?)\b", re.I),
    re.compile(r"\b(?:by|before|in)\s+\d{4}\b", re.I),       # by 2028
]
_STOPWORDS = {
    "The", "A", "An", "This", "That", "These", "Those", "President",
    "Maldives", "Government", "His", "Her", "Excellency", "Dr",
    "Hon", "Mr", "Mrs", "Honourable",
}


def extract_entities(text: str) -> set[str]:
    """Pull number/date/proper-noun entities for overlap comparison."""
    if not text:
        return set()
    out: set[str] = set()
    for pat in _ENTITY_PATTERNS:
        for m in pat.findall(text):
            tok = m.strip()
            if tok and tok not in _STOPWORDS and len(tok) >= 2:
                out.add(tok)
    return out


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ────────────────────────────────────────────────────────────────────
# Embedding (OpenAI)
# ────────────────────────────────────────────────────────────────────

def embed_batch(texts: list[str],
                 provider: EmbeddingProvider | None = None) -> tuple:
    """Embed N texts via the active provider. Returns
    (vectors, tokens, model, dim, cost_usd)."""
    p = provider or get_provider()
    batch = p.embed(texts)
    return (batch.vectors, batch.tokens, batch.model,
             batch.dim, batch.cost_usd)


# ────────────────────────────────────────────────────────────────────
# Embedding pass
# ────────────────────────────────────────────────────────────────────

def _embed_text_for_claim(claim: dict) -> str:
    """The string we actually embed. Combines subject + quote so similar
    claims about different subjects don't false-match on quote alone."""
    parts = []
    if claim.get("subject_normalized"):
        parts.append(claim["subject_normalized"])
    elif claim.get("subject"):
        parts.append(claim["subject"])
    if claim.get("quote"):
        parts.append(claim["quote"])
    return " | ".join(parts) or "(empty)"


def run_embedding(conn, *, limit: Optional[int] = None,
                   batch_size: int = 100,
                   budget_usd: float = 5.0,
                   provider_name: Optional[str] = None,
                   progress_cb=None) -> dict:
    """Embed all checkable claims that don't yet have an embedding.
    Idempotent. Provider auto-selected via embeddings.get_provider()
    unless `provider_name` is passed (test override or explicit user
    pick). Returns {claims_embedded, tokens, cost_usd, model, dim}."""
    claims_db.init_claims_schema(conn)

    provider = get_provider(name=provider_name)
    logger.info("matcher.embed: provider=%s model=%s dim=%d",
                 type(provider).__name__, provider.model, provider.dim)

    rows = claims_db.claims_missing_embedding(conn, limit=limit)
    todo = [dict(r) for r in rows]
    if not todo:
        logger.info("matcher.embed: no claims need embedding")
        return {"claims_embedded": 0, "tokens": 0, "cost_usd": 0.0,
                "model": provider.model, "dim": provider.dim}

    total_tokens = 0
    total_cost = 0.0
    n_embedded = 0
    for batch_start in range(0, len(todo), batch_size):
        batch = todo[batch_start:batch_start + batch_size]
        texts = [_embed_text_for_claim(c) for c in batch]
        vectors, tokens, model, dim, cost = embed_batch(texts, provider)
        total_tokens += tokens
        total_cost += cost
        for c, v in zip(batch, vectors):
            claims_db.upsert_claim_embedding(
                conn, c["id"], pack_vector(v), model, dim
            )
            n_embedded += 1
        if progress_cb:
            progress_cb(n_embedded, len(todo), total_tokens, total_cost)
        if total_cost >= budget_usd:
            logger.warning(f"matcher.embed: budget hit (${total_cost:.4f})")
            break

    return {"claims_embedded": n_embedded, "tokens": total_tokens,
            "cost_usd": total_cost,
            "model": provider.model, "dim": provider.dim}


# ────────────────────────────────────────────────────────────────────
# Match pass — candidate shortlist via cosine, confirm via entity / LLM
# ────────────────────────────────────────────────────────────────────

def _candidate_pool(conn, claim, *, embed_model: Optional[str] = None
                     ) -> list[dict]:
    """Earlier claims in the SAME subject_normalized bucket AND embedded
    with the SAME model (cross-model cosine is meaningless). If the claim
    lacks subject_normalized (V1 legacy), fall back to subject."""
    bucket = claim.get("subject_normalized") or claim.get("subject")
    if not bucket:
        return []
    sql = """SELECT c.id, c.quote, c.subject, c.subject_normalized,
                    ce.vector, ce.model
             FROM claims c
             JOIN claim_embeddings ce ON ce.claim_id = c.id
             WHERE c.language = 'EN'
               AND c.id < ?
               AND (c.subject_normalized = ? OR c.subject = ?)
               AND c.type != 'no_specific_claims'"""
    params: list = [claim["id"], bucket, bucket]
    if embed_model is not None:
        sql += " AND ce.model = ?"
        params.append(embed_model)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _llm_tiebreaker(claim_a: dict, claim_b: dict) -> bool:
    """Ask the LLM: are these two claims paraphrases of the same proposition?
    Returns True iff yes. Used when embedding similarity is high but
    entity overlap is low."""
    import anthropic
    client = anthropic.Anthropic()
    prompt = (
        "You are a fact-checking deduplication tool. Decide whether the "
        "two political claims below assert the SAME proposition — i.e. a "
        "responsible journalist would treat them as one repeated claim, "
        "not two distinct claims. Different speakers, dates, or wording "
        "are fine if the propositional content is identical.\n\n"
        f"Claim A: {claim_a.get('quote', '')!r}\n"
        f"Claim B: {claim_b.get('quote', '')!r}\n\n"
        "Reply with exactly one word: SAME or DIFFERENT."
    )
    r = client.messages.create(
        model=LLM_MODEL, max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    text = r.content[0].text.strip().upper()
    return text.startswith("SAME"), r.usage.input_tokens, r.usage.output_tokens


def find_match(conn, claim) -> tuple[Optional[int], str]:
    """Find the canonical claim for `claim`. Returns (canonical_id, reason)
    where reason is 'embed+entity' | 'embed+llm' | 'self' (no match found,
    claim becomes its own canonical) | 'no-bucket' (couldn't compute).
    """
    emb = claims_db.get_claim_embedding(conn, claim["id"])
    if emb is None:
        return None, "no-embedding"
    vec_bytes, model, _dim = emb
    vec = unpack_vector(vec_bytes)

    pool = _candidate_pool(conn, claim, embed_model=model)
    if not pool:
        return claim["id"], "self"   # first claim in its bucket

    # Score each candidate
    claim_entities = extract_entities(claim.get("quote", ""))
    best = None  # (similarity, candidate)
    for cand in pool:
        cv = unpack_vector(cand["vector"])
        sim = cosine(vec, cv)
        if sim >= COSINE_THRESHOLD:
            if best is None or sim > best[0]:
                best = (sim, cand)

    if best is None:
        return claim["id"], "self"

    sim, cand = best
    cand_entities = extract_entities(cand.get("quote", ""))
    overlap = jaccard(claim_entities, cand_entities)

    if overlap >= ENTITY_OVERLAP_THRESHOLD:
        # Walk to the candidate's canonical id (it may itself point to
        # an earlier claim — collapse the chain).
        cid_row = conn.execute(
            "SELECT canonical_claim_id FROM claims WHERE id = ?",
            (cand["id"],),
        ).fetchone()
        cid = cid_row[0] if cid_row else None
        return (cid or cand["id"]), "embed+entity"

    # Embed match, entity miss → LLM tiebreaker
    is_same, _, _ = _llm_tiebreaker(claim, cand)
    if is_same:
        cid_row = conn.execute(
            "SELECT canonical_claim_id FROM claims WHERE id = ?",
            (cand["id"],),
        ).fetchone()
        cid = cid_row[0] if cid_row else None
        return (cid or cand["id"]), "embed+llm"
    return claim["id"], "self"


def run_matching(conn, *, limit: Optional[int] = None,
                  budget_usd: float = 5.0,
                  progress_cb=None) -> dict:
    """Walk every embedded claim that doesn't yet have canonical_claim_id
    set, find its canonical id, persist.

    Idempotent — re-running picks up unset canonicals only."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        # We may need the LLM tiebreaker; refuse without a key rather
        # than silently downgrading.
        raise RuntimeError("ANTHROPIC_API_KEY not set (needed for tiebreaker)")
    claims_db.init_claims_schema(conn)

    run_id = claims_db.start_matching_run(
        conn, embed_model=EMBED_MODEL, llm_model=LLM_MODEL,
    )

    sql = """
        SELECT c.id, c.quote, c.subject, c.subject_normalized, c.polarity
        FROM claims c
        JOIN claim_embeddings ce ON ce.claim_id = c.id
        WHERE c.language = 'EN'
          AND c.canonical_claim_id IS NULL
          AND c.type != 'no_specific_claims'
        ORDER BY c.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(conn.execute(sql))

    pairs_compared = 0
    pairs_matched = 0
    llm_tiebreakers = 0
    llm_in = llm_out = 0
    for r in rows:
        cand_pool = _candidate_pool(conn, dict(r))
        pairs_compared += len(cand_pool)
        cid, reason = find_match(conn, dict(r))
        if cid is None:
            continue
        if reason == "embed+llm":
            llm_tiebreakers += 1
        if cid != r["id"]:
            pairs_matched += 1
        claims_db.set_canonical(conn, r["id"], cid)
        if progress_cb:
            llm_cost = (llm_in / 1e6 * LLM_PRICE_IN_PER_M
                        + llm_out / 1e6 * LLM_PRICE_OUT_PER_M)
            progress_cb(pairs_compared, len(rows), pairs_matched,
                         llm_tiebreakers, llm_cost)

    llm_cost = (llm_in / 1e6 * LLM_PRICE_IN_PER_M
                + llm_out / 1e6 * LLM_PRICE_OUT_PER_M)
    claims_db.finish_matching_run(
        conn, run_id,
        claims_embedded=0,
        pairs_compared=pairs_compared,
        pairs_matched=pairs_matched,
        llm_tiebreakers=llm_tiebreakers,
        llm_cost_usd=llm_cost,
        status="completed",
    )
    return {
        "run_id": run_id,
        "claims_processed": len(rows),
        "pairs_compared": pairs_compared,
        "pairs_matched": pairs_matched,
        "llm_tiebreakers": llm_tiebreakers,
        "llm_cost_usd": llm_cost,
    }
