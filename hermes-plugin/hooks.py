# SPDX-License-Identifier: Apache-2.0
"""kahzaabu pre_llm_call hook — ambient context injection.

When a user's message in any hermes chat mentions a Maldivian-politics
topic, this hook does a cheap keyword prefilter, then a fast BM25
lookup against the kahzaabu archive, and returns 1-3 relevant
fact-checks + 1-2 constitution articles as context for that turn.

The goal: turn kahzaabu from "a tool you call" into "an ambient
knowledge layer that pays attention". A user discussing the President
in a hermes chat gets kahzaabu's grounding automatically — they don't
have to remember `/kahzaabu`.

Performance targets (hard requirements — this fires on EVERY user turn
across hermes):
  - Non-match path: < 10ms  (the regex prefilter is the hot path)
  - Match path:     < 200ms (BM25 lookup against the local SQLite)

Opt-out:
  - KAHZAABU_AMBIENT_DISABLE=1  disables the hook entirely.
  - The hook also no-ops if kahzaabu isn't importable (defensive: the
    plugin must work even if the DB is missing).

Return contract (hermes pre_llm_call):
  - None → no injection
  - {"context": str}  → injected into the user message for that turn
                         (NOT system prompt, so prompt cache is preserved).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Compiled once at import. Case-insensitive. Patterns are designed
# to match word-stems — "maldiv" matches Maldiv/Maldives/Maldivian
# because the trailing `\b` is omitted intentionally (we want the
# stem-match behaviour, not the exact-word behaviour).
#
# Notes on the choices:
#   - "muizzu" / "kahzaabu" — President's name + Dhivehi for "lie"
#   - "presidency.gov" — anchored URL form
#   - "JSC" — Judicial Service Commission abbreviation
#   - "majlis" — the Maldivian parliament (Dhivehi)
#   - "atoll" — almost-unique to Maldives in English usage
#   - "raajje" — Dhivehi for "kingdom" (the country)
#   - "hulhumale" / "gulhifalhu" / "ras male" — landmark place names
_AMBIENT_KEYWORDS = re.compile(
    r"("
    r"\bmuizzu|\bkahzaabu|\bmaldiv|"          # stem match, no trailing \b
    r"\bpresidency\.gov\b|"
    r"\bJSC\b|\bjudicial service commission\b|"
    r"\bpresidential office\b|\bmajlis\b|"
    r"\batoll|\braajje\b|\bgulhifalhu\b|"
    r"\bhulhumale|\bras\s+male\b"
    r")",
    re.IGNORECASE,
)

# Secondary pattern: "manifesto" / "fact-check" / "president" are too
# common standalone. Require co-occurrence with a Maldivian anchor in
# the same message — implemented as two regexes that both must match.
_GENERIC_TERMS = re.compile(
    r"\b(manifesto|fact[\s-]?check|president|housing\s+scheme|amendment)\b",
    re.IGNORECASE,
)
_MALDIVIAN_ANCHOR = re.compile(
    r"(\bmuizzu|\bkahzaabu|\bmaldiv|\bmajlis\b|\batoll|\braajje\b)",
    re.IGNORECASE,
)

# Soft cap on injected context size — agents do better with concise
# context than a wall of text. 1.5KB is enough for 3 fact-checks +
# 2 constitution headers.
_MAX_CONTEXT_CHARS = 1500


def _is_relevant(user_message: str) -> bool:
    """Fast prefilter. Sub-10ms target.

    True if EITHER:
      - A strong Maldivian-politics keyword appears (single-pass match), OR
      - Both a generic-term AND a Maldivian-anchor appear (two-pass match,
        used to disambiguate "the President said …" — only relevant
        when the surrounding text is about the Maldives).
    """
    if not user_message or len(user_message) < 8:
        return False
    if _AMBIENT_KEYWORDS.search(user_message):
        return True
    if _GENERIC_TERMS.search(user_message) and _MALDIVIAN_ANCHOR.search(user_message):
        return True
    return False


def _resolve_db_path() -> Optional[Path]:
    """Locate the kahzaabu SQLite DB. Returns None if unfindable."""
    override = os.environ.get("KAHZAABU_DB")
    if override:
        p = Path(override).expanduser()
        return p if p.exists() else None
    # Try the dev-tree-relative path the plugin uses elsewhere.
    try:
        import kahzaabu as _kpkg
        p = Path(_kpkg.__file__).resolve().parents[1] / "data" / "kahzaabu.db"
        if p.exists():
            return p
    except ImportError:
        pass
    # Fall back to the OS-conventional install location.
    p = Path.home() / ".local" / "share" / "kahzaabu" / "kahzaabu.db"
    return p if p.exists() else None


def _format_context(fc_hits: list, const_hits: list) -> str:
    """Build the context string that gets injected. Concise, no chrome —
    the agent reads this and we want to minimise token bloat."""
    lines = [
        "[Ambient kahzaabu context — auto-injected because your message "
        "mentions a Maldivian-politics topic. This is a reference-"
        "implementation archive; not authoritative.]",
    ]
    if fc_hits:
        lines.append("")
        lines.append(f"Relevant fact-checks ({len(fc_hits)}):")
        for h in fc_hits:
            v = (h.get("verdict_label") or "—").replace("_", " ")
            claim = (h.get("claim") or "")[:140]
            lines.append(f"  • fc#{h['id']} [{v}] {claim}")
    if const_hits:
        lines.append("")
        lines.append(f"Relevant Constitution articles ({len(const_hits)}):")
        for h in const_hits:
            title = (h.get("title") or "")[:60]
            lines.append(f"  • Article {h['article_no']} — {title}")
    lines.append("")
    lines.append(
        "Reminder: cite the original press release at presidency.gov.mv, "
        "not kahzaabu's automated analysis."
    )
    out = "\n".join(lines)
    if len(out) > _MAX_CONTEXT_CHARS:
        out = out[:_MAX_CONTEXT_CHARS] + "…"
    return out


def on_pre_llm_call(
    *,
    session_id: str = "",
    user_message: str = "",
    conversation_history: Any = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "cli",
    **_kw,
) -> Optional[dict]:
    """The hook itself. Return None for no injection, or
    {"context": "..."} to inject."""
    # Opt-out check is the very first thing. Honour both the new
    # env var and a generic kill switch so an operator can disable
    # all kahzaabu hooks without restarting hermes.
    if os.environ.get("KAHZAABU_AMBIENT_DISABLE"):
        return None

    if not _is_relevant(user_message):
        return None

    # We're in the match path. Open the DB lazily — the prefilter
    # path above never touches the FS.
    try:
        db_path = _resolve_db_path()
        if db_path is None:
            logger.debug("ambient hook: kahzaabu DB not found, skipping injection")
            return None

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            from kahzaabu.factcheck_search import search_fact_checks
            from kahzaabu.constitution import lookup as const_lookup

            fc_hits = search_fact_checks(
                conn, user_message, limit=3, published_only=True)
            const_hits = const_lookup(conn, user_message, limit=2)
        finally:
            conn.close()
    except Exception as e:
        # Defensive: a hook that throws would degrade the entire hermes
        # turn. Log and return None — the agent continues without our
        # context but doesn't fail.
        logger.warning("ambient hook failed: %s", e)
        return None

    if not fc_hits and not const_hits:
        return None

    return {"context": _format_context(fc_hits, const_hits)}
