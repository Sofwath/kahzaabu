# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 4 — contradiction finder (ADR 0004).

The headline V2 feature: machine-checkable contradiction records.

THREE STAGES per run:

  1. SHORTLIST — cheap SQL joins claim pairs of opposite polarity on
     the same subject_normalized bucket. AFFIRM/PROMISE on one side;
     DENY/DENIAL_OF_PROMISE on the other. CLAIM_OF_FACT pairs with
     either. NEUTRAL never pairs. Returns candidate pairs.

  2. CLASSIFY — for each candidate, an LLM (Sonnet — we prioritize
     rigor over speed here) reads both quotes plus claim metadata
     (date, type, surrounding article context) and classifies into
     one of four verdicts:

       CONTRADICTION       hard contradiction, no plausible explanation
       EVOLVING_POSITION   honest revision with acknowledgement
       CONTEXT_CHANGED     external facts shifted (defensible)
       NOT_CONTRADICTORY   polarity-pair false positive (different
                           sub-subjects, scopes, or time windows)

     The LLM produces a `reasoning_chain` JSON: list of {question,
     answer, evidence_citation} pairs walking through how it reached
     the verdict. This is what makes the call defensible.

  3. PERSIST — write contradiction_pairs rows. UNIQUE(claim_a, claim_b)
     keeps the table idempotent. Only `CONTRADICTION` verdict rows
     get propagated to the public fact-check stream in Slice 5.

Idempotent: re-runs skip already-classified pairs (looked up via
UNIQUE constraint).

Run:
    .venv/bin/kahzaabu find-contradictions [--limit N] [--budget X]
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
from . import metrics

logger = logging.getLogger("kahzaabu")

# Sonnet 4.6 for the verdict — this is the most consequential LLM call
# in V2 and quality here directly affects published fact-checks.
MODEL = pricing.MODELS["sonnet"].id

# Bias the SQL shortlist toward the polarity pairings the ADR calls out.
OPPOSITE_POLARITIES = {
    "AFFIRM":            {"DENY", "DENIAL_OF_PROMISE"},
    "PROMISE":           {"DENY", "DENIAL_OF_PROMISE"},
    "DENY":              {"AFFIRM", "PROMISE", "CLAIM_OF_FACT"},
    "DENIAL_OF_PROMISE": {"AFFIRM", "PROMISE", "CLAIM_OF_FACT"},
    "CLAIM_OF_FACT":     {"DENY", "DENIAL_OF_PROMISE"},
    "NEUTRAL":           set(),  # ceremonial; never pairs
}

# Same-day claims are not contradictions — they're the same statement.
# We require at least N days between the two claims.
MIN_DAYS_APART = 1

# Semantic-similarity prefilter. Two claims with opposite polarity on the
# same subject_normalized bucket are CANDIDATES, but most of those pairs
# are about different TOPICS within the bucket (e.g. "the government" can
# affirm housing and deny corruption — same subject, different propositions,
# not a contradiction). The cosine threshold restricts to pairs about the
# SAME proposition.
#
# Calibration on the kahzaabu corpus:
#   ≥ 0.95  paraphrases (same canonical_claim_id; we exclude these as not
#           contradiction-candidates — they ARE the same statement)
#   0.55-0.95  same proposition, different framing — contradiction zone
#   < 0.55  different propositions — likely false positive, drop
#
# 0.55 is the floor where the LLM call is worth making. Tunable; lower
# = more LLM cost but catches more edge cases. Higher = cheaper but misses
# real contradictions where the wording diverged a lot.
MIN_SIMILARITY = 0.55
MAX_SIMILARITY = 0.95   # above this, the two claims are paraphrases of
                         # one another — exclude (they're not opposites)


SYSTEM = """You are evaluating whether two political claims, made at different times,
constitute a contradiction. You will be given:

- Claim A: speaker + date + extracted polarity + quote
- Claim B: speaker + date + extracted polarity + quote
- Subject bucket: the topic both claims address

Classify the pair into EXACTLY ONE of these four verdicts:

1. CONTRADICTION — the two claims logically cannot both be true; the
   speaker has either changed position without acknowledgement OR made
   a false statement. Hard call; high bar.

2. EVOLVING_POSITION — the speaker has changed position AND acknowledged
   the change either in the second claim or in surrounding context.
   This is honest revision, not contradiction.

3. CONTEXT_CHANGED — external facts (court rulings, IMF agreements,
   natural disasters, parliamentary action) shifted in a way that
   defensibly justifies the new position. Pair is preserved for
   transparency but is NOT a contradiction.

4. NOT_CONTRADICTORY — the polarity-pair shortlist false-positived.
   The two claims address different sub-subjects, different time
   windows, different scopes, or have nuance the polarity labels
   missed. Often the most common verdict.

Also produce a `reasoning_chain` — a JSON list of 2-4 {question,
answer, evidence} objects walking through your reasoning. Each
`evidence` field should be a verbatim quote (or `"no direct evidence"`).
The reasoning chain is what makes the verdict defensible to a third
party.

Output STRICT JSON:
{
  "verdict": "CONTRADICTION" | "EVOLVING_POSITION" | "CONTEXT_CHANGED" |
             "NOT_CONTRADICTORY",
  "confidence": 0.0-1.0,
  "reasoning_chain": [
    {"question": "...", "answer": "...", "evidence": "..."},
    ...
  ]
}

Be CONSERVATIVE: when in doubt between CONTRADICTION and any softer
verdict, pick the softer one. Conflating honest revision with lying
undermines the project's credibility."""


