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
# MAINTAINER NOTE:
# When you ADD a new load-bearing keyword to this regex, also add a
# corresponding row to `StrongKeywordInvariants.CRITICAL_KEYWORDS` in
# `tests/test_ambient_hook.py`. The test class is the safety net
# against a future refactor silently stripping a critical anchor —
# without a matching row there, a regression goes uncaught.
# When you REMOVE a keyword, also remove the corresponding test row.
# The structural test asserts the test list ⊆ regex source, so a
# stale test row will fail loudly.
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

# Note: an earlier "co-occurrence" path (generic-term + Maldivian-
# anchor pair) was removed 2026-05-22. Every token in the anchor
# regex was also a strong keyword, so the strong path always
# preempted it — the co-occurrence path was effectively dead code
# and confused the DB-missing-hint gating logic (which wanted to
# distinguish strong from co-occurrence, but in practice "co-
# occurrence" meant "never"). Reintroduce only if we add WEAK
# anchor terms — specific atoll/island names (Addu, Faafu, etc.)
# that aren't in _AMBIENT_KEYWORDS but should imply Maldivian
# context when paired with a political-sounding generic term.

# Sticky-session follow-up pattern. The "baseline" set is hardcoded
# so the hook works correctly on a fresh DB before the pipeline runs
# (otherwise a new install would have a degraded sticky-session path
# until the first scrape). The CORPUS-DERIVED set is computed once
# from the live DB at first-fire and unioned on top — see
# `_compute_followup_pattern()` below. Maintainers get self-updating
# topic coverage; the hook stays robust to an empty/missing DB.
_FOLLOWUP_BASELINE_WORDS = frozenset({
    "housing", "education", "foreign", "election", "debt", "court",
    "judic", "cabinet", "ministry", "parliament", "legislation",
    "policy", "gdp", "reserves", "defense", "security", "healthcare",
    "scheme", "project", "target", "promise", "commission",
    "amendment", "appoint", "reshuffle", "tourism", "fisheries",
    "sovereignty", "treaty", "aid", "loan", "infrastructure",
    "airport", "reclamation",
})

# Filled lazily by _followup_pattern() — cached with a TTL so a
# long-lived hermes daemon picks up new topics from the pipeline
# without a restart. Tests can wipe via _clear_followup_pattern_cache().
_followup_pattern_cache: Optional[re.Pattern] = None
_followup_words_cache: Optional[frozenset[str]] = None
_followup_built_at: Optional[float] = None

# Default TTL is 6h. Override via KAHZAABU_FOLLOWUP_TTL_SECONDS in
# ~/.hermes/.env or shell env — useful for deployments that run the
# pipeline more frequently and want faster topic propagation, or
# tests that need a short cache life. Read on every call to
# _followup_pattern() so a runtime env change takes effect on the
# next vocab-rebuild (don't have to restart hermes to dial it).
_FOLLOWUP_CACHE_TTL_DEFAULT_SECONDS = 6 * 60 * 60

# Memo for the parsed TTL. Invalidated when the env-var STRING changes
# (cheap pointer-compare/equality check on every read; int() parse only
# runs on actual env change). Keeps the hot path zero-allocation.
_ttl_parsed_cache: Optional[int] = None
_ttl_parsed_env_value: Optional[str] = None


