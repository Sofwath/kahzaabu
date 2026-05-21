# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 2 — Q&A decomposition (ADR 0001, AVeriTeC-shaped).

For each checkable claim, ask an LLM: "what 2-5 questions would a
researcher need to answer to verify this claim?". Each question is
later answered against the archive / web / constitution / manifesto
(Slice 5 — verify refactor). The decomposition makes the verification
chain explicit and citable — the same shape as the AVeriTeC benchmark
and the RAGAR Chain-of-RAG flow.

This module does ONLY the decomposition (writes claim_questions rows
with `answer = NULL`). Answers are filled by a later pipeline stage.

Run:
    .venv/bin/kahzaabu decompose --limit 20         # dry-run sample
    .venv/bin/kahzaabu decompose                    # full backfill
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
from . import metrics

logger = logging.getLogger("kahzaabu")

# Haiku 4.5 is more than capable of structured-decomposition tasks and
# is ~6× cheaper than Sonnet. Switch to Sonnet only if the dry-run
# shows quality issues.
MODEL = "claude-haiku-4-5"
PRICE_IN_PER_M = 1.0
PRICE_OUT_PER_M = 5.0

SYSTEM = """You are a fact-checking research planner.

Given ONE extracted political claim (its quoted text + extracted fields),
produce a JSON array of 2-5 SUB-QUESTIONS that a journalist or researcher
would need to answer in order to verify (or refute) the claim. The
verification itself happens later — your job is ONLY to enumerate what
must be checked.

Output rules:

1. Each question must be a single concrete factual question with a
   definite answer that can be looked up in a published source.
2. Cover the claim's verification surface: (a) was it actually said?
   (b) was the specific number / date / actor accurate as stated?
   (c) what does the current/historical record show on the same point?
   (d) has the speaker contradicted this elsewhere?
3. Avoid meta-questions ("is this misleading?"), opinion questions
   ("is this a good policy?"), and unanswerable questions. The
   AVeriTeC benchmark treats unanswerable items separately; we don't
   generate them up front.
4. Questions should be ORDERED — the most determinative one first.
5. 2 questions for narrow numeric claims; 4-5 for compound or
   credit-claim style claims.

For each question include:
  "question"     : the question text
  "answer_type"  : one of "Abstractive" | "Extractive" | "Boolean"
                   — the expected SHAPE of an eventual answer.
                   - Boolean: yes/no questions
                   - Extractive: a specific number, date, name pulled
                     from a source verbatim
                   - Abstractive: a synthesis that summarizes evidence
  "source_medium": one of "archive" | "web_search" | "constitution" |
                   "manifesto"
                   — where the answer should be sought.
                   - archive: kahzaabu's press-release corpus
                   - manifesto: the 2023 campaign promises table
                   - constitution: the parsed Constitution of the Maldives
                   - web_search: open-web verification needed

Return STRICT JSON: {"questions": [...]}. No commentary, no markdown."""

USER_TEMPLATE = """Claim ID: {claim_id}
From article: "{article_title}" ({article_date}, {article_category})
Claim type: {type}
Polarity: {polarity}
Subject: {subject_normalized}
Subject (raw): {subject}
Value: {value}
Deadline: {deadline}
Quote:
\"\"\"{quote}\"\"\"

Decompose into 2-5 verification sub-questions. Return JSON only."""

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _decompose_one(client, claim: dict, retries: int = 3) -> dict:
    """Returns {'questions': [...], '_in': N, '_out': N} or
    {'_error': str}."""
    import anthropic
    user = USER_TEMPLATE.format(
        claim_id=claim["id"],
        article_title=claim.get("title") or "(untitled)",
        article_date=(claim.get("published_date") or "")[:10],
        article_category=claim.get("category") or "press_release",
        type=claim.get("type") or "?",
        polarity=claim.get("polarity") or "(not enriched)",
        subject_normalized=claim.get("subject_normalized") or "(not enriched)",
        subject=claim.get("subject") or "?",
        value=claim.get("value") or "(none)",
        deadline=claim.get("deadline") or "(none)",
        quote=(claim.get("quote") or "")[:400],
    )
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=1500, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = r.content[0].text
            m = JSON_RE.search(text)
            if not m:
                return {"_error": "no JSON found", "_raw": text[:200],
                        "_in": r.usage.input_tokens,
                        "_out": r.usage.output_tokens}
            d = json.loads(m.group(0))
            qs = d.get("questions", [])
            if not isinstance(qs, list):
                return {"_error": "questions not a list",
                        "_in": r.usage.input_tokens,
                        "_out": r.usage.output_tokens}
            return {"questions": qs,
                    "_in": r.usage.input_tokens,
                    "_out": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"_error": str(e)[:200]}
            time.sleep(2 ** attempt)
    return {"_error": "exhausted retries"}