USER_TEMPLATE = """Claim A
  date: {a_date}
  type: {a_type}, polarity: {a_polarity}
  subject (raw): {a_subject}
  quote: {a_quote!r}

Claim B
  date: {b_date}
  type: {b_type}, polarity: {b_polarity}
  subject (raw): {b_subject}
  quote: {b_quote!r}

Subject bucket: {subject_bucket}

Both claims are attributed to: {speaker}

Return JSON only."""


JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def shortlist_candidates(conn, *, limit: Optional[int] = None,
                          subject_bucket: Optional[str] = None,
                          min_similarity: float = MIN_SIMILARITY,
                          max_similarity: float = MAX_SIMILARITY,
                          apply_similarity_filter: bool = True,
                          ) -> list[tuple[int, int, str]]:
    """Return list of (claim_a_id, claim_b_id, subject) triples that
    are candidate contradictions per the polarity-pair rules.

    claim_a is always the EARLIER claim. Same-day pairs are excluded
    (MIN_DAYS_APART). Pairs already in contradiction_pairs are excluded
    (UNIQUE guard).

    The subject_normalized field clusters claims for the bucket. If
    `subject_bucket` is set, restrict to that single bucket — useful
    for incremental runs."""
    # Build the set of unordered polarity-pairs, then for each pair
    # match either polarity-ordering (claim_a may be on either side
    # since id-ordering and polarity-ordering are independent).
    unordered_pairs: set[frozenset] = set()
    for left, rights in OPPOSITE_POLARITIES.items():
        for r in rights:
            unordered_pairs.add(frozenset((left, r)))

    candidates: list[tuple[int, int, str]] = []
    for fs in sorted(unordered_pairs, key=lambda s: sorted(s)):
        if len(fs) == 1:
            # self-pair (shouldn't happen given OPPOSITE_POLARITIES, but safe)
            p1 = p2 = next(iter(fs))
        else:
            p1, p2 = sorted(fs)
        sql = """
            SELECT ca.id, cb.id, ca.subject_normalized
            FROM claims ca
            JOIN claims cb
              ON ca.subject_normalized = cb.subject_normalized
             AND ca.subject_normalized IS NOT NULL
             AND ca.id < cb.id
            LEFT JOIN articles aa ON aa.id = ca.article_id AND aa.language = ca.language
            LEFT JOIN articles ab ON ab.id = cb.article_id AND ab.language = cb.language
            LEFT JOIN contradiction_pairs cp
              ON cp.claim_a_id = ca.id AND cp.claim_b_id = cb.id
            WHERE cp.id IS NULL
              AND ((ca.polarity = ? AND cb.polarity = ?) OR
                   (ca.polarity = ? AND cb.polarity = ?))
              AND ca.language = 'EN' AND cb.language = 'EN'
              AND ca.is_checkable = 1 AND cb.is_checkable = 1
              AND aa.published_date IS NOT NULL
              AND ab.published_date IS NOT NULL
              AND ABS(JULIANDAY(ab.published_date) - JULIANDAY(aa.published_date)) >= ?
        """
        params: list = [p1, p2, p2, p1, MIN_DAYS_APART]
        if subject_bucket:
            sql += " AND ca.subject_normalized = ?"
            params.append(subject_bucket)
        sql += " ORDER BY ca.id, cb.id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        for row in conn.execute(sql, params):
            candidates.append((row[0], row[1], row[2]))
    # Deduplicate (some pairs may match via both polarity orderings)
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, str]] = []
    for a, b, s in candidates:
        if (a, b) in seen:
            continue
        seen.add((a, b))
        out.append((a, b, s))

    if not apply_similarity_filter:
        return out

    # Semantic-similarity filter. Without this, "the government" pairs
    # 4,000+ claims-of-fact against 40 denials at the polarity level,
    # producing ~96k candidates that mostly describe different topics.
    # Filter to pairs whose embeddings are close enough to be about the
    # SAME proposition.
    return _filter_by_similarity(conn, out, min_similarity, max_similarity)