def _followup_ttl_seconds() -> int:
    """Return the current TTL.

    Reads KAHZAABU_FOLLOWUP_TTL_SECONDS on every call so a runtime
    env change picks up on the next call — tests can patch.dict
    os.environ and the next cache build will honour the override.

    The PARSED value is memoised against the raw env-var string;
    int() only runs when the env actually changes. Hot path is a
    dict lookup + equality compare.
    """
    global _ttl_parsed_cache, _ttl_parsed_env_value
    raw = os.environ.get("KAHZAABU_FOLLOWUP_TTL_SECONDS", "")
    if raw == _ttl_parsed_env_value and _ttl_parsed_cache is not None:
        return _ttl_parsed_cache
    # Slow path — env-var string changed (or first call this process).
    stripped = raw.strip()
    if not stripped:
        result = _FOLLOWUP_CACHE_TTL_DEFAULT_SECONDS
    else:
        try:
            v = int(stripped)
            result = v if v > 0 else _FOLLOWUP_CACHE_TTL_DEFAULT_SECONDS
        except ValueError:
            # Malformed env var — fall back to default. Don't crash
            # the hook over a typo.
            logger.debug(
                "kahzaabu: KAHZAABU_FOLLOWUP_TTL_SECONDS=%r is not an int; "
                "using default %d", raw, _FOLLOWUP_CACHE_TTL_DEFAULT_SECONDS,
            )
            result = _FOLLOWUP_CACHE_TTL_DEFAULT_SECONDS
    _ttl_parsed_cache = result
    _ttl_parsed_env_value = raw
    return result

# Soft cap on injected context size — agents do better with concise
# context than a wall of text. 1.5KB is enough for 3 fact-checks +
# 2 constitution headers.
_MAX_CONTEXT_CHARS = 1500


# Match-strength enum. Used by both _is_relevant (which returns bool)
# and _classify_match (which returns the strength) so callers that
# care about why we matched can route accordingly. The DB-missing
# hint, for example, only fires on STRONG matches — sticky matches
# stay silent because we either already had a chance to inject the
# hint on the original strong match, or this session never had one.
MATCH_NONE = 0
MATCH_STRONG = 1   # high-precision keyword (Muizzu, JSC, Maldiv, …)
MATCH_STICKY = 2   # session is already hot; loose follow-up


def _classify_match(user_message: str, session_id: str = "") -> int:
    """Return MATCH_NONE / STRONG / STICKY.

    Side-effect: STRONG matches mark the session hot.
    STICKY matches refresh the TTL. This is the single authority
    on prefilter classification — _is_relevant is a thin truthy
    wrapper."""
    if not user_message or len(user_message) < 8:
        return MATCH_NONE

    if _AMBIENT_KEYWORDS.search(user_message):
        _mark_session_hot(session_id)
        return MATCH_STRONG

    if session_id and _is_session_hot(session_id) and _followup_pattern().search(user_message):
        _mark_session_hot(session_id)
        return MATCH_STICKY

    return MATCH_NONE


def _is_relevant(user_message: str, session_id: str = "") -> bool:
    """Backwards-compat truthy wrapper around _classify_match.

    Existing callers and tests treat the prefilter as a boolean;
    this preserves that surface while internal callers can use
    _classify_match for finer routing."""
    return _classify_match(user_message, session_id=session_id) != MATCH_NONE


