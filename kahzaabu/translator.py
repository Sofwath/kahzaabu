# SPDX-License-Identifier: Apache-2.0
"""Press-office-style EN ↔ DV translation (ADR 0016, Slice 16).

The Maldives Presidency Office writes Dhivehi in a distinctive
formal register — "ރައީސުލްޖުމްހޫރިއްޔާ" (not "ޕްރެޒިޑެންޓް") for
"President", specific Thaana renderings for institutional names,
and stock phrasings that a generic LLM translation doesn't
reproduce. This module produces translations in that style by:

  1. Pulling 3 topic-similar paired articles from the last 90 days
     via the articles_fts BM25 index (Slice 16's FTS5; mirrors
     factcheck_search.py's pattern).
  2. Looking up relevant terms from translation_glossary
     (precomputed via build_glossary()'s one-shot batch LLM job).
  3. Composing a system prompt + glossary + few-shot exemplars +
     input text, calling Sonnet at temperature 0.3 for consistency.
  4. Recording every translation to translation_runs — both the
     audit trail and the cache backing store (same input + target
     within 1h returns instantly without a fresh LLM call).

Design rationale + alternatives considered in
docs/adr/0016-style-faithful-translation.md.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from kahzaabu import articles_fts, pricing

logger = logging.getLogger(__name__)


# ── Language detection ──────────────────────────────────────────────

# Thaana script range. https://www.unicode.org/charts/PDF/U0780.pdf
_THAANA_RE = re.compile(r"[ހ-޿]")


def detect_language(text: Optional[str]) -> str:
    """Return 'DV' if the text is dominantly Thaana, else 'EN'.

    Heuristic: >50% of the non-whitespace characters are Thaana →
    DV. Empty / None → EN (default; the caller usually passes a
    non-empty input). Robust to mixed text (a DV body with English
    proper nouns or numbers stays classified as DV)."""
    if not text:
        return "EN"
    non_ws = [c for c in text if not c.isspace()]
    if not non_ws:
        return "EN"
    thaana_n = len(_THAANA_RE.findall(text))
    return "DV" if (thaana_n / len(non_ws)) > 0.5 else "EN"


# ── Few-shot selection ──────────────────────────────────────────────

def select_few_shot(
    conn: sqlite3.Connection,
    source_lang: str,
    query_text: str,
    *,
    k: int = 3,
    recency_days: int = 90,
) -> list[dict]:
    """Hybrid topic + recency: top-k paired articles within
    recency_days that BM25-match the query_text, falling back to
    most-recent paired articles if FTS5 returns fewer than k hits.

    Each returned dict has: en_article_id, dv_article_id,
    en_body, dv_body, en_title, published_date.

    EN ↔ DV is taken from articles.paired_id. We always return the
    EN-side article_id as en_article_id regardless of source_lang
    (the translator builds the prompt symmetrically — both EN body
    and DV body are shown in every example)."""
    # FTS5 search on the SOURCE language. We want similarity to the
    # source text. Then the EN/DV pair is the canonical exemplar.
    hits = articles_fts.search_articles(
        conn, query_text,
        language=source_lang, limit=k * 2,
        require_paired=True, recency_days=recency_days,
    )

    out: list[dict] = []
    seen_ids: set[int] = set()
    for h in hits:
        if len(out) >= k:
            break
        if h["article_id"] in seen_ids:
            continue
        # Resolve the paired counterpart in the OTHER language.
        paired = conn.execute(
            "SELECT id, language, title, body_text, published_date "
            "FROM articles WHERE id = ?", (h["paired_id"],),
        ).fetchone()
        if paired is None:
            continue
        # Skip if either side has no body — paired metadata can
        # exist with NULL bodies (sparse content for some pairs).
        if not h.get("body_text") or not paired[3]:
            continue
        en_art = (h, paired) if h["language"] == "EN" else (paired, h)
        en_row, dv_row = en_art
        en_id = en_row["article_id"] if isinstance(en_row, dict) else en_row[0]
        dv_id = dv_row["article_id"] if isinstance(dv_row, dict) else dv_row[0]
        en_title = en_row["title"] if isinstance(en_row, dict) else en_row[2]
        en_body = en_row["body_text"] if isinstance(en_row, dict) else en_row[3]
        dv_body = dv_row["body_text"] if isinstance(dv_row, dict) else dv_row[3]
        pub_date = (en_row["published_date"] if isinstance(en_row, dict)
                    else en_row[4])
        out.append({
            "en_article_id": en_id,
            "dv_article_id": dv_id,
            "en_title":      en_title or "",
            "en_body":       en_body or "",
            "dv_body":       dv_body or "",
            "published_date": pub_date or "",
        })
        seen_ids.add(en_id)
        seen_ids.add(dv_id)

    # Recency fallback if FTS5 didn't yield enough paired exemplars
    if len(out) < k:
        need = k - len(out)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=recency_days * 4)
                  ).strftime("%Y-%m-%d")
        # Get recent paired EN articles, dedup against already-selected.
        existing = ",".join(str(i) for i in seen_ids) or "0"
        rows = conn.execute(
            f"""SELECT a.id, a.title, a.body_text, a.published_date,
                       p.id AS paired_id, p.body_text AS paired_body
                FROM articles a
                JOIN articles p ON a.paired_id = p.id
                WHERE a.language = 'EN' AND p.language = 'DV'
                  AND a.body_text IS NOT NULL AND a.body_text != ''
                  AND p.body_text IS NOT NULL AND p.body_text != ''
                  AND a.published_date >= ?
                  AND a.id NOT IN ({existing})
                ORDER BY a.published_date DESC
                LIMIT ?""",
            (cutoff, need),
        ).fetchall()
        for r in rows:
            out.append({
                "en_article_id":   r[0],
                "dv_article_id":   r[4],
                "en_title":        r[1] or "",
                "en_body":         r[2] or "",
                "dv_body":         r[5] or "",
                "published_date":  r[3] or "",
            })

    return out[:k]


# ── Glossary subset ──────────────────────────────────────────────────

def select_glossary_subset(
    conn: sqlite3.Connection,
    text: str,
    source_lang: str,
    *,
    max_terms: int = 25,
) -> list[dict]:
    """Return up to max_terms glossary rows whose source-language
    term appears in `text`. Sorted by freq DESC so the highest-
    confidence terms get priority when the prompt's context budget
    is tight.

    Implementation: LIKE prefilter against the indexed source column,
    then exact substring re-check on the Python side (defensive
    against partial-word matches; SQLite LIKE doesn't word-boundary)."""
    if not text:
        return []
    col = "en_term" if source_lang == "EN" else "dv_term"
    # Pre-narrow with LIKE on each non-trivial token to limit the
    # rows we have to check.
    if source_lang == "EN":
        tokens = re.findall(r"[A-Za-z]{4,}", text)
    else:
        # For DV, we extract Thaana runs of length >= 2 (Thaana words
        # are generally short; the glossary stores phrase-level pairs
        # which we'll substring-match below).
        tokens = re.findall(r"[ހ-޿]+", text)
    if not tokens:
        return []
    # Build a single SQL with OR-of-LIKEs against the source column.
    placeholders = " OR ".join([f"{col} LIKE ?"] * min(len(tokens), 30))
    params = [f"%{t}%" for t in tokens[:30]]
    sql = (
        f"SELECT id, en_term, dv_term, domain, freq "
        f"FROM translation_glossary WHERE ({placeholders}) "
        f"ORDER BY freq DESC LIMIT ?"
    )
    params.append(max_terms * 3)
    try:
        candidates = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        logger.debug("glossary subset query failed: %s", e)
        return []

    # Second pass: exact case-insensitive substring match. LIKE
    # could false-match on adjacent words; this confirms.
    text_lower = text.lower()
    out: list[dict] = []
    for r in candidates:
        term = (r[1] if source_lang == "EN" else r[2]) or ""
        if not term:
            continue
        if source_lang == "EN":
            if term.lower() in text_lower:
                out.append({
                    "id": r[0], "en_term": r[1], "dv_term": r[2],
                    "domain": r[3], "freq": r[4],
                })
        else:
            # DV — Thaana doesn't have case; just substring check.
            if term in text:
                out.append({
                    "id": r[0], "en_term": r[1], "dv_term": r[2],
                    "domain": r[3], "freq": r[4],
                })
        if len(out) >= max_terms:
            break
    return out


# ── Prompt assembly ─────────────────────────────────────────────────

_PO_STYLE_NOTES = (
    "The Maldives Presidency Office's Dhivehi register is formal "
    "and institutional. Distinctive markers:\n"
    "  - 'ރައީސުލްޖުމްހޫރިއްޔާ' (NOT 'ޕްރެޒިޑެންޓް') for 'President'\n"
    "  - 'ދައުލަތުގެ ވަޒީރުންގެ މަޖިލިސް' for 'Cabinet'\n"
    "  - 'ރައްޔިތުންގެ މަޖިލިސް' for 'People's Majlis' (Parliament)\n"
    "  - Full institutional names, not abbreviations\n"
    "  - No colloquial Thaana; preserves classical political register\n"
)


def _compose_prompt(
    input_text: str,
    source_lang: str,
    target_lang: str,
    glossary: list[dict],
    exemplars: list[dict],
) -> tuple[str, str]:
    """Return (system_prompt, user_message)."""
    target_name = "Dhivehi (Thaana script)" if target_lang == "DV" else "English"
    source_name = "Dhivehi" if source_lang == "DV" else "English"
    system = (
        f"You translate text from {source_name} to {target_name} in "
        f"the style of the Maldives Presidency Office press releases. "
        f"Match their register, terminology, and idiomatic political "
        f"vocabulary exactly.\n\n"
        f"{_PO_STYLE_NOTES}\n"
        f"Output ONLY the translation. No commentary, no quotes, no "
        f"prefixes like 'Here is the translation:'. Plain text."
    )

    parts: list[str] = []
    if glossary:
        parts.append(
            f"GLOSSARY — terms appearing in the input with the press "
            f"office's preferred translations:"
        )
        for g in glossary:
            parts.append(f"  - \"{g['en_term']}\" ↔ \"{g['dv_term']}\"")
        parts.append("")
    if exemplars:
        parts.append(
            f"EXAMPLES of the press office's {source_name}↔{target_name} "
            f"style (use these as your style guide):"
        )
        for i, ex in enumerate(exemplars, start=1):
            en_snippet = (ex["en_body"] or "")[:600]
            dv_snippet = (ex["dv_body"] or "")[:600]
            parts.append(
                f"\nExample {i} [{ex['published_date']}, art #{ex['en_article_id']}]:"
            )
            parts.append(f"  EN: {en_snippet}")
            parts.append(f"  DV: {dv_snippet}")
        parts.append("")
    parts.append(f"NOW TRANSLATE THIS TO {target_name.upper()}:")
    parts.append("")
    parts.append(input_text)
    return system, "\n".join(parts)


# ── Cache hit ────────────────────────────────────────────────────────

def _cache_lookup(
    conn: sqlite3.Connection,
    input_text: str,
    target_lang: str,
    *,
    ttl_hours: float = 1.0,
) -> Optional[dict]:
    """Return a cached translation_runs row if input + target match
    within the TTL. The slice-16 plan uses translation_runs as the
    LRU backing store so we don't need a separate cache layer."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
              ).isoformat()
    row = conn.execute(
        """SELECT source_lang, target_lang, output_text, exemplar_ids,
                  glossary_terms_used, model, cost_usd, created_at
           FROM translation_runs
           WHERE input_text = ? AND target_lang = ? AND created_at >= ?
           ORDER BY created_at DESC LIMIT 1""",
        (input_text, target_lang, cutoff),
    ).fetchone()
    if row is None:
        return None
    try:
        exemplar_ids = json.loads(row[3] or "[]")
    except (json.JSONDecodeError, TypeError):
        exemplar_ids = []
    return {
        "translation":         row[2],
        "source_lang":         row[0],
        "target_lang":         row[1],
        "exemplar_ids":        exemplar_ids,
        "glossary_terms_used": row[4] or 0,
        "model":               row[5],
        "cost_usd":            row[6] or 0.0,
        "cache_hit":           True,
        "cached_at":           row[7],
    }


# ── Public entry: translate() ───────────────────────────────────────

def translate(
    conn: sqlite3.Connection,
    text: str,
    *,
    target_lang: Optional[str] = None,
    llm: Any = None,
    model_alias: str = "sonnet",
) -> dict:
    """End-to-end translate. See module docstring for the workflow.

    `llm` is an Anthropic client; if None, we construct one from
    ANTHROPIC_API_KEY. (Hermes-plugin callers pass ctx.llm so the
    provider abstraction is honoured.)

    Returns:
      {
        "translation":         str,
        "source_lang":         "EN" | "DV",
        "target_lang":         "EN" | "DV",
        "exemplar_ids":        [int, ...],   # EN article ids used as few-shot
        "glossary_terms_used": int,
        "model":               str,
        "cost_usd":            float,
        "cache_hit":           bool,
        "disclaimer":          str,
      }"""
    if not text or not text.strip():
        raise ValueError("translate(): text is empty")

    source_lang = detect_language(text)
    if target_lang is None or target_lang == "auto":
        target_lang = "DV" if source_lang == "EN" else "EN"
    if target_lang not in ("EN", "DV"):
        raise ValueError(f"target_lang must be 'EN' or 'DV' (got {target_lang!r})")
    if target_lang == source_lang:
        raise ValueError(
            f"source and target languages are both {source_lang} — "
            "detect_language thinks the input is already in the target "
            "language. Specify target_lang explicitly to override."
        )

    # Cache check
    hit = _cache_lookup(conn, text, target_lang)
    if hit is not None:
        hit["disclaimer"] = _DISCLAIMER
        return hit

    # Gather context
    exemplars = select_few_shot(conn, source_lang, text, k=3)
    glossary = select_glossary_subset(conn, text, source_lang, max_terms=20)

    system, user = _compose_prompt(
        text, source_lang, target_lang, glossary, exemplars,
    )

    # Call LLM
    if llm is None:
        import anthropic
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not configured — set it in "
                "~/.hermes/.env or shell env."
            )
        llm = anthropic.Anthropic(api_key=api_key)

    model_id = pricing.model_id(model_alias)
    resp = llm.messages.create(
        model=model_id,
        max_tokens=2000,
        temperature=0.3,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    translation = "".join(
        getattr(b, "text", "")
        for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    tokens_in = getattr(resp.usage, "input_tokens", 0)
    tokens_out = getattr(resp.usage, "output_tokens", 0)
    cost = pricing.cost(model_alias, tokens_in=tokens_in, tokens_out=tokens_out)

    exemplar_ids = [e["en_article_id"] for e in exemplars]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO translation_runs
           (source_lang, target_lang, input_text, output_text,
            exemplar_ids, glossary_terms_used, model, cost_usd,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_lang, target_lang, text, translation,
         json.dumps(exemplar_ids), len(glossary), model_id, cost, now),
    )
    conn.commit()

    return {
        "translation":         translation,
        "source_lang":         source_lang,
        "target_lang":         target_lang,
        "exemplar_ids":        exemplar_ids,
        "glossary_terms_used": len(glossary),
        "model":               model_id,
        "cost_usd":            cost,
        "cache_hit":           False,
        "disclaimer":          _DISCLAIMER,
    }


_DISCLAIMER = (
    "Reference-implementation output. This translation was produced "
    "by an LLM in the style of Maldives Presidency Office press "
    "releases. NOT an official translation — review against the "
    "original before publishing or quoting."
)


# ── Glossary builder (one-shot batch job) ───────────────────────────

_GLOSSARY_BUILDER_SYSTEM = (
    "You extract bilingual term pairs from paired Maldives Presidency "
    "Office press releases. The English body and Dhivehi body describe "
    "the same content. Identify 5-12 institution names, technical "
    "terms, policy phrases, or proper nouns that appear in BOTH "
    "bodies, and pair them up. Output JSON only.\n\n"
    "Schema:\n"
    "{\"pairs\": [\n"
    "  {\"en\": \"Judicial Service Commission\", \"dv\": \"ޝަރުޢީ ޚިދުމަތާ ބެހޭ ކޮމިޝަން\", "
    "\"domain\": \"government\", \"confidence\": 0.95},\n"
    "  ...\n"
    "]}\n\n"
    "domain options: government, geography, legal, economic, "
    "diplomatic, general. confidence: 0..1 reflecting how certain "
    "you are this is the canonical PO rendering (vs an ad-hoc one). "
    "Skip generic words ('said', 'meeting', 'today') — only "
    "domain-specific terms."
)


_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def _extract_pairs_from_article(
    llm: Any, en_body: str, dv_body: str, *, model_alias: str = "sonnet",
) -> tuple[list[dict], dict]:
    """Call the LLM on one paired article. Returns (pairs, cost_meta).
    Truncates each body to 6000 chars to bound prompt size."""
    user = (
        f"EN BODY:\n{en_body[:6000]}\n\n"
        f"DV BODY:\n{dv_body[:6000]}\n\n"
        f"Extract paired terms as JSON per the schema."
    )
    resp = llm.messages.create(
        model=pricing.model_id(model_alias), max_tokens=2000,
        temperature=0.2,
        system=_GLOSSARY_BUILDER_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        getattr(b, "text", "")
        for b in resp.content if getattr(b, "type", None) == "text"
    )
    m = _JSON_RE.search(text)
    tokens_in = getattr(resp.usage, "input_tokens", 0)
    tokens_out = getattr(resp.usage, "output_tokens", 0)
    meta = {
        "tokens_in":  tokens_in,
        "tokens_out": tokens_out,
        "cost_usd":   pricing.cost(model_alias,
                                     tokens_in=tokens_in,
                                     tokens_out=tokens_out),
    }
    if not m:
        return [], meta
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], meta
    return d.get("pairs", []), meta


def build_glossary(
    conn: sqlite3.Connection,
    *,
    sample_size: int = 200,
    budget_usd: float = 10.0,
    llm: Any = None,
    progress_cb=None,
    model_alias: str = "sonnet",
) -> dict:
    """One-shot batch job: sample paired articles, call the LLM on
    each, aggregate term-pair frequencies, write top-N to
    translation_glossary.

    Sampling strategy: most recent paired articles with non-empty
    bodies. Recency matters because the press office's preferred
    terminology can drift over time; the most recent corpus reflects
    current style.

    Budget-gated: stops as soon as cumulative cost ≥ budget_usd
    (prevents accidental large spends). Default $10 covers ~200
    pairs at ~$0.05 each.

    Returns: {"pairs_in_db": int, "pairs_processed": int,
              "cost_usd": float}."""
    if llm is None:
        import anthropic
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        llm = anthropic.Anthropic(api_key=api_key)

    pairs = conn.execute(
        """SELECT en.id, en.body_text, dv.body_text
           FROM articles en
           JOIN articles dv ON en.paired_id = dv.id
           WHERE en.language = 'EN' AND dv.language = 'DV'
             AND en.body_text IS NOT NULL AND en.body_text != ''
             AND dv.body_text IS NOT NULL AND dv.body_text != ''
             AND LENGTH(en.body_text) >= 500
           ORDER BY en.published_date DESC
           LIMIT ?""",
        (sample_size,),
    ).fetchall()

    aggregated: dict = {}   # key (en, dv) → {freq, domain, confidence, sample_ids}
    cost_total = 0.0
    processed = 0
    for en_id, en_body, dv_body in pairs:
        if cost_total >= budget_usd:
            logger.info("build_glossary: budget cap reached at $%.2f", cost_total)
            break
        terms, meta = _extract_pairs_from_article(
            llm, en_body, dv_body, model_alias=model_alias)
        cost_total += meta.get("cost_usd", 0.0)
        processed += 1
        for t in terms:
            en = (t.get("en") or "").strip()
            dv = (t.get("dv") or "").strip()
            if not en or not dv:
                continue
            key = (en.lower(), dv)
            if key not in aggregated:
                aggregated[key] = {
                    "en_term": en,
                    "dv_term": dv,
                    "domain":  t.get("domain") or "general",
                    "confidence_max": float(t.get("confidence") or 0.5),
                    "sample_ids": [en_id],
                    "freq": 1,
                }
            else:
                aggregated[key]["freq"] += 1
                aggregated[key]["confidence_max"] = max(
                    aggregated[key]["confidence_max"],
                    float(t.get("confidence") or 0.5))
                aggregated[key]["sample_ids"].append(en_id)
        if progress_cb is not None and processed % 10 == 0:
            progress_cb(processed, len(pairs), cost_total)

    # Write top-N (sorted by freq DESC) into translation_glossary.
    # Clear previous rows extracted by the SAME model so re-running
    # this job is idempotent. Manual edits (extracted_by='manual')
    # are preserved.
    now = datetime.now(timezone.utc).isoformat()
    model_id = pricing.model_id(model_alias)
    conn.execute(
        "DELETE FROM translation_glossary WHERE extracted_by = ?",
        (model_id,),
    )
    rows = sorted(aggregated.values(), key=lambda r: -r["freq"])
    for r in rows:
        conn.execute(
            """INSERT INTO translation_glossary
               (en_term, dv_term, domain, freq, confidence,
                sample_en_ids, extracted_at, extracted_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["en_term"], r["dv_term"], r["domain"], r["freq"],
             r["confidence_max"], json.dumps(r["sample_ids"][:10]),
             now, model_id),
        )
    conn.commit()
    n_in_db = conn.execute(
        "SELECT COUNT(*) FROM translation_glossary"
    ).fetchone()[0]
    if progress_cb is not None:
        progress_cb(processed, len(pairs), cost_total)
    return {
        "pairs_in_db":     n_in_db,
        "pairs_processed": processed,
        "cost_usd":        cost_total,
    }