def _filter_by_similarity(conn, candidates: list[tuple[int, int, str]],
                           min_sim: float, max_sim: float
                           ) -> list[tuple[int, int, str]]:
    """Drop pairs whose cosine similarity falls outside [min_sim, max_sim].
    Pairs without embeddings are dropped silently (matcher hadn't run on
    them). Reuses matcher.cosine + matcher.unpack_vector to stay in sync."""
    from . import matcher as _m
    # Pre-load all needed embeddings in one query
    ids: set[int] = set()
    for a, b, _ in candidates:
        ids.add(a); ids.add(b)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT claim_id, vector, model FROM claim_embeddings "
        f"WHERE claim_id IN ({placeholders})", sorted(ids),
    ).fetchall()
    vecs: dict[int, tuple[list, str]] = {
        r[0]: (_m.unpack_vector(r[1]), r[2]) for r in rows
    }
    out: list[tuple[int, int, str]] = []
    for a, b, subj in candidates:
        if a not in vecs or b not in vecs:
            continue
        va, ma = vecs[a]
        vb, mb = vecs[b]
        if ma != mb:
            # cross-model cosine is meaningless (ADR 0007)
            continue
        sim = _m.cosine(va, vb)
        if min_sim <= sim <= max_sim:
            out.append((a, b, subj))
    return out


def _load_claim_context(conn, claim_id: int) -> dict:
    """Pull claim + its article date for the LLM prompt."""
    r = conn.execute(
        """SELECT c.id, c.type, c.polarity, c.subject, c.quote,
                  c.subject_normalized, a.published_date, a.title
           FROM claims c
           JOIN articles a ON a.id = c.article_id AND a.language = c.language
           WHERE c.id = ?""",
        (claim_id,),
    ).fetchone()
    if r is None:
        return {}
    return dict(zip([d[0] for d in conn.description] if hasattr(conn, 'description')
                     else ("id", "type", "polarity", "subject", "quote",
                            "subject_normalized", "published_date", "title"),
                     r if not hasattr(r, "keys") else [r[k] for k in r.keys()]))


def _classify_pair(client, a_ctx: dict, b_ctx: dict, subject_bucket: str,
                    retries: int = 3) -> dict:
    """Run one LLM classification call."""
    # claim_a is always earlier — if not, swap.
    if (a_ctx.get("published_date") or "") > (b_ctx.get("published_date") or ""):
        a_ctx, b_ctx = b_ctx, a_ctx
    import anthropic
    user = USER_TEMPLATE.format(
        a_date=(a_ctx.get("published_date") or "?")[:10],
        a_type=a_ctx.get("type") or "?",
        a_polarity=a_ctx.get("polarity") or "?",
        a_subject=(a_ctx.get("subject") or "")[:120],
        a_quote=(a_ctx.get("quote") or "")[:300],
        b_date=(b_ctx.get("published_date") or "?")[:10],
        b_type=b_ctx.get("type") or "?",
        b_polarity=b_ctx.get("polarity") or "?",
        b_subject=(b_ctx.get("subject") or "")[:120],
        b_quote=(b_ctx.get("quote") or "")[:300],
        subject_bucket=subject_bucket,
        speaker="Mohamed Muizzu (President of the Maldives)",
    )
    for attempt in range(retries):
        try:
            r = client.messages.create(
                model=MODEL, max_tokens=1200, system=SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = r.content[0].text
            m = JSON_RE.search(text)
            if not m:
                return {"_error": "no JSON in response",
                        "_in": r.usage.input_tokens,
                        "_out": r.usage.output_tokens}
            d = json.loads(m.group(0))
            verdict = (d.get("verdict") or "").strip().upper()
            if verdict not in claims_db.VALID_CONTRADICTION_VERDICTS:
                return {"_error": f"invalid verdict: {verdict}",
                        "_in": r.usage.input_tokens,
                        "_out": r.usage.output_tokens}
            return {
                "verdict": verdict,
                "confidence": float(d.get("confidence") or 0.5),
                "reasoning_chain": d.get("reasoning_chain") or [],
                "_in": r.usage.input_tokens,
                "_out": r.usage.output_tokens,
            }
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt * 2)
        except Exception as e:
            if attempt == retries - 1:
                return {"_error": str(e)[:200]}
            time.sleep(2 ** attempt)
    return {"_error": "exhausted retries"}


