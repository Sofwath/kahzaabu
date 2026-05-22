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
    recency_days: int = 365,
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


# ── Phrase-anchored context retrieval ──────────────────────────────
#
# Beyond the article-level few-shot, we extract specific phrases
# from the input and pull paragraph-level snippets where the PO
# has used those exact phrases. The LLM sees not just topically-
# similar articles but the specific sentence patterns it should
# match. This is what addresses Nash's case more robustly than
# whole-article BM25 — when the input has "judicial independence"
# or "expatriate workers", we point the LLM directly at sentences
# where the PO has used that phrase.


# Phrase extraction:
#  - EN: runs of 2+ capitalised words (proper nouns / institutions).
#    Filters single-capital-then-lowercase ("The" at sentence start)
#    by requiring at least one INTERIOR capital word — i.e., a
#    capital word that's NOT the very first token after a sentence
#    break.
#  - DV: 2-3 word Thaana sequences. Thaana doesn't have case, so
#    we can't extract proper nouns reliably. Instead we just take
#    all bigrams + trigrams; the FTS5 phrase-existence check
#    filters out ones that don't actually appear in the corpus.

_EN_CAP_RUN_RE = re.compile(
    r"\b(?:[A-Z][a-z]+(?:\s+(?:of|the|and|for|de|du|al)?\s*[A-Z][a-z]+){1,5})\b"
)
_THAANA_WORD = r"[ހ-޿]+"
_DV_NGRAM_RE = re.compile(
    rf"({_THAANA_WORD}(?:\s+{_THAANA_WORD}){{1,2}})"
)
_STOPPHRASE_EN = frozenset({
    "the maldives", "the president", "the government", "the cabinet",
    "the ministry", "today", "yesterday", "this week", "this year",
    "this month",
})


def _extract_phrases(
    text: str, source_lang: str, max_phrases: int = 8,
) -> list[str]:
    """Heuristic phrase extraction. Returns up to max_phrases
    candidate strings, longest-first.

    The result is fed into FTS5 phrase queries — order doesn't
    matter, but longer phrases tend to be more distinctive (better
    BM25 hits) so we prefer them when capping at max_phrases."""
    if not text:
        return []
    out: list[str] = []
    if source_lang == "EN":
        for m in _EN_CAP_RUN_RE.finditer(text):
            phrase = m.group(0).strip()
            # Strip leading article ("The Judicial Service Commission"
            # → "Judicial Service Commission") — articles are caught by
            # the cap-run regex when at sentence start, but the FTS5
            # phrase query is more specific without them.
            for prefix in ("The ", "A ", "An "):
                if phrase.startswith(prefix):
                    phrase = phrase[len(prefix):]
                    break
            if phrase.lower() in _STOPPHRASE_EN:
                continue
            # Skip pure "Word Word" where neither is a known
            # institution marker. Loose filter — corpus FTS5 will
            # confirm relevance downstream.
            if len(phrase) < 6:
                continue
            out.append(phrase)
    else:
        for m in _DV_NGRAM_RE.finditer(text):
            phrase = m.group(1).strip()
            if len(phrase) < 6:
                continue
            out.append(phrase)
    # Dedupe preserving order, then sort by length DESC (longer →
    # more distinctive).
    seen: set = set()
    deduped = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    deduped.sort(key=lambda p: (-len(p), p))
    return deduped[:max_phrases]


def _paragraph_of(text: str, phrase: str) -> Optional[str]:
    """Find the paragraph in `text` containing `phrase`. Paragraphs
    are \\n\\n-separated. Returns None if not found."""
    if not text or not phrase:
        return None
    idx = text.find(phrase)
    if idx < 0:
        return None
    # Find paragraph boundaries
    start = text.rfind("\n\n", 0, idx)
    start = 0 if start < 0 else start + 2
    end = text.find("\n\n", idx)
    end = len(text) if end < 0 else end
    return text[start:end].strip()


def _paired_paragraph_at_index(
    paired_text: Optional[str], index_in_source: int,
    n_source_paragraphs: int,
) -> Optional[str]:
    """Best-effort paragraph alignment: if the source's matching
    paragraph is the i-th paragraph, return the i-th paragraph
    of the paired text.

    Articles aren't always perfectly aligned but the press office
    typically structures EN/DV pairs in parallel — paragraph N in
    EN corresponds to paragraph N in DV. When they don't, the
    snippet just provides loose context; the LLM can interpret."""
    if not paired_text:
        return None
    paras = [p.strip() for p in paired_text.split("\n\n") if p.strip()]
    if not paras:
        return None
    if index_in_source < 0 or index_in_source >= n_source_paragraphs:
        # Clamp to last paragraph
        return paras[-1][:600]
    # Map proportionally if paragraph counts don't match
    if len(paras) == n_source_paragraphs:
        return paras[index_in_source][:600]
    ratio = len(paras) / max(n_source_paragraphs, 1)
    target = min(len(paras) - 1, int(round(index_in_source * ratio)))
    return paras[target][:600]