@metrics.tracked_stage("decomposer", model="claude-haiku-4-5")
def run_decomposition(conn, *, limit: Optional[int] = None,
                       budget_usd: float = 1.0,
                       concurrency: int = 6,
                       progress_cb=None) -> dict:
    """Decompose claims that don't yet have claim_questions rows.

    Returns {run_id, claims_processed, questions_generated, errors,
             tokens_in, tokens_out, cost_usd}.
    """
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    claims_db.init_claims_schema(conn)

    rows = claims_db.claims_missing_decomposition(conn, limit=limit)
    todo = [dict(r) for r in rows]
    if not todo:
        logger.info("decompose: no claims need decomposition")
        return {"claims_processed": 0, "questions_generated": 0,
                "cost_usd": 0.0}

    logger.info(f"decompose: {len(todo)} claims to process "
                f"(budget cap: ${budget_usd:.2f})")

    import anthropic
    client = anthropic.Anthropic()
    run_id = claims_db.start_decomposition_run(conn, MODEL)

    tokens_in = tokens_out = 0
    n_questions = 0
    n_errors = 0
    n_done = 0

    def worker(idx):
        return idx, _decompose_one(client, todo[idx])

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i) for i in range(len(todo))]
            for fut in as_completed(futures):
                idx, r = fut.result()
                tokens_in += r.get("_in") or 0
                tokens_out += r.get("_out") or 0
                if r.get("_error"):
                    n_errors += 1
                    logger.warning(
                        f"  claim {todo[idx]['id']}: {r['_error']}")
                elif r.get("questions"):
                    n_q = claims_db.insert_claim_questions(
                        conn, run_id, todo[idx]["id"], r["questions"])
                    n_questions += n_q
                n_done += 1
                cost = (tokens_in / 1e6 * PRICE_IN_PER_M
                        + tokens_out / 1e6 * PRICE_OUT_PER_M)
                if progress_cb:
                    progress_cb(n_done, len(todo), tokens_in, tokens_out,
                                 cost, n_questions)
                if cost >= budget_usd:
                    logger.warning(
                        f"decompose: budget cap hit (${cost:.2f}); "
                        f"stopping after {n_done} claims")
                    break
    except KeyboardInterrupt:
        cost = (tokens_in / 1e6 * PRICE_IN_PER_M
                + tokens_out / 1e6 * PRICE_OUT_PER_M)
        claims_db.finish_decomposition_run(
            conn, run_id, claims_processed=n_done,
            questions_generated=n_questions, errors=n_errors,
            tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost, status="interrupted")
        raise

    cost = (tokens_in / 1e6 * PRICE_IN_PER_M
            + tokens_out / 1e6 * PRICE_OUT_PER_M)
    claims_db.finish_decomposition_run(
        conn, run_id, claims_processed=n_done,
        questions_generated=n_questions, errors=n_errors,
        tokens_in=tokens_in, tokens_out=tokens_out,
        cost_usd=cost, status="completed")
    logger.info(
        f"decompose done: {n_done} claims, {n_questions} questions, "
        f"{n_errors} errors, cost=${cost:.3f}")
    return {
        "run_id": run_id,
        "claims_processed": n_done,
        "questions_generated": n_questions,
        "errors": n_errors,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
    }
