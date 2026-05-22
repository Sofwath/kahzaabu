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
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── State carried across hook invocations ───────────────────────────
#
# Two module-level dicts. Both are guarded by `_state_lock` because
# hermes can dispatch hook callbacks from multiple threads when a
# session is processing parallel platform events.

_state_lock = threading.Lock()

# Sticky-session memory: map session_id → unix-timestamp of last
# strong-match. While a session is "hot", the prefilter accepts looser
# generic-term mentions (president, manifesto, amendment) without
# requiring a Maldivian anchor — because we already know the
# conversation is on-topic.
#
# Conceptually: once the user has mentioned Muizzu / JSC / Maldives
# in a session, follow-up turns like "what did he do about housing?"
# also get kahzaabu context.
_session_hits: dict[str, float] = {}

# TTL: how long a session stays "hot" after the last strong match.
# 30 minutes covers most chat sessions; a stale entry just means the
# prefilter falls back to its strict path on the next turn.
_STICKY_TTL_SECONDS = 30 * 60

# Cap entries to bound memory under runaway session-id churn (e.g.
# automated test harnesses). LRU eviction by oldest-timestamp.
_STICKY_MAX_ENTRIES = 1000

# One-time warning state — log the "DB not found" hint at most once
# per process so a misconfigured install gets noticed without flooding
# the log on every user turn.
_db_missing_warned: bool = False


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

# BROADER pattern used ONLY for the sticky-session follow-up path.
# Once a session is already on-topic, a loose follow-up like
# "what about housing?" / "did anything happen with the JSC?" /
# "what's the court ruling?" should still inject context. This
# pattern matches the kinds of topic-words that come up in Maldivian
# political conversations — but is intentionally not used for cold-
# session classification (would flood unrelated chats otherwise).
_FOLLOWUP_TOPICS = re.compile(
    r"\b("
    r"housing|education|foreign|election|debt|court|judic|cabinet|"
    r"ministry|parliament|legislation|policy|gdp|reserves|"
    r"defense|security|healthcare|scheme|project|target|promise|"
    r"commission|amendment|appoint|reshuffle|tourism|fisheries|"
    r"sovereignty|treaty|aid|loan|infrastructure|airport|reclamation"
    r")\b",
    re.IGNORECASE,
)

# Soft cap on injected context size — agents do better with concise
# context than a wall of text. 1.5KB is enough for 3 fact-checks +
# 2 constitution headers.
_MAX_CONTEXT_CHARS = 1500


def _is_relevant(user_message: str, session_id: str = "") -> bool:
    """Fast prefilter. Sub-10ms target.

    Returns True when the message warrants kahzaabu context. Three
    paths, from strictest to loosest:

      1. Strong keyword path (always fires) — a high-precision Maldivian
         keyword appears (Muizzu, JSC, Maldiv, Majlis, …). Also marks
         the session as "hot" so subsequent looser follow-ups still
         match.

      2. Co-occurrence path — both a generic term (president, manifesto,
         amendment) AND a Maldivian anchor appear. Disambiguates
         "the President said …" from "the French president said …".

      3. Sticky-session path — the session is currently "hot" (saw a
         strong match within _STICKY_TTL_SECONDS) AND the message
         contains any generic term. This is how "what did he do
         about housing?" follow-ups still get context even though
         "he" / "housing" alone wouldn't trip the strict prefilter.
    """
    if not user_message or len(user_message) < 8:
        return False

    # Path 1 — strong keyword
    if _AMBIENT_KEYWORDS.search(user_message):
        _mark_session_hot(session_id)
        return True

    # Path 2 — co-occurrence
    if _GENERIC_TERMS.search(user_message) and _MALDIVIAN_ANCHOR.search(user_message):
        _mark_session_hot(session_id)
        return True

    # Path 3 — sticky session (loose follow-up in an already-hot session).
    # Uses the broader _FOLLOWUP_TOPICS pattern, which is too permissive
    # to use for cold-session classification but the right granularity
    # for follow-up turns where we already know the topic.
    if session_id and _is_session_hot(session_id) and _FOLLOWUP_TOPICS.search(user_message):
        # Refresh the TTL — the session is still on-topic.
        _mark_session_hot(session_id)
        return True

    return False


