# SPDX-License-Identifier: Apache-2.0
"""Incremental claim extraction — pulls articles without claims and extracts."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import anthropic

from . import claims_db
from . import metrics

logger = logging.getLogger("kahzaabu")

MODEL = "claude-sonnet-4-6"
TRUNC_PR = 4000
TRUNC_SPEECH = 8000
PRICE_IN_PER_M = 3.0
PRICE_OUT_PER_M = 15.0

SYSTEM = """You are a forensic fact-extraction analyst working on Maldives Presidency press releases.

For each article, extract SPECIFIC, CHECKABLE claims that could later be verified, contradicted, or compared. Skip pure rhetoric.

Claim types (the existing taxonomy — pick the closest):
- "numeric_promise"        : a number+subject the govt commits to
- "deadline_promise"       : something promised by a specific date or timeframe
- "numeric_update"         : reporting a current status number
- "credit_claim"           : taking credit for delivering / inaugurating / completing something
- "policy_assertion"       : a definite factual claim about state of policy/economy/diplomacy
- "denial"                 : explicit denial / refutation of an allegation
- "boast"                  : superlative comparison
- "comparison_to_predecessor" : framing about what previous govt did or didn't do

POLARITY (V2 — required, one of these six labels per claim):
- "AFFIRM"             : asserts something IS, will be, or has been the case
                         (e.g. "We are building 5,000 housing units")
- "DENY"               : asserts something is NOT / will not / has not been
                         (e.g. "We will not raise taxes")
- "PROMISE"            : future-tense commitment WITH a specific target
                         — numeric, dated, or both
                         (e.g. "We will deliver 12,000 flats by end of 2025")
- "DENIAL_OF_PROMISE"  : explicit disavowal of a prior commitment
                         (e.g. "I never promised that 12,000 figure")
- "CLAIM_OF_FACT"      : past/present factual assertion NOT tied to the
                         speaker's own action
                         (e.g. "The economy grew 4% last year")
- "NEUTRAL"            : ceremonial / rhetorical / acknowledgement;
                         no checkable substantive content
                         (e.g. "I thank the people of Gulhi for their hospitality")

SUBJECT NORMALIZATION (V2 — required):
"subject_normalized" is the entity-resolved subject — collapse all references
to the same actor into one canonical form. Examples:
  "the President" / "Muizzu" / "Dr Mohamed Muizzu" / "His Excellency"
        → "President Muizzu"
  "MTCC" / "the Maldives Transport and Contracting Company"
        → "MTCC"
  "the government" / "the State" / "this Administration"
        → "the government"
  "the previous government" / "the prior administration" / "MDP government"
        → "the previous government"

IS_CHECKABLE (V2 — required, boolean):
  true  : the claim makes a verifiable factual assertion
  false : it's ceremonial / rhetorical / hyperbolic — not checkable in principle
PolitiFact rule: opinions and rhetorical flourish are NOT fact-checkable.
NEUTRAL claims should have is_checkable=false; everything else true by default.

For each claim include:
  "type", "subject", "value" (string or null), "deadline" (string or null),
  "actor_credited", "quote" (verbatim, <=200 chars),
  "polarity"           (one of the 6 labels above),
  "subject_normalized" (entity-resolved string),
  "is_checkable"       (true | false)

Return STRICT JSON: {"claims": [...]}. Empty array if no specific claims.

Be conservative — vague aspirations are NOT claims; skip them.
Be liberal on numbers — every specific number with a subject is a claim worth recording."""

USER_TEMPLATE = """Article ID: {id}
Date: {date}
Category: {category}
Title: {title}

Body:
\"\"\"
{body}
\"\"\"

