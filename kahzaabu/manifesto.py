# SPDX-License-Identifier: Apache-2.0
"""Muizzu 2023 manifesto promise extraction + delivery cross-reference.

Two passes:

1. EXTRACT — chunk the Dhivehi/EN manifesto text, ask Sonnet to pull every
   concrete promise as structured JSON (DV verbatim + EN translation +
   category + subject + target + deadline). Store in manifesto_promises.

2. CROSS_REF — for each extracted promise, search the existing
   claims/fact_checks corpus for matching delivery evidence. Assign
   delivery_status ∈ {delivered, in_progress, broken, modified,
   abandoned, unmentioned} and store linked article/fact_check ids.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import anthropic

from . import claims_db
from . import pricing

logger = logging.getLogger("kahzaabu")

MODEL = pricing.MODELS["sonnet"].id
PRICE_IN_PER_M = pricing.MODELS["sonnet"].in_per_m
PRICE_OUT_PER_M = pricing.MODELS["sonnet"].out_per_m

CHUNK_SIZE_CHARS = 6000     # comfortable for Sonnet, leaves headroom
CHUNK_OVERLAP_CHARS = 400   # so promises straddling chunks don't get cut
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


EXTRACT_SYSTEM = """You are extracting concrete, checkable campaign promises from President Muizzu's 2023 election manifesto. The text is mostly Dhivehi (Thaana script) with some English words / numbers mixed in.

For each chunk of text, return a JSON object listing every SPECIFIC promise Muizzu made. Skip rhetorical aspirations like "we will work hard" — only include promises that name a concrete deliverable, location, number, or deadline.

For each promise, output:
{
  "promise_text_dv": "verbatim Dhivehi snippet, <=300 chars",
  "promise_text_en": "your literal English translation, <=300 chars",
  "category": "housing | infrastructure | economy | governance | health | education | tourism | fisheries | religion | foreign_policy | youth | sports | other",
  "subject": "<=80 chars summary noun phrase for matching (e.g. 'Vilimalé tertiary hospital', 'Ras Malé eco-city')",
  "target_value": "quantified target if present (e.g. '100 beds', 'MVR 1 billion', '5,000 housing units'), else null",
  "deadline_stated": "deadline if present ('year 1', 'first 100 days', 'within 5 years', 'by 2028'), else null",
  "section_hint": "any section/topic heading visible in the chunk, else null"
}

Be conservative — only emit promises that are specific enough to be later
verified. Vague statements like "we will ensure justice" are NOT promises.

Return ONLY the JSON object:
{"promises": [...]}"""


CROSS_REF_SYSTEM = """You are a delivery-evaluation analyst. For a given campaign promise from Muizzu's 2023 manifesto, you receive:

1. The promise (DV + EN translation, category, subject, target, deadline).
2. RELATED_CLAIMS — claims extracted from Muizzu administration press releases that mention similar subjects/numbers/locations.
3. RELATED_FACT_CHECKS — fact-checks from the system that touch on similar topics.

Today is 2026-05-19. Muizzu has been in office since 2023-11-17.

Decide the most accurate delivery_status:
- "delivered"   : there is clear evidence the specific promise was completed
- "in_progress" : work has visibly begun and is ongoing
- "modified"    : the promise still exists but the target/scope/deadline has been changed
- "broken"      : the deadline has passed without delivery, OR the project was abandoned, OR it was contradicted
- "abandoned"   : the promise was dropped entirely from later communications (no mentions)
- "unmentioned" : no related evidence in the corpus either way

Return strict JSON:
{
  "delivery_status": "...",
  "rationale": "1-2 sentence reading citing specific article_ids/fact_check_ids/dates",
  "linked_article_ids": [int, ...],
  "linked_fact_check_ids": [int, ...]
}