def _mark_session_hot(session_id: str) -> None:
    """Record (or refresh) that the given session is on-topic.

    Writes both to in-memory state AND (best-effort) to the
    persistent ambient_hot_sessions table. Cross-process consistency:
    a strong match on platform A (e.g. CLI) makes the session hot
    on platform B (e.g. Telegram) inside the same hermes deployment.

    Bounded LRU eviction on the in-memory dict prevents runaway
    growth; SQLite handles its own row count.
    """
    if not session_id:
        return
    now = time.monotonic()
    with _state_lock:
        _session_hits[session_id] = now
        if len(_session_hits) > _STICKY_MAX_ENTRIES:
            n_drop = max(1, _STICKY_MAX_ENTRIES // 10)
            for k in sorted(_session_hits, key=_session_hits.get)[:n_drop]:
                _session_hits.pop(k, None)
    # Persistent write — best effort. If the DB is missing or
    # locked we fall back to in-memory-only (already done above).
    db_path = _resolve_db_path()
    if db_path is None:
        return
    try:
        # Wall-clock unix timestamp for cross-process comparability
        # (time.monotonic() is per-process; can't share across procs).
        import time as _t
        hot_until = _t.time() + _STICKY_TTL_SECONDS
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        try:
            conn.execute(
                "INSERT INTO ambient_hot_sessions (session_id, hot_until) "
                "VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET hot_until = excluded.hot_until",
                (session_id, hot_until),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        # Common cases: DB locked by another writer, table missing
        # (legacy DB without the migration). Both are recoverable —
        # the in-memory path above keeps the hook working.
        logger.debug("ambient: persistent hot-session write failed: %s", e)


def _is_session_hot(session_id: str) -> bool:
    """True if the session has a fresh strong-match within the TTL.

    Checks in-memory first (cheap; same-process). Falls through to
    SQLite (cross-process). Lazy GC: while we're here for a read,
    drop any rows whose hot_until is in the past."""
    if not session_id:
        return False
    # In-memory check — same process, sub-microsecond.
    with _state_lock:
        last = _session_hits.get(session_id)
    if last is not None and (time.monotonic() - last) < _STICKY_TTL_SECONDS:
        return True
    # Persistent check — cross-process.
    db_path = _resolve_db_path()
    if db_path is None:
        return False
    try:
        import time as _t
        now = _t.time()
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        try:
            row = conn.execute(
                "SELECT hot_until FROM ambient_hot_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            if row[0] >= now:
                # Mirror into in-memory so subsequent checks in this
                # process avoid the DB round-trip.
                with _state_lock:
                    _session_hits[session_id] = time.monotonic()
                return True
            # Expired — lazy GC. Drop this row and any others in the
            # past while we hold the connection (cheap).
            conn.execute(
                "DELETE FROM ambient_hot_sessions WHERE hot_until < ?",
                (now,),
            )
            conn.commit()
            return False
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug("ambient: persistent hot-session read failed: %s", e)
        return False


def _clear_sticky_state() -> None:
    """Test helper — wipe sticky-session memory AND persistent state.
    Not used in production."""
    with _state_lock:
        _session_hits.clear()
    db_path = _resolve_db_path()
    if db_path is None:
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        try:
            conn.execute("DELETE FROM ambient_hot_sessions")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _compute_followup_words(db_path: Optional[Path]) -> frozenset[str]:
    """Build the corpus-derived follow-up vocabulary.

    Pulls the `fact_checks.topic` column (which is a discrete
    categorical like "fiscal_debt", "diplomatic_india_china") and
    splits on underscores. Yields tokens like {fiscal, debt,
    diplomatic, india, china, ...} — clean signal, no need for
    stopword filtering.

    Falls back gracefully: empty set if DB missing or query fails,
    so the baseline keyword list still works."""
    if db_path is None or not db_path.exists():
        return frozenset()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT DISTINCT topic FROM fact_checks "
                "WHERE published = 1 AND topic IS NOT NULL "
                "AND topic != ''"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.debug("corpus follow-up vocab build failed: %s", e)
        return frozenset()
    words: set[str] = set()
    for (topic,) in rows:
        # Split on underscore or whitespace; keep tokens that are
        # at least 4 chars (matches our prefilter's general bias).
        for tok in re.split(r"[_\s]+", topic.lower()):
            if len(tok) >= 4:
                words.add(tok)
    return frozenset(words)


def _followup_pattern() -> re.Pattern:
    """Return the compiled sticky-session follow-up pattern.

    Built from BASELINE ∪ CORPUS-DERIVED words. Cached with a TTL
    read lazily from `_followup_ttl_seconds()` so a long-running
    hermes daemon picks up new pipeline-emitted topics without
    restart and operators can dial the TTL at runtime. Tests can
    drop the cache via _clear_followup_pattern_cache()."""
    global _followup_pattern_cache, _followup_words_cache, _followup_built_at
    now = time.monotonic()
    # Fast path: cache fresh — no lock needed for a single read
    # because Python's GIL keeps reference reads atomic.
    if _followup_pattern_cache is not None and _followup_built_at is not None:
        if (now - _followup_built_at) < _followup_ttl_seconds():
            return _followup_pattern_cache
    # Slow path: rebuild under the lock (double-checked).
    with _state_lock:
        if (_followup_pattern_cache is not None
                and _followup_built_at is not None
                and (now - _followup_built_at) < _followup_ttl_seconds()):
            return _followup_pattern_cache
        corpus = _compute_followup_words(_resolve_db_path())
        all_words = _FOLLOWUP_BASELINE_WORDS | corpus
        # Sort by length DESC so longer alternatives win in the OR
        # group (avoids "judic" shadowing "judicial" mid-alternation
        # quirks across regex engines).
        sorted_words = sorted(all_words, key=lambda w: (-len(w), w))
        pat = r"\b(" + "|".join(re.escape(w) for w in sorted_words) + r")\b"
        _followup_pattern_cache = re.compile(pat, re.IGNORECASE)
        _followup_words_cache = frozenset(all_words)
        _followup_built_at = now
        logger.debug(
            "kahzaabu sticky follow-up vocab: %d words "
            "(baseline %d + corpus %d) — TTL %ds",
            len(all_words), len(_FOLLOWUP_BASELINE_WORDS), len(corpus),
            _followup_ttl_seconds(),
        )
    return _followup_pattern_cache


def _clear_followup_pattern_cache() -> None:
    """Test helper — drop the cached follow-up pattern so the next
    call rebuilds it (e.g. after fixture DB changes)."""
    global _followup_pattern_cache, _followup_words_cache, _followup_built_at
    with _state_lock:
        _followup_pattern_cache = None
        _followup_words_cache = None
        _followup_built_at = None


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


def _handle_db_missing_once() -> Optional[dict]:
    """Called when the hook's match path finds no DB.

    On the FIRST such occurrence per process, returns a context-dict
    that the agent will see in its incoming context — so the user can
    learn about the setup gap in their actual chat reply, not just via
    a log message they probably won't read. Also logs a single WARNING
    for the operator.

    On every subsequent miss, returns None — silent on the log side
    (so it doesn't flood) AND silent in the chat (so the user doesn't
    see the same hint every turn).

    Returning {"context": ...} for an unpopulated archive is a soft
    nudge: the agent can mention the setup gap naturally if it's
    relevant ("by the way, the archive isn't populated yet — run
    `hermes kahzaabu setup` to enable the fact-check cross-references")
    or ignore it if the user's question doesn't depend on the archive."""
    global _db_missing_warned
    with _state_lock:
        if _db_missing_warned:
            return None
        _db_missing_warned = True

    logger.warning(
        "kahzaabu ambient hook: matched a Maldivian-politics topic, but "
        "no kahzaabu DB found. Run `hermes kahzaabu setup` to populate "
        "the archive — until then, the ambient hook is a no-op. Set "
        "KAHZAABU_AMBIENT_DISABLE=1 in ~/.hermes/.env to silence this "
        "and skip future hook dispatches."
    )
    return {"context": (
        "[Kahzaabu ambient hook — heads-up: this message mentions a "
        "Maldivian-politics topic, and the kahzaabu fact-check archive "
        "is enabled but not yet populated on this machine. The archive "
        "would normally inject relevant fact-checks + constitution "
        "articles here. To enable that, run "
        "`hermes kahzaabu setup` (or `hermes kahzaabu update` if "
        "setup is already done). This notice is shown once per "
        "hermes process. Set KAHZAABU_AMBIENT_DISABLE=1 in "
        "~/.hermes/.env to suppress entirely.]"
    )}


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

    # Classify the prefilter match. STRONG means a high-precision
    # Maldivian keyword fired (the user is clearly asking about
    # Maldivian politics). COOCCURRENCE / STICKY are softer — same
    # message-level relevance but lower confidence the user wants
    # an interruption from us if the DB is missing.
    strength = _classify_match(user_message, session_id=session_id)
    if strength == MATCH_NONE:
        return None

    # We're in the match path. Open the DB lazily — the prefilter
    # path above never touches the FS.
    try:
        db_path = _resolve_db_path()
        if db_path is None:
            # Inject the one-time "run setup" hint ONLY on STRONG
            # matches. Soft co-occurrence/sticky matches stay silent
            # to avoid injecting setup chatter into chats where the
            # user mentioned a Maldivian topic only in passing.
            if strength == MATCH_STRONG:
                return _handle_db_missing_once()
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