def _mark_session_hot(session_id: str) -> None:
    """Record (or refresh) that the given session is on-topic. Bounded
    LRU eviction prevents runaway growth."""
    if not session_id:
        return
    now = time.monotonic()
    with _state_lock:
        _session_hits[session_id] = now
        if len(_session_hits) > _STICKY_MAX_ENTRIES:
            # Drop the oldest 10% of entries
            n_drop = max(1, _STICKY_MAX_ENTRIES // 10)
            for k in sorted(_session_hits, key=_session_hits.get)[:n_drop]:
                _session_hits.pop(k, None)


def _is_session_hot(session_id: str) -> bool:
    """True if the session has a fresh strong-match within the TTL."""
    if not session_id:
        return False
    with _state_lock:
        last = _session_hits.get(session_id)
    if last is None:
        return False
    return (time.monotonic() - last) < _STICKY_TTL_SECONDS


def _clear_sticky_state() -> None:
    """Test helper — wipe sticky-session memory. Not used in production."""
    with _state_lock:
        _session_hits.clear()


def _platform_allowed(platform: str) -> bool:
    """Honour the KAHZAABU_AMBIENT_PLATFORMS whitelist.

    Unset → all platforms allowed (default backwards-compat behaviour).
    Set to "cli,telegram" → hook fires only on those platforms.

    Lets a user enable the ambient hook in their terminal while keeping
    it quiet in group chats (Slack/Discord) where the auto-injection
    might surprise other participants."""
    allowlist = os.environ.get("KAHZAABU_AMBIENT_PLATFORMS", "").strip()
    if not allowlist:
        return True
    allowed = {p.strip().lower() for p in allowlist.split(",") if p.strip()}
    return (platform or "cli").lower() in allowed


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


def _warn_db_missing_once() -> None:
    """Log a helpful 'run setup' hint ONCE per process when the hook's
    match path finds no DB. Silent on every subsequent miss so the log
    doesn't get flooded — but the operator sees the message the first
    time a Maldivian-politics topic comes up in conversation.

    Without this, a misconfigured install (plugin enabled but pipeline
    never run) silently no-ops forever and the operator never finds out
    why the ambient context isn't showing up."""
    global _db_missing_warned
    with _state_lock:
        if _db_missing_warned:
            return
        _db_missing_warned = True
    logger.warning(
        "kahzaabu ambient hook: matched a Maldivian-politics topic, but "
        "no kahzaabu DB found. Run `hermes kahzaabu setup` to populate "
        "the archive — until then, the ambient hook is a no-op. Set "
        "KAHZAABU_AMBIENT_DISABLE=1 in ~/.hermes/.env to silence this "
        "and skip future hook dispatches."
    )


def _reset_db_missing_warning() -> None:
    """Test helper — reset the one-time warning flag."""
    global _db_missing_warned
    with _state_lock:
        _db_missing_warned = False


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
    # Opt-out check is the very first thing. Honour both the
    # whole-hook kill switch AND the per-platform allowlist.
    if os.environ.get("KAHZAABU_AMBIENT_DISABLE"):
        return None
    if not _platform_allowed(platform):
        return None

    # Prefilter (with sticky-session context for follow-up turns).
    if not _is_relevant(user_message, session_id=session_id):
        return None

    # We're in the match path. Open the DB lazily — the prefilter
    # path above never touches the FS.
    try:
        db_path = _resolve_db_path()
        if db_path is None:
            _warn_db_missing_once()
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


def hook_status() -> dict:
    """Return a snapshot of the hook's runtime state for diagnostics.

    Consumed by `hermes kahzaabu doctor` to surface "the hook is
    enabled and ready" vs "enabled but DB missing" vs "disabled by
    env var" — three operationally-distinct states that all look
    identical to a casual log inspection.

    Shape:
        {
            "enabled": bool,
            "disable_reason": Optional[str],  # "env" | "no_db" | None
            "platform_allowlist": Optional[list[str]],
            "db_path": Optional[str],
            "hot_sessions": int,
        }
    """
    disabled_env = bool(os.environ.get("KAHZAABU_AMBIENT_DISABLE"))
    db_path = _resolve_db_path()
    allowlist_raw = os.environ.get("KAHZAABU_AMBIENT_PLATFORMS", "").strip()
    allowlist = (
        [p.strip().lower() for p in allowlist_raw.split(",") if p.strip()]
        if allowlist_raw else None
    )

    if disabled_env:
        disable_reason = "env"
        enabled = False
    elif db_path is None:
        disable_reason = "no_db"
        enabled = False  # would no-op on match anyway
    else:
        disable_reason = None
        enabled = True

    with _state_lock:
        hot_count = len(_session_hits)

    return {
        "enabled":            enabled,
        "disable_reason":     disable_reason,
        "platform_allowlist": allowlist,
        "db_path":            str(db_path) if db_path else None,
        "hot_sessions":       hot_count,
    }