Be CONSERVATIVE. If the evidence is weak or ambiguous, prefer "unmentioned" or "modified" over "delivered" or "broken". Cite the specific items you relied on."""


# ============================================================================
# CHUNKING
# ============================================================================

def chunk_text(text: str, size: int = CHUNK_SIZE_CHARS,
               overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """Split text into character-window chunks with overlap.

    Tries to break at line/paragraph boundaries near the target boundary so we
    don't slice mid-word.
    """
    text = text.strip()
    if len(text) <= size:
        return [text]
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + size, len(text))
        if end < len(text):
            # Prefer a nearby paragraph break (within 10% of target)
            window = text[end - size // 10: end]
            br = window.rfind("\n\n")
            if br < 0:
                br = window.rfind("\n")
            if br > 0:
                end = end - size // 10 + br
        chunks.append(text[pos:end].strip())
        if end >= len(text):
            break
        pos = max(end - overlap, pos + 1)
    return chunks


# ============================================================================
# EXTRACTION
# ============================================================================

def _extract_chunk(client: anthropic.Anthropic, chunk_idx: int, chunk: str,
                   retries: int = 3) -> dict:
    user = (f"Chunk #{chunk_idx + 1} from Muizzu 2023 manifesto. Extract concrete promises.\n\n"
            f"TEXT:\n\"\"\"\n{chunk}\n\"\"\"\n\n"
            "Return strict JSON: {\"promises\": [...]}")
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=4000, system=EXTRACT_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
            m = JSON_RE.search(text)
            if not m:
                return {"chunk_index": chunk_idx, "promises": [], "_parse_error": True,
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                return {"chunk_index": chunk_idx, "promises": [], "_parse_error": True,
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            return {"chunk_index": chunk_idx, "promises": d.get("promises", []),
                    "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"chunk_index": chunk_idx, "promises": [], "_error": str(e)[:200]}
            time.sleep(2 ** attempt)


def run_extraction(conn: sqlite3.Connection, text: str, *,
                   concurrency: int = 4, daily_budget_usd: float = 10.0,
                   limit_chunks: Optional[int] = None,
                   progress_cb=None) -> dict:
    """Extract promises from full manifesto text. Stores rows in manifesto_promises."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    claims_db.init_claims_schema(conn)

    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        logger.warning(f"daily budget ${daily_budget_usd:.2f} already spent; skipping")
        return {"skipped": True, "reason": "budget", "today_spent": today_spent}

    chunks = chunk_text(text)
    if limit_chunks:
        chunks = chunks[:limit_chunks]
    logger.info(f"manifesto: {len(chunks)} chunks to process "
                f"(budget remaining: ${daily_budget_usd - today_spent:.2f})")

    # Synth a run record
    run_id = conn.execute(
        "INSERT INTO manifesto_runs (started_at, kind) VALUES (?, 'extract')",
        (claims_db.now_iso(),)
    ).lastrowid
    conn.commit()

    client = anthropic.Anthropic()
    tokens_in = tokens_out = 0
    promises_total = 0
    done = 0

    def worker(idx):
        return _extract_chunk(client, idx, chunks[idx])

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i) for i in range(len(chunks))]
            for fut in as_completed(futures):
                res = fut.result()
                idx = res["chunk_index"]
                tokens_in += res.get("_in") or 0
                tokens_out += res.get("_out") or 0
                for p in res.get("promises", []):
                    conn.execute(
                        """INSERT INTO manifesto_promises
                           (section, promise_text_dv, promise_text_en, category, subject,
                            target_value, deadline_stated, chunk_index, extraction_run_id,
                            created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            p.get("section_hint"),
                            (p.get("promise_text_dv") or "")[:1000],
                            (p.get("promise_text_en") or "")[:1000],
                            p.get("category"),
                            (p.get("subject") or "")[:200],
                            p.get("target_value"),
                            p.get("deadline_stated"),
                            idx, run_id, claims_db.now_iso(),
                        ),
                    )
                    promises_total += 1
                conn.commit()
                done += 1
                cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                if progress_cb:
                    progress_cb(done, len(chunks), promises_total, cost)
                if cost + today_spent >= daily_budget_usd:
                    logger.warning(f"budget hit (${cost + today_spent:.2f}); stopping")
                    break
    except KeyboardInterrupt:
        pass

    cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
    conn.execute(
        """UPDATE manifesto_runs SET finished_at = ?, chunks_processed = ?,
           promises_extracted = ?, tokens_in = ?, tokens_out = ?, cost_usd = ?,
           status = 'completed' WHERE id = ?""",
        (claims_db.now_iso(), done, promises_total, tokens_in, tokens_out, cost, run_id),
    )
    conn.commit()
    logger.info(f"extraction done: {done} chunks, {promises_total} promises, ${cost:.2f}")
    return {"run_id": run_id, "chunks": done, "promises": promises_total,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}


# ============================================================================
# CROSS-REFERENCE
# ============================================================================

def _related_evidence(conn: sqlite3.Connection, promise: dict, max_items: int = 12) -> dict:
    """Pull claims + fact_checks that mention similar subjects/keywords."""
    subj = (promise.get("subject") or "")
    target = (promise.get("target_value") or "")
    text = subj + " " + (promise.get("promise_text_en") or "")

    # Extract content words from subject + target for keyword search
    words = set(re.findall(r"[A-Za-z]{4,}", text.lower()))
    # Drop very common words
    stop = {"that", "this", "with", "from", "have", "been", "will", "year", "years",
            "made", "made", "into", "over", "more", "such", "than", "they", "their"}
    words -= stop
    words = list(words)[:8]

    if not words:
        return {"claims": [], "fact_checks": []}

    where_claims = " OR ".join(["c.subject LIKE ?"] * len(words))
    params_c = [f"%{w}%" for w in words]
    claims_rows = conn.execute(
        f"""SELECT c.article_id, c.type, c.subject, c.value, c.deadline, c.quote,
                   a.published_date, a.title
            FROM claims c JOIN articles a ON c.article_id = a.id AND c.language = a.language
            WHERE c.language='EN' AND c.type != 'no_specific_claims'
              AND ({where_claims})
            ORDER BY a.published_date DESC LIMIT ?""",
        params_c + [max_items],
    ).fetchall()

    where_fcs = " OR ".join(["claim LIKE ?"] * len(words))
    fc_rows = conn.execute(
        f"""SELECT id, category, claim_date, claim, what_actually_happened, topic
            FROM fact_checks
            WHERE ({where_fcs})
            ORDER BY claim_date DESC LIMIT ?""",
        params_c + [max_items],
    ).fetchall()

    return {
        "claims": [dict(r) for r in claims_rows],
        "fact_checks": [dict(r) for r in fc_rows],
    }


def _cross_ref_one(client: anthropic.Anthropic, promise: dict, evidence: dict,
                   retries: int = 3) -> dict:
    user = (
        f"PROMISE (from 2023 manifesto):\n{json.dumps(promise, ensure_ascii=False)[:2000]}\n\n"
        f"RELATED_CLAIMS (from administration's own press releases):\n"
        f"{json.dumps(evidence['claims'][:10], ensure_ascii=False)[:6000]}\n\n"
        f"RELATED_FACT_CHECKS (from this archive):\n"
        f"{json.dumps(evidence['fact_checks'][:8], ensure_ascii=False)[:4000]}\n\n"
        "Return the JSON object."
    )
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=1500, system=CROSS_REF_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text")
            m = JSON_RE.search(text)
            if not m:
                return {"_parse_error": True, "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            try:
                d = json.loads(m.group(0))
            except json.JSONDecodeError:
                return {"_parse_error": True, "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            return {"verdict": d, "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"_error": str(e)[:200]}
            time.sleep(2 ** attempt)


def run_cross_ref(conn: sqlite3.Connection, *,
                  limit: Optional[int] = None, concurrency: int = 4,
                  daily_budget_usd: float = 10.0,
                  only_unmentioned: bool = True,
                  progress_cb=None) -> dict:
    """For each promise without a delivery_status assigned (or all), cross-ref evidence."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    claims_db.init_claims_schema(conn)
    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        logger.warning(f"daily budget ${daily_budget_usd:.2f} already spent; skipping")
        return {"skipped": True, "reason": "budget", "today_spent": today_spent}

    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM manifesto_promises"
    if only_unmentioned:
        sql += " WHERE delivery_status = 'unmentioned' OR cross_ref_run_id IS NULL"
    sql += " ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    todo = conn.execute(sql).fetchall()
    if not todo:
        logger.info("no promises need cross-ref")
        return {"processed": 0, "cost_usd": 0.0}

    run_id = conn.execute(
        "INSERT INTO manifesto_runs (started_at, kind) VALUES (?, 'cross_ref')",
        (claims_db.now_iso(),)
    ).lastrowid
    conn.commit()
    client = anthropic.Anthropic()

    tokens_in = tokens_out = 0
    processed = 0
    status_counts: dict[str, int] = {}

    # Build evidence per promise in a single connection (used here in main thread,
    # workers read-only via the cross_ref call only).
    def worker(idx, promise):
        # local read-only connection for evidence gather
        local = sqlite3.connect("data/kahzaabu.db")
        local.row_factory = sqlite3.Row
        try:
            ev = _related_evidence(local, dict(promise))
        finally:
            local.close()
        res = _cross_ref_one(client, dict(promise), ev)
        return idx, promise, res

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i, p) for i, p in enumerate(todo)]
            for fut in as_completed(futures):
                idx, promise, res = fut.result()
                tokens_in += res.get("_in") or 0
                tokens_out += res.get("_out") or 0
                verdict = res.get("verdict") or {}
                status = verdict.get("delivery_status") or "unmentioned"
                status_counts[status] = status_counts.get(status, 0) + 1
                conn.execute(
                    """UPDATE manifesto_promises SET
                         delivery_status = ?,
                         delivery_evidence_json = ?,
                         cross_ref_run_id = ?
                       WHERE id = ?""",
                    (
                        status,
                        json.dumps({
                            "rationale": verdict.get("rationale"),
                            "linked_article_ids": verdict.get("linked_article_ids", []),
                            "linked_fact_check_ids": verdict.get("linked_fact_check_ids", []),
                        }, ensure_ascii=False),
                        run_id, promise["id"],
                    ),
                )
                conn.commit()
                processed += 1
                cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                if progress_cb:
                    progress_cb(processed, len(todo), status_counts, cost)
                if cost + today_spent >= daily_budget_usd:
                    logger.warning(f"budget hit (${cost + today_spent:.2f}); stopping")
                    break
    except KeyboardInterrupt:
        pass

    cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
    conn.execute(
        """UPDATE manifesto_runs SET finished_at = ?, promises_cross_ref = ?,
           tokens_in = ?, tokens_out = ?, cost_usd = ?, status = 'completed' WHERE id = ?""",
        (claims_db.now_iso(), processed, tokens_in, tokens_out, cost, run_id),
    )
    conn.commit()
    logger.info(f"cross-ref done: {processed} promises, statuses={status_counts}, "
                f"cost=${cost:.2f}")
    return {"run_id": run_id, "processed": processed,
            "status_counts": status_counts,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost}