def select_phrase_contexts(
    conn: sqlite3.Connection,
    input_text: str,
    source_lang: str,
    *,
    max_phrases: int = 4,
    snippets_per_phrase: int = 1,
) -> list[dict]:
    """For each extracted phrase, find paragraph-level snippets
    where the PO has used that phrase. Returns a list of:
        {phrase, source_snippet, target_snippet, article_id}

    Caps total snippets at max_phrases * snippets_per_phrase.
    Per-phrase de-dup against the same article — so 5 hits on the
    same article still produce snippets from at most 1 article."""
    phrases = _extract_phrases(input_text, source_lang,
                                  max_phrases=max_phrases)
    if not phrases:
        return []
    contexts: list[dict] = []
    seen_articles: set = set()
    for phrase in phrases:
        if len(contexts) >= max_phrases * snippets_per_phrase:
            break
        # FTS5 phrase query — quoted to match as exact phrase
        try:
            hits = conn.execute(
                """SELECT a.id, a.language, a.paired_id, a.body_text,
                          p.body_text AS paired_body
                   FROM articles_fts f
                   JOIN articles a ON a.id = f.article_id
                                  AND a.language = f.language
                   LEFT JOIN articles p ON p.id = a.paired_id
                                       AND p.language != a.language
                   WHERE articles_fts MATCH ?
                     AND a.language = ?
                     AND a.paired_id IS NOT NULL
                     AND a.body_text IS NOT NULL AND a.body_text != ''
                     AND p.body_text IS NOT NULL AND p.body_text != ''
                   ORDER BY bm25(articles_fts, 3.0, 1.0)
                   LIMIT ?""",
                (f'"{phrase}"', source_lang, snippets_per_phrase * 3),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        added_for_phrase = 0
        for row in hits:
            if added_for_phrase >= snippets_per_phrase:
                break
            art_id, lang, paired_id, body, paired_body = row
            if art_id in seen_articles:
                continue
            seen_articles.add(art_id)
            src_para = _paragraph_of(body or "", phrase)
            if not src_para:
                continue
            # Compute the index of this paragraph in the source body
            # so we can fetch the same-position paragraph from paired.
            src_paras = [p.strip() for p in (body or "").split("\n\n") if p.strip()]
            try:
                src_idx = src_paras.index(src_para.strip())
            except ValueError:
                src_idx = 0
            tgt_para = _paired_paragraph_at_index(
                paired_body, src_idx, len(src_paras),
            )
            if not tgt_para:
                continue
            contexts.append({
                "phrase":         phrase,
                "source_snippet": src_para[:600],
                "target_snippet": tgt_para,
                "article_id":     art_id,
                "paired_id":      paired_id,
            })
            added_for_phrase += 1
    return contexts


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
    "\n"
    "===  TERMINOLOGY FIDELITY RULE (LOAD-BEARING)  ===\n"
    "The PO has a preferred phrasing for many recurring concepts "
    "that DIFFERS from the literal translation of the input. "
    "Before producing your output, scan the EXAMPLES below for the "
    "same concept as anything in the input — if the examples use a "
    "specific phrase, you MUST use that exact phrase in your "
    "translation, even if the input uses different wording for the "
    "same concept.\n"
    "\n"
    "Worked example: an input that says 'undocumented foreign "
    "nationals' must be translated using the PO's actual phrase "
    "for that concept (e.g. 'undocumented expatriate workers' in "
    "the EN direction, or 'ބިދޭސީން' in the DV direction) — NOT a "
    "literal translation of the input's words. The examples carry "
    "the PO's canonical phrasing; defer to them.\n"
    "\n"
    "This rule overrides literal accuracy when the two conflict.\n"
)