def _persist_pair(conn, run_id: int, claim_a_id: int, claim_b_id: int,
                   subject: str, verdict: str, confidence: float,
                   reasoning_chain: list) -> None:
    from datetime import datetime, timezone
    # The claim_a / claim_b ordering must be a < b at INSERT time.
    a, b = sorted((claim_a_id, claim_b_id))
    conn.execute(
        """INSERT OR IGNORE INTO contradiction_pairs
           (claim_a_id, claim_b_id, subject, verdict, confidence,
            reasoning_chain, finder_run_id, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (a, b, subject, verdict, max(0.0, min(1.0, confidence)),
         json.dumps(reasoning_chain), run_id,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


@metrics.tracked_stage("contradictions", model=MODEL)
def run_finder(conn, *, limit: Optional[int] = None,
                budget_usd: float = 5.0,
                concurrency: int = 4,
                progress_cb=None) -> dict:
    if "ANTHROPIC_API_KEY" not in os.environ:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    claims_db.init_claims_schema(conn)

    # Step 1 — shortlist
    candidates = shortlist_candidates(conn, limit=limit)
    if not candidates:
        return {"shortlisted": 0, "classified": 0, "cost_usd": 0.0}

    logger.info(f"contradictions: shortlisted {len(candidates)} candidate "
                f"pairs (budget cap: ${budget_usd:.2f})")

    import anthropic
    client = anthropic.Anthropic()

    cur = conn.execute(
        "INSERT INTO contradiction_finder_runs (started_at, model) "
        "VALUES (datetime('now'), ?)", (MODEL,),
    )
    run_id = cur.lastrowid
    conn.commit()

    tok_in = tok_out = 0
    classified = 0
    by_verdict = {"CONTRADICTION": 0, "EVOLVING_POSITION": 0,
                  "CONTEXT_CHANGED": 0, "NOT_CONTRADICTORY": 0}

    # Pre-load all claim contexts in one pass (cheap)
    ids = set()
    for a, b, _ in candidates:
        ids.add(a); ids.add(b)
    id_list = sorted(ids)
    placeholders = ",".join("?" * len(id_list))
    rows = conn.execute(
        f"""SELECT c.id, c.type, c.polarity, c.subject, c.quote,
                   c.subject_normalized, a.published_date, a.title
            FROM claims c
            JOIN articles a ON a.id = c.article_id AND a.language = c.language
            WHERE c.id IN ({placeholders})""", id_list,
    ).fetchall()
    ctx_by_id = {r[0]: {
        "id": r[0], "type": r[1], "polarity": r[2], "subject": r[3],
        "quote": r[4], "subject_normalized": r[5],
        "published_date": r[6], "title": r[7],
    } for r in rows}

    def worker(idx):
        a_id, b_id, subj = candidates[idx]
        return idx, _classify_pair(client, ctx_by_id[a_id],
                                    ctx_by_id[b_id], subj)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(worker, i) for i in range(len(candidates))]
            for fut in as_completed(futures):
                idx, r = fut.result()
                tok_in += r.get("_in") or 0
                tok_out += r.get("_out") or 0
                if r.get("_error"):
                    logger.warning(f"  pair {candidates[idx][0]}↔"
                                    f"{candidates[idx][1]}: {r['_error']}")
                    continue
                a_id, b_id, subj = candidates[idx]
                _persist_pair(
                    conn, run_id, a_id, b_id, subj,
                    r["verdict"], r["confidence"], r["reasoning_chain"],
                )
                by_verdict[r["verdict"]] = by_verdict.get(r["verdict"], 0) + 1
                classified += 1
                cost = pricing.cost('sonnet', tokens_in=tok_in, tokens_out=tok_out)
                if progress_cb:
                    progress_cb(classified, len(candidates),
                                 by_verdict["CONTRADICTION"], cost)
                if cost >= budget_usd:
                    logger.warning(f"finder: budget hit (${cost:.4f})")
                    break
    finally:
        cost = pricing.cost('sonnet', tokens_in=tok_in, tokens_out=tok_out)
        conn.execute(
            """UPDATE contradiction_finder_runs
               SET finished_at = datetime('now'),
                   candidates_shortlisted = ?, pairs_classified = ?,
                   contradictions = ?, evolving = ?, context_changed = ?,
                   not_contradictory = ?, tokens_in = ?, tokens_out = ?,
                   cost_usd = ?, status = 'completed'
               WHERE id = ?""",
            (len(candidates), classified,
             by_verdict["CONTRADICTION"], by_verdict["EVOLVING_POSITION"],
             by_verdict["CONTEXT_CHANGED"], by_verdict["NOT_CONTRADICTORY"],
             tok_in, tok_out, cost, run_id),
        )
        conn.commit()

    return {
        "run_id": run_id,
        "shortlisted": len(candidates),
        "classified": classified,
        "by_verdict": by_verdict,
        "tokens_in": tok_in,
        "tokens_out": tok_out,
        "cost_usd": cost,
    }