Extract claims as JSON: {{"claims": [...]}}.
Return ONLY the JSON object."""

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _trim_body(text: str, category: str) -> str:
    limit = TRUNC_SPEECH if category in ("speech", "vp_speech") else TRUNC_PR
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_period = cut.rfind(". ")
    return cut[: last_period + 1] if last_period > limit * 0.7 else cut


def _extract_one(client: anthropic.Anthropic, article: dict, retries: int = 3) -> dict:
    body = _trim_body(article["body_text"], article["category"])
    user = USER_TEMPLATE.format(
        id=article["id"], date=article["published_date"][:10],
        category=article["category"], title=article["title"], body=body,
    )
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=2500, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = r.content[0].text
            m = JSON_RE.search(text)
            if not m:
                return {"claims": [], "_parse_error": True,
                        "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
            d = json.loads(m.group(0))
            return {"claims": d.get("claims", []),
                    "_in": r.usage.input_tokens, "_out": r.usage.output_tokens}
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"claims": [], "_error": str(e)[:200]}
            time.sleep(2 ** attempt)
    return {"claims": [], "_error": "exhausted retries"}


@metrics.tracked_stage("extractor", model="claude-sonnet-4-6")
def run_extraction(conn, *, since_date: Optional[str] = "2023-11-17",
                   limit: Optional[int] = None, concurrency: int = 6,
                   daily_budget_usd: float = 1.0,
                   progress_cb=None) -> dict:
    """Extract claims for articles in the DB that don't have claims yet.

    Returns summary dict with counts and cost.
    """
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    claims_db.init_claims_schema(conn)

    today_spent = claims_db.daily_spend(conn)
    if today_spent >= daily_budget_usd:
        logger.warning(f"daily budget ${daily_budget_usd:.2f} already spent (${today_spent:.2f}); skipping extraction")
        return {"skipped": True, "reason": "budget", "today_spent": today_spent}

    todo_rows = claims_db.articles_missing_claims(
        conn, since_date=since_date, limit=limit,
    )
    todo = [dict(r) for r in todo_rows]
    if not todo:
        logger.info("no articles missing claims; nothing to do")
        return {"articles_processed": 0, "claims_extracted": 0, "cost_usd": 0.0}

    logger.info(f"extraction: {len(todo)} articles to process "
                f"(budget remaining: ${daily_budget_usd - today_spent:.2f})")

    run_id = claims_db.start_extraction_run(conn)
    client = anthropic.Anthropic()

    tokens_in = tokens_out = 0
    n_claims = 0
    n_errors = 0
    n_done = 0

    def worker(idx):
        return idx, _extract_one(client, todo[idx])

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i) for i in range(len(todo))]
            for fut in as_completed(futures):
                idx, r = fut.result()
                art = todo[idx]
                if r.get("_error") or r.get("_parse_error"):
                    n_errors += 1
                tokens_in += r.get("_in") or 0
                tokens_out += r.get("_out") or 0
                if r.get("claims"):
                    n_claims += claims_db.insert_claims(
                        conn, run_id, art["id"], art["language"], r["claims"]
                    )
                else:
                    # Insert a sentinel so we don't re-extract this article.
                    # Use a marker claim of type 'no_specific_claims' with no quote.
                    claims_db.insert_claims(
                        conn, run_id, art["id"], art["language"],
                        [{"type": "no_specific_claims", "subject": None,
                          "value": None, "deadline": None,
                          "actor_credited": None, "quote": None}],
                    )
                n_done += 1
                cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
                if progress_cb:
                    progress_cb(n_done, len(todo), tokens_in, tokens_out, cost)
                # Budget check mid-run
                if cost + today_spent >= daily_budget_usd:
                    logger.warning(f"budget hit (${cost + today_spent:.2f} >= ${daily_budget_usd}); stopping")
                    break
    except KeyboardInterrupt:
        cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
        claims_db.finish_extraction_run(
            conn, run_id, articles_processed=n_done, claims_extracted=n_claims,
            errors=n_errors, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost, status="interrupted",
        )
        raise

    cost = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
    claims_db.finish_extraction_run(
        conn, run_id, articles_processed=n_done, claims_extracted=n_claims,
        errors=n_errors, tokens_in=tokens_in, tokens_out=tokens_out,
        cost_usd=cost, status="completed",
    )
    logger.info(f"extraction done: {n_done} articles, {n_claims} claims, "
                f"{n_errors} errors, cost=${cost:.2f}")
    return {
        "run_id": run_id,
        "articles_processed": n_done,
        "claims_extracted": n_claims,
        "errors": n_errors,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
    }