def _compose_prompt(
    input_text: str,
    source_lang: str,
    target_lang: str,
    glossary: list[dict],
    exemplars: list[dict],
    phrase_contexts: Optional[list[dict]] = None,
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

    # PHRASE CONTEXTS — added 2026-05-22. Per-phrase sentence-level
    # context for phrases that appear in the input. The LLM gets to
    # see HOW the PO has used these specific phrases in surrounding
    # text, not just at the article level. This is what makes the
    # translator pick up phrasing patterns like "expatriate workers"
    # (Nash's case) when the broader article-level few-shot might
    # miss them.
    if phrase_contexts:
        parts.append(
            f"PHRASE CONTEXTS — sentences from the press office "
            f"corpus showing how specific phrases in your input "
            f"are used in real {source_name} → {target_name} "
            f"pairs. Match these sentence-level patterns closely:"
        )
        for ctx in phrase_contexts:
            parts.append(f"\n[\"{ctx['phrase']}\" — from art #{ctx['article_id']}]:")
            parts.append(f"  {source_name}: {ctx['source_snippet']}")
            parts.append(f"  {target_name}: {ctx['target_snippet']}")
        parts.append("")

    if exemplars:
        parts.append(
            f"EXAMPLES of the press office's {source_name}↔{target_name} "
            f"style (broader article-level context):"
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
        "phrase_contexts":     [],  # not persisted; empty on cache hit
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
    verify: bool = False,
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

    # Gather context — three layers, increasingly granular:
    #   1. article-level few-shot (exemplars): topic similarity
    #   2. term-level glossary subset: phrase-pair dictionary
    #   3. sentence-level phrase contexts: per-phrase actual usage
    exemplars = select_few_shot(conn, source_lang, text, k=3)
    glossary = select_glossary_subset(conn, text, source_lang, max_terms=20)
    phrase_contexts = select_phrase_contexts(
        conn, text, source_lang,
        max_phrases=4, snippets_per_phrase=1,
    )

    system, user = _compose_prompt(
        text, source_lang, target_lang, glossary, exemplars,
        phrase_contexts=phrase_contexts,
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

    out = {
        "translation":         translation,
        "source_lang":         source_lang,
        "target_lang":         target_lang,
        "exemplar_ids":        exemplar_ids,
        "glossary_terms_used": len(glossary),
        "phrase_contexts":     [
            {"phrase": c["phrase"], "article_id": c["article_id"]}
            for c in phrase_contexts
        ],
        "model":               model_id,
        "cost_usd":            cost,
        "cache_hit":           False,
        "disclaimer":          _DISCLAIMER,
    }
    if verify:
        # Round-trip back-translation. Doubles per-call cost but
        # surfaces "grammatically valid but factually wrong" outputs
        # — the worst failure mode for political text.
        verification = verify_back_translation(
            conn, text, translation,
            source_lang=source_lang, target_lang=target_lang,
            llm=llm, model_alias=model_alias,
        )
        out["verification"] = verification
        out["cost_usd"] = out["cost_usd"] + verification["cost_usd"]
    return out


_DISCLAIMER = (
    "Reference-implementation output. This translation was produced "
    "by an LLM in the style of Maldives Presidency Office press "
    "releases. NOT an official translation — review against the "
    "original before publishing or quoting."
)


# ── Back-translation verification (opt-in) ──────────────────────────


_PROPER_NOUN_RE = re.compile(r"\b[A-Z][A-Za-z]{2,}\b")
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")


def verify_back_translation(
    conn: sqlite3.Connection,
    original_text: str,
    translation: str,
    *,
    source_lang: str,
    target_lang: str,
    llm: Any = None,
    model_alias: str = "sonnet",
) -> dict:
    """Round-trip semantic-preservation check (opt-in).

    Translates the OUTPUT back to the SOURCE language, then compares
    numbers + proper nouns (the high-value invariants for political
    text — a "4 schools" that becomes "1 school" in either direction
    is exactly the failure mode we want to catch).

    Returns: {
        "back_translation":  str,   # for the operator to read
        "numbers_lost":      list,  # numbers in original missing from back
        "numbers_added":     list,
        "proper_nouns_lost": list,
        "proper_nouns_added": list,
        "passed":            bool,  # True iff lost+added are empty
        "cost_usd":          float,
    }

    Doubles the per-translation cost when enabled (~$0.04 instead of
    ~$0.02). Not on by default; CLI surfaces it as --verify, web UI
    as a checkbox."""
    if llm is None:
        import anthropic
        import os
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        llm = anthropic.Anthropic(api_key=api_key)

    # Back-translate without few-shot — we want a STRAIGHT translation
    # so we can compare apples-to-apples with the source. Few-shot
    # would bias the back-translation toward the same exemplars that
    # shaped the forward one.
    target_name = "English" if source_lang == "EN" else "Dhivehi"
    system = (
        f"Translate the given text to {target_name}. Preserve all "
        f"numbers, proper nouns, dates, and institutional names "
        f"exactly. Output the translation only — no commentary."
    )
    resp = llm.messages.create(
        model=pricing.model_id(model_alias), max_tokens=2000,
        temperature=0.1,
        system=system,
        messages=[{"role": "user", "content": translation}],
    )
    back = "".join(
        getattr(b, "text", "")
        for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    tokens_in = getattr(resp.usage, "input_tokens", 0)
    tokens_out = getattr(resp.usage, "output_tokens", 0)
    cost = pricing.cost(model_alias, tokens_in=tokens_in, tokens_out=tokens_out)

    # Compare invariants. We only check FROM THE ORIGINAL'S side
    # because comparing back-translation against the LLM-translated
    # output would just measure round-trip consistency, not fidelity
    # to the input.
    orig_nums = set(_NUMBER_RE.findall(original_text))
    back_nums = set(_NUMBER_RE.findall(back))
    numbers_lost = sorted(orig_nums - back_nums)
    numbers_added = sorted(back_nums - orig_nums)

    # Proper nouns ONLY apply when the source was English (Thaana
    # has no case). If source was DV, we skip the noun check.
    if source_lang == "EN":
        orig_pn = set(_PROPER_NOUN_RE.findall(original_text))
        back_pn = set(_PROPER_NOUN_RE.findall(back))
        # Drop stopwordy capitalisations ("The", "And", etc.)
        stop = {"The", "And", "For", "But", "With", "From", "When",
                 "Where", "What", "Why", "How", "Who", "This",
                 "That", "These", "Those", "Today", "Yesterday",
                 "Tomorrow", "Maldives", "Maldivian"}
        orig_pn -= stop
        back_pn -= stop
        proper_nouns_lost = sorted(orig_pn - back_pn)
        proper_nouns_added = sorted(back_pn - orig_pn)
    else:
        proper_nouns_lost = []
        proper_nouns_added = []

    passed = (not numbers_lost and not numbers_added
              and not proper_nouns_lost and not proper_nouns_added)

    return {
        "back_translation":   back,
        "numbers_lost":       numbers_lost,
        "numbers_added":      numbers_added,
        "proper_nouns_lost":  proper_nouns_lost,
        "proper_nouns_added": proper_nouns_added,
        "passed":             passed,
        "cost_usd":           cost,
    }


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
    llm: Any, en_body: str, dv_body: str, *,
    model_alias: str = "sonnet",
    retries: int = 3,
    timeout_seconds: float = 60.0,
) -> tuple[list[dict], dict]:
    """Call the LLM on one paired article. Returns (pairs, cost_meta).

    Retries on RateLimitError (exponential backoff) and generic
    exceptions (one backoff between attempts). Per-call timeout
    set on the Anthropic SDK so a network stall can't hang the
    whole batch job indefinitely.

    Bodies truncated to 6000 chars to bound prompt size.

    Lifted from `kahzaabu/dv_compare.py`'s `_call_llm` pattern —
    same exponential-backoff schedule. A larger glossary run
    (~$10 / 200 articles) routinely hits the 1% of API calls
    that stall; without retries, one stalled call kills the run."""
    import anthropic
    import time as _time
    user = (
        f"EN BODY:\n{en_body[:6000]}\n\n"
        f"DV BODY:\n{dv_body[:6000]}\n\n"
        f"Extract paired terms as JSON per the schema."
    )
    last_err: Optional[str] = None
    for attempt in range(retries):
        try:
            resp = llm.with_options(timeout=timeout_seconds).messages.create(
                model=pricing.model_id(model_alias), max_tokens=2000,
                temperature=0.2,
                system=_GLOSSARY_BUILDER_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(
                getattr(b, "text", "")
                for b in resp.content
                if getattr(b, "type", None) == "text"
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
        except anthropic.RateLimitError:
            # 2s, 4s, 8s backoff — gives the API a chance to recover
            _time.sleep(2 ** attempt * 2)
            last_err = "rate_limit"
        except Exception as e:
            # Network stalls, timeouts, transient 5xx — back off and
            # retry on all but the last attempt.
            last_err = str(e)[:200]
            if attempt == retries - 1:
                break
            _time.sleep(2 ** attempt)
    # Exhausted retries — return empty pairs + a cost_meta with zeros.
    # Caller's outer loop will move on to the next article.
    logger.warning("build_glossary: exhausted %d retries (%s); skipping article",
                   retries, last_err)
    return [], {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                "_error": last_err}


def _has_anthropic_module():
    """Return whether `anthropic` is importable. Used to defensively
    fall back when the test environment mocks the LLM client and the
    real SDK isn't installed."""
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


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
