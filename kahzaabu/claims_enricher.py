# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 4 prep — backfill polarity / subject_normalized / is_checkable
for existing claims that pre-date Slice 1's extractor enrichment.

Going forward, the extractor populates these fields directly (ADR 0002).
For the ~9,000 claims extracted before Slice 1 landed, this module
makes the same labels in a single LLM pass — batched 20 claims at a
time to amortize the prompt overhead.

Cost: Haiku 4.5 at ~$0.003 per batch of 20 → ~$1.50 for the full backfill.
Budget-capped, idempotent (skips claims that already have polarity set).

Run:
    .venv/bin/kahzaabu enrich-claims [--limit N] [--budget X]
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from . import claims_db
from . import pricing

logger = logging.getLogger("kahzaabu")

MODEL = pricing.MODELS["haiku"].id

SYSTEM = """You enrich political claims with structured labels for downstream
contradiction-detection. For each input claim, output three fields:

1. polarity (REQUIRED) — exactly one of:
   - "AFFIRM"             asserts something IS/will be/has been the case
   - "DENY"               asserts something is NOT/will not be/has not been
   - "PROMISE"            future-tense commitment WITH a specific target
                          (number, date, or both)
   - "DENIAL_OF_PROMISE"  explicit disavowal of a prior commitment
   - "CLAIM_OF_FACT"      past/present factual assertion not tied to the
                          speaker's own action
   - "NEUTRAL"            ceremonial / rhetorical / acknowledgement only

2. subject_normalized (REQUIRED, string) — canonical actor name. Collapse:
   "the President" / "Muizzu" / "Dr Mohamed Muizzu"   → "President Muizzu"
   "MTCC" / "Maldives Transport and Contracting Co."  → "MTCC"
   "the government" / "the State" / "this Administration" → "the government"
   "previous government" / "MDP government"           → "the previous government"

3. is_checkable (REQUIRED, true/false) — is this a verifiable factual
   assertion (true) or ceremonial / opinion / hyperbole (false)? NEUTRAL
   polarity claims are always is_checkable=false.

Return STRICT JSON: {"results": [{"claim_id": N, "polarity": "X",
"subject_normalized": "Y", "is_checkable": true|false}, ...]}.
One result per input claim, in the same order. No commentary."""

USER_TEMPLATE = """Claims to enrich:

{claims_block}

Return JSON only."""

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _format_batch(claims: list[dict]) -> str:
    lines = []
    for c in claims:
        q = (c.get("quote") or "").replace("\n", " ").strip()[:300]
        lines.append(
            f"- claim_id={c['id']}  type={c.get('type','?')}  "
            f"subject={(c.get('subject') or '?')[:60]}\n"
            f"  quote: {q!r}"
        )
    return "\n".join(lines)


def _enrich_batch(client, claims: list[dict], retries: int = 3) -> dict:
    import anthropic
    user = USER_TEMPLATE.format(claims_block=_format_batch(claims))
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=2000, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = r.content[0].text
            m = JSON_RE.search(text)
            if not m:
                return {"_error": "no JSON in response",
                        "_in": r.usage.input_tokens,
                        "_out": r.usage.output_tokens}
            d = json.loads(m.group(0))
            results = d.get("results", [])
            return {"results": results,
                    "_in": r.usage.input_tokens,
                    "_out": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"_error": str(e)[:200]}
            time.sleep(2 ** attempt)
    return {"_error": "exhausted retries"}


def _claims_needing_enrichment(conn, limit: Optional[int] = None) -> list[dict]:
    """Checkable claims (type != no_specific_claims) with NO polarity yet.
    Going through these populates polarity, subject_normalized,
    is_checkable in one pass."""
    sql = """SELECT id, type, subject, quote
             FROM claims
             WHERE language = 'EN'
               AND polarity IS NULL
               AND type IS NOT NULL
               AND type != 'no_specific_claims'
               AND quote IS NOT NULL
             ORDER BY id"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)]


def _apply_enrichment(conn, results: list[dict]) -> int:
    """Apply one batch of {claim_id, polarity, subject_normalized,
    is_checkable} updates. Polarity is validated against
    VALID_POLARITIES; invalid → NULL. is_checkable coerced to 0/1."""
    n = 0
    for r in results:
        cid = r.get("claim_id")
        if cid is None:
            continue
        pol = r.get("polarity")
        if pol is not None:
            pol = str(pol).strip().upper().replace(" ", "_")
            if pol not in claims_db.VALID_POLARITIES:
                pol = None
        sn = r.get("subject_normalized")
        ic = r.get("is_checkable")
        if isinstance(ic, bool):
            ic = int(ic)
        elif isinstance(ic, str):
            ic = 1 if ic.lower() in ("true", "yes", "1") else 0
        elif isinstance(ic, int):
            ic = 1 if ic else 0
        else:
            ic = None
        conn.execute(
            """UPDATE claims
               SET polarity = ?, subject_normalized = ?, is_checkable = ?
               WHERE id = ? AND polarity IS NULL""",
            (pol, sn, ic, cid),
        )
        n += 1
    conn.commit()
    return n


def run_enrichment(conn, *, limit: Optional[int] = None,
                    batch_size: int = 20,
                    concurrency: int = 6,
                    budget_usd: float = 5.0,
                    progress_cb=None) -> dict:
    """Idempotent backfill of polarity/subject_normalized/is_checkable."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    todo = _claims_needing_enrichment(conn, limit=limit)
    if not todo:
        return {"enriched": 0, "cost_usd": 0.0, "tokens": 0}

    batches = [todo[i:i + batch_size]
                for i in range(0, len(todo), batch_size)]
    logger.info(f"enrich: {len(todo)} claims, {len(batches)} batches "
                f"of {batch_size} (budget cap: ${budget_usd:.2f})")

    import anthropic
    client = anthropic.Anthropic()

    tok_in = tok_out = 0
    n_enriched = 0
    n_errors = 0

    def worker(idx):
        return idx, _enrich_batch(client, batches[idx])

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(worker, i) for i in range(len(batches))]
        for fut in as_completed(futures):
            idx, r = fut.result()
            tok_in += r.get("_in") or 0
            tok_out += r.get("_out") or 0
            if r.get("_error"):
                n_errors += 1
                logger.warning(f"  batch {idx}: {r['_error']}")
                continue
            applied = _apply_enrichment(conn, r.get("results") or [])
            n_enriched += applied
            cost = pricing.cost('haiku', tokens_in=tok_in, tokens_out=tok_out)
            if progress_cb:
                progress_cb(n_enriched, len(todo), tok_in, tok_out, cost)
            if cost >= budget_usd:
                logger.warning(f"enrich: budget hit (${cost:.4f})")
                break

    cost = pricing.cost('haiku', tokens_in=tok_in, tokens_out=tok_out)
    return {
        "enriched": n_enriched,
        "errors": n_errors,
        "tokens_in": tok_in,
        "tokens_out": tok_out,
        "cost_usd": cost,
    }
