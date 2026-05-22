# SPDX-License-Identifier: Apache-2.0
"""Tests for the pre_llm_call ambient-context hook.

The hook fires on every user turn across hermes, so the tests pin:

  1. The prefilter is fast (sub-10ms target) and never returns True
     for messages without a Maldivian-politics anchor.
  2. The opt-out env var (KAHZAABU_AMBIENT_DISABLE) is honoured.
  3. On a relevant message, the hook calls into the search modules
     and returns a {"context": str} dict.
  4. Empty/very short messages never fire (avoids surfacing on
     "hi" / "ok" / "thanks").
  5. The hook is defensive: if the DB is missing or the search
     functions throw, it returns None rather than letting the
     exception propagate into the hermes turn.

Integration with the actual hermes process is verified separately
(see the parent commit's verification notes).
"""
from __future__ import annotations

import logging
import logging.handlers
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The hook lives in the hermes-plugin tree. Stage it for import the
# same way test_hermes_plugin.py does — symlink hermes-stub/plugins/
# kahzaabu so the `plugins.kahzaabu` import path resolves.
HERMES_STUB = ROOT / "hermes-stub"
(HERMES_STUB / "plugins").mkdir(parents=True, exist_ok=True)
plugin_link = HERMES_STUB / "plugins" / "kahzaabu"
if not plugin_link.exists():
    plugin_link.symlink_to(ROOT / "hermes-plugin")
(HERMES_STUB / "plugins" / "__init__.py").touch()
sys.path.insert(0, str(HERMES_STUB))

from plugins.kahzaabu import hooks  # noqa: E402


# ───────────────────────────────────────────────────────────────────
# Prefilter — the hot path, must be fast and high-precision
# ───────────────────────────────────────────────────────────────────

class PrefilterRelevance(unittest.TestCase):
    """The prefilter is the regex-only path that runs on every user
    turn. Has to fire on Maldivian-politics topics and stay quiet
    on everything else."""

    # Messages that MUST fire — strong Maldivian-politics keywords
    RELEVANT = [
        "What did Muizzu do last week?",
        "Tell me about the JSC composition amendments",
        "Is the Maldives going to host the SAARC summit?",
        "What's happening at the Majlis?",
        "The Maldivian government's housing scheme",
        "President Muizzu announced…",
        "How does the Maldives Constitution define citizenship?",
        # Co-occurrence path (manifesto + maldiv)
        "Tell me about the Maldivian manifesto",
        # Co-occurrence with anchor
        "What did the president say about Hulhumale housing?",
    ]
    # Messages that MUST NOT fire — these would be false positives
    NOT_RELEVANT = [
        "What's the weather in San Francisco?",
        "Write a Python script that sorts a list",
        "How do I deploy a FastAPI app?",
        "Tell me about the French president",  # 'president' alone, no anchor
        "What's a manifesto in software engineering?",  # 'manifesto' no anchor
        "I need a fact-check on this claim about climate",  # generic
        "",
        "ok",
        "thanks",
        "yes",
        "hi there",
    ]

    def test_relevant_messages_match(self):
        for msg in self.RELEVANT:
            with self.subTest(msg=msg):
                self.assertTrue(hooks._is_relevant(msg),
                    f"Expected the prefilter to fire on: {msg!r}")

    def test_irrelevant_messages_do_not_match(self):
        for msg in self.NOT_RELEVANT:
            with self.subTest(msg=msg):
                self.assertFalse(hooks._is_relevant(msg),
                    f"Prefilter false-positive on: {msg!r}. This message "
                    "doesn't mention Maldivian politics — firing the hook "
                    "would inject irrelevant context into every hermes "
                    "conversation about unrelated topics.")

    def test_prefilter_is_fast(self):
        """The prefilter runs on every user turn in hermes; it has to be
        cheap. 1000 calls in well under 100ms (sub-100µs each)."""
        sample = (
            "I'm trying to figure out the best way to refactor this "
            "Python module without breaking the existing tests. The "
            "current design has too many circular imports."
        )
        start = time.perf_counter()
        for _ in range(1000):
            hooks._is_relevant(sample)
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.assertLess(elapsed_ms, 100,
            f"prefilter took {elapsed_ms:.1f}ms for 1000 calls; "
            "needs to be sub-100ms total to be safe on hot path")


# ───────────────────────────────────────────────────────────────────
# Opt-out — env var must short-circuit the entire hook
# ───────────────────────────────────────────────────────────────────

class OptOutEnvVar(unittest.TestCase):
    def test_disable_env_var_makes_hook_return_none(self):
        with patch.dict("os.environ", {"KAHZAABU_AMBIENT_DISABLE": "1"}):
            r = hooks.on_pre_llm_call(
                user_message="What did Muizzu do last week?"
            )
            self.assertIsNone(r,
                "KAHZAABU_AMBIENT_DISABLE=1 must suppress the hook "
                "entirely — even for messages that would otherwise match")


# ───────────────────────────────────────────────────────────────────
# Hook return contract
# ───────────────────────────────────────────────────────────────────

class HookReturnContract(unittest.TestCase):
    """The hook must return None for misses and {"context": str} for hits.
    Anything else would break hermes' invoke_hook contract."""

    def test_irrelevant_message_returns_none(self):
        r = hooks.on_pre_llm_call(
            user_message="How do I write a Python decorator?")
        self.assertIsNone(r)

    def test_empty_message_returns_none(self):
        self.assertIsNone(hooks.on_pre_llm_call(user_message=""))
        self.assertIsNone(hooks.on_pre_llm_call(user_message="hi"))

    def test_relevant_message_with_db_returns_context(self):
        """When the DB is present and FTS5-populated, the hook should
        return a dict with a 'context' string for Maldivian topics."""
        # Use the live DB if it exists. Otherwise this test is skipped —
        # the unit-level behaviour is covered by the prefilter tests; this
        # is the end-to-end pin.
        db = Path.cwd() / "data" / "kahzaabu.db"
        if not db.exists():
            self.skipTest("live DB not available")
        with patch.dict("os.environ", {}, clear=False):
            # Make sure opt-out is not set
            import os as _os
            _os.environ.pop("KAHZAABU_AMBIENT_DISABLE", None)
            r = hooks.on_pre_llm_call(
                user_message="What's happening with the Maldives JSC?")
        # If there are no fc_hits AND no const_hits the hook returns
        # None even on a match — that's allowed; we just want to check
        # that WHEN it returns, the shape is correct.
        if r is not None:
            self.assertIsInstance(r, dict)
            self.assertIn("context", r)
            self.assertIsInstance(r["context"], str)
            self.assertIn("Ambient kahzaabu context", r["context"])
            self.assertIn("Reminder", r["context"])

    def test_defensive_no_db_returns_setup_hint_then_none(self):
        """If the DB is missing, the FIRST match returns a one-time
        setup-hint context dict (so the user learns about the setup
        gap in their actual chat). Subsequent matches return None
        (silent no-op — avoids flooding the conversation)."""
        hooks._reset_db_missing_warning()
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            r1 = hooks.on_pre_llm_call(
                user_message="What did Muizzu announce yesterday?")
            r2 = hooks.on_pre_llm_call(
                user_message="Tell me about Muizzu's JSC reforms")
        self.assertIsInstance(r1, dict,
            "First match-without-DB must return a context dict so the "
            "user sees the setup hint in their reply, not just a log "
            "message they probably won't read")
        self.assertIn("context", r1)
        self.assertIn("setup", r1["context"].lower())
        self.assertIsNone(r2,
            "Second match-without-DB must be silent (otherwise "
            "every user turn would re-inject the hint)")

    def test_defensive_search_throws_returns_none(self):
        """If the search modules throw (e.g., a corrupt DB), the hook
        must swallow and return None."""
        from kahzaabu import factcheck_search
        with patch.object(factcheck_search, "search_fact_checks",
                           side_effect=RuntimeError("simulated failure")):
            # Need DB to exist for the call to reach search; mock that too
            with patch.object(hooks, "_resolve_db_path",
                               return_value=Path("/tmp/nonexistent.db")):
                # The connect itself will fail, also defensive
                r = hooks.on_pre_llm_call(
                    user_message="Tell me about the Maldives constitution")
                self.assertIsNone(r)


# ───────────────────────────────────────────────────────────────────
# Context formatting
# ───────────────────────────────────────────────────────────────────

class ContextFormat(unittest.TestCase):
    def test_format_truncates_to_max(self):
        # Force a huge result set
        big_fc = [
            {"id": i, "verdict_label": "REFUTED",
             "claim": "x" * 200}
            for i in range(100)
        ]
        out = hooks._format_context(big_fc, [])
        self.assertLessEqual(len(out), hooks._MAX_CONTEXT_CHARS + 1)

    def test_format_includes_reminder(self):
        out = hooks._format_context(
            [{"id": 1, "verdict_label": "REFUTED", "claim": "test"}],
            [])
        self.assertIn("Reminder", out,
            "Every ambient injection must remind the agent that "
            "kahzaabu is not authoritative — otherwise an agent "
            "could quote the fact-check verdict as fact")


# ───────────────────────────────────────────────────────────────────
# Sticky-session memory (Concern 2)
# ───────────────────────────────────────────────────────────────────

class StickySessionMemory(unittest.TestCase):
    """A session that's been marked hot (saw a strong Maldivian-politics
    match) should keep returning True for loose follow-up turns that
    would otherwise fail the strict prefilter.

    The motivating example: user asks "What did Muizzu announce?"
    (strong match, marks session hot). Next turn they say "what about
    housing?" — that alone has no Maldivian anchor, but in the context
    of THIS session it's obviously still on-topic."""

    def setUp(self):
        hooks._clear_sticky_state()

    def tearDown(self):
        hooks._clear_sticky_state()

    def test_loose_followup_in_hot_session_matches(self):
        sid = "session-abc"
        # Strong match — marks the session as hot.
        self.assertTrue(hooks._is_relevant(
            "What did Muizzu announce today?", session_id=sid))
        # Loose follow-up: "what about housing?" — no Maldivian anchor,
        # would normally not match. But the session is hot, so it
        # should still trigger the prefilter.
        self.assertTrue(hooks._is_relevant(
            "what about the housing scheme?", session_id=sid),
            "Sticky-session: loose follow-up in a hot session must "
            "still match — this is the whole point of session "
            "broadening")

    def test_loose_followup_in_cold_session_does_not_match(self):
        # No prior strong match: loose follow-up alone should NOT match.
        self.assertFalse(hooks._is_relevant(
            "what about the housing scheme?", session_id="brand-new"),
            "Sticky-session must NOT fire for sessions that never had "
            "a strong match — would be a global false-positive otherwise")

    def test_hot_session_eventually_expires(self):
        """Verify the TTL is wired. We age BOTH the in-memory entry
        and the persistent SQLite row — the persistent layer is what
        Concern 2's slice added, and without aging it the in-memory
        eviction alone won't make the session look cold."""
        # Disable the persistent layer for this test (would otherwise
        # need a per-test temp DB). _resolve_db_path=None makes
        # _mark_session_hot skip the SQLite write and _is_session_hot
        # skip the SQLite read — exercising just the in-memory TTL.
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            sid = "session-ttl"
            self.assertTrue(hooks._is_relevant(
                "Tell me about Muizzu's manifesto", session_id=sid))
            # Manually age the session beyond the TTL.
            with hooks._state_lock:
                hooks._session_hits[sid] = (
                    hooks.time.monotonic() - hooks._STICKY_TTL_SECONDS - 1
                )
            # Now loose follow-ups should NOT match.
            self.assertFalse(hooks._is_relevant(
                "what about housing?", session_id=sid),
                "Expired hot session must fall back to strict prefilter "
                "— otherwise sessions stay hot forever")

    def test_empty_session_id_does_not_corrupt_state(self):
        """When hermes sends an empty session_id, the sticky-session
        path must be a no-op (don't pollute the global state with an
        empty-string key)."""
        hooks._is_relevant("Muizzu announces JSC amendments", session_id="")
        with hooks._state_lock:
            self.assertNotIn("", hooks._session_hits,
                "Empty session_id must not be recorded as hot — would "
                "make EVERY session with empty id share the same hot "
                "state, defeating the per-session contract")

    def test_lru_eviction_caps_dict_size(self):
        """Memory bound: the sticky-session dict can't grow unboundedly
        under runaway session-id churn (automated test harnesses, etc.).
        We push past the cap and verify the size stays bounded."""
        # Push 1.5x the cap
        for i in range(int(hooks._STICKY_MAX_ENTRIES * 1.5)):
            hooks._mark_session_hot(f"session-{i}")
        with hooks._state_lock:
            self.assertLessEqual(
                len(hooks._session_hits),
                hooks._STICKY_MAX_ENTRIES,
                "LRU eviction must cap the dict size — otherwise a "
                "test harness firing many sessions could OOM the "
                "hermes process")


# ───────────────────────────────────────────────────────────────────
# Per-platform whitelist (Concern 3)
# ───────────────────────────────────────────────────────────────────

class PlatformAllowlist(unittest.TestCase):
    """KAHZAABU_AMBIENT_PLATFORMS=cli,telegram lets users enable the
    ambient hook in their personal flow (CLI, Telegram DMs) while
    keeping it quiet in group chats (Slack, Discord)."""

    def test_unset_allowlist_means_all_platforms(self):
        with patch.dict("os.environ", {}, clear=False):
            import os as _os
            _os.environ.pop("KAHZAABU_AMBIENT_PLATFORMS", None)
            self.assertTrue(hooks._platform_allowed("cli"))
            self.assertTrue(hooks._platform_allowed("telegram"))
            self.assertTrue(hooks._platform_allowed("slack"))
            self.assertTrue(hooks._platform_allowed("anything"))

    def test_allowlist_honoured(self):
        with patch.dict("os.environ",
                          {"KAHZAABU_AMBIENT_PLATFORMS": "cli,telegram"}):
            self.assertTrue(hooks._platform_allowed("cli"))
            self.assertTrue(hooks._platform_allowed("telegram"))
            self.assertTrue(hooks._platform_allowed("TELEGRAM"),
                "case-insensitive — operators write 'Telegram' or "
                "'telegram' interchangeably")
            self.assertFalse(hooks._platform_allowed("slack"))
            self.assertFalse(hooks._platform_allowed("discord"))

    def test_hook_returns_none_for_disallowed_platform(self):
        with patch.dict("os.environ", {"KAHZAABU_AMBIENT_PLATFORMS": "cli"}):
            r = hooks.on_pre_llm_call(
                user_message="What did Muizzu announce?",
                platform="slack")
            self.assertIsNone(r,
                "Hook must NOT fire on a platform that's not on the "
                "allowlist — that's the whole point of the env var")


# ───────────────────────────────────────────────────────────────────
# One-time DB-missing warning (Concern 1)
# ───────────────────────────────────────────────────────────────────

class DBMissingWarning(unittest.TestCase):
    def setUp(self):
        hooks._reset_db_missing_warning()
        hooks._clear_sticky_state()

    def test_warns_once_on_match_with_no_db(self):
        """The warning fires only on the FIRST match-without-DB, then
        stays quiet so the log doesn't flood. We assert by checking
        the module-level flag is set after the first call — a second
        call would short-circuit (see _handle_db_missing_once)."""
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            with self.assertLogs("plugins.kahzaabu.hooks",
                                  level="WARNING") as cm:
                # First match — should warn
                hooks.on_pre_llm_call(
                    user_message="What did Muizzu announce?",
                    session_id="s1")
                self.assertTrue(any("hermes kahzaabu setup" in m
                                     for m in cm.output),
                    f"First DB-missing match must log a setup hint; "
                    f"got: {cm.output}")

            # Confirm the flag is set — guarantees a second call
            # would short-circuit inside _handle_db_missing_once.
            self.assertTrue(hooks._db_missing_warned,
                "First warning must set the once-only flag so a "
                "second invocation skips the log call entirely")

            # Belt-and-braces: actually call again and verify no
            # NEW "kahzaabu setup" log appears (we can't use
            # assertNoLogs because it's Python 3.10+).
            handler = logging.handlers.MemoryHandler(capacity=10)
            log = logging.getLogger("plugins.kahzaabu.hooks")
            log.addHandler(handler)
            try:
                hooks.on_pre_llm_call(
                    user_message="Tell me about Muizzu's JSC reforms",
                    session_id="s2")
                second_call_warnings = [
                    r for r in handler.buffer
                    if r.levelno >= logging.WARNING
                    and "kahzaabu setup" in r.getMessage()
                ]
                self.assertEqual(second_call_warnings, [],
                    "Second DB-missing match must NOT re-warn — "
                    "otherwise every user turn floods the log")
            finally:
                log.removeHandler(handler)


# ───────────────────────────────────────────────────────────────────
# hook_status() — consumed by `hermes kahzaabu doctor`
# ───────────────────────────────────────────────────────────────────

class HookStatusDiagnostic(unittest.TestCase):
    def test_disabled_by_env(self):
        with patch.dict("os.environ", {"KAHZAABU_AMBIENT_DISABLE": "1"}):
            s = hooks.hook_status()
            self.assertFalse(s["enabled"])
            self.assertEqual(s["disable_reason"], "env")

    def test_no_db(self):
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            import os as _os
            _os.environ.pop("KAHZAABU_AMBIENT_DISABLE", None)
            s = hooks.hook_status()
            self.assertFalse(s["enabled"])
            self.assertEqual(s["disable_reason"], "no_db",
                "Doctor needs to distinguish 'env-disabled' from "
                "'DB-missing' — they have different fixes")

    def test_allowlist_surfaced(self):
        with patch.dict("os.environ",
                          {"KAHZAABU_AMBIENT_PLATFORMS": "cli,telegram"}):
            s = hooks.hook_status()
            self.assertEqual(sorted(s["platform_allowlist"]),
                              ["cli", "telegram"])


# ───────────────────────────────────────────────────────────────────
# Corpus-derived follow-up vocabulary (Concern 1 follow-up)
# ───────────────────────────────────────────────────────────────────

class CorpusDerivedFollowupVocab(unittest.TestCase):
    """The sticky-session follow-up regex should pull discrete topic
    tokens from the live fact_checks.topic column, on top of the
    hardcoded baseline. New topics added by the pipeline propagate
    without a hand edit."""

    def setUp(self):
        hooks._clear_followup_pattern_cache()
        hooks._clear_sticky_state()
        hooks._reset_db_missing_warning()

    def test_corpus_topics_appear_in_pattern(self):
        """Build a fixture DB with a non-baseline topic, then check
        the pattern matches it."""
        import sqlite3
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            from kahzaabu.claims_db import init_full_schema
            conn = sqlite3.connect(str(db_path))
            init_full_schema(conn)
            # Insert a fact_check with a topic that's NOT in the baseline.
            conn.execute(
                "INSERT INTO fact_checks "
                "(id, category, claim, claim_date, topic, confidence, "
                " source_article_ids, evidence_quotes, created_at, "
                " published, verdict_label) VALUES "
                "(?, 'LIE', 'test', '2026-01-01', 'cryptocurrency_regulation', "
                " 'reviewed', '[]', '[]', '2026-01-01T00:00:00Z', 1, 'REFUTED')",
                (9999,)
            )
            conn.commit()
            conn.close()
            with patch.dict("os.environ", {"KAHZAABU_DB": str(db_path)}):
                # Force rebuild of the cached pattern against this DB.
                hooks._clear_followup_pattern_cache()
                pat = hooks._followup_pattern()
                # Both corpus words should be matchable
                self.assertTrue(pat.search("any updates on cryptocurrency?"),
                    "Corpus-derived 'cryptocurrency' (from the topic) must "
                    "match the sticky-session follow-up pattern")
                self.assertTrue(pat.search("what about regulation?"),
                    "Corpus-derived 'regulation' (from the topic) must "
                    "match the sticky-session follow-up pattern")
        finally:
            db_path.unlink(missing_ok=True)

    def test_pattern_falls_back_to_baseline_when_no_db(self):
        """Without a DB the pattern must still work — only the
        baseline keywords are available, but they should still
        match the common cases."""
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            hooks._clear_followup_pattern_cache()
            pat = hooks._followup_pattern()
            # Baseline keywords still fire
            self.assertTrue(pat.search("what about the housing scheme?"))
            self.assertTrue(pat.search("any updates on the election?"))


# ───────────────────────────────────────────────────────────────────
# Cross-process hot-session persistence (Concern 2 follow-up)
# ───────────────────────────────────────────────────────────────────

class CrossProcessHotSessions(unittest.TestCase):
    """When the DB is available, hot-session state must survive a
    process restart (or be visible to a sibling process). This is
    what makes the sticky path work in multi-process hermes deployments
    where adapter platforms run as separate processes."""

    def setUp(self):
        hooks._clear_sticky_state()
        hooks._reset_db_missing_warning()

    def tearDown(self):
        hooks._clear_sticky_state()

    def test_persisted_to_sqlite_when_db_available(self):
        """Marking a session hot should write a row to
        ambient_hot_sessions when the DB is present."""
        import sqlite3
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            from kahzaabu.claims_db import init_full_schema
            conn = sqlite3.connect(str(db_path))
            init_full_schema(conn)
            conn.close()
            with patch.dict("os.environ", {"KAHZAABU_DB": str(db_path)}):
                hooks._mark_session_hot("crossproc-1")
                # Read the row back from a separate connection
                conn = sqlite3.connect(str(db_path))
                row = conn.execute(
                    "SELECT session_id, hot_until FROM ambient_hot_sessions "
                    "WHERE session_id = ?",
                    ("crossproc-1",)
                ).fetchone()
                conn.close()
                self.assertIsNotNone(row,
                    "_mark_session_hot must persist to "
                    "ambient_hot_sessions when a DB is available")
                self.assertEqual(row[0], "crossproc-1")
                import time as _t
                self.assertGreater(row[1], _t.time(),
                    "hot_until must be in the future")
        finally:
            db_path.unlink(missing_ok=True)

    def test_simulated_separate_process_sees_hot_session(self):
        """Wipe in-memory state to simulate a sibling process that
        hasn't seen the session before, then verify _is_session_hot
        still returns True via the DB read."""
        import sqlite3
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            from kahzaabu.claims_db import init_full_schema
            conn = sqlite3.connect(str(db_path))
            init_full_schema(conn)
            conn.close()
            with patch.dict("os.environ", {"KAHZAABU_DB": str(db_path)}):
                hooks._mark_session_hot("crossproc-2")
                # Simulate sibling process — wipe in-memory dict
                with hooks._state_lock:
                    hooks._session_hits.clear()
                # The "sibling" should still see the hot session
                self.assertTrue(hooks._is_session_hot("crossproc-2"),
                    "After wiping in-memory state, _is_session_hot "
                    "must still return True via the SQLite read — "
                    "this is what makes cross-process stickiness work")
        finally:
            db_path.unlink(missing_ok=True)

    def test_expired_rows_garbage_collected_on_read(self):
        """Lazy GC: when reading, drop any rows whose hot_until is
        in the past. Prevents the table from growing unboundedly."""
        import sqlite3
        import tempfile
        import time as _t
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            from kahzaabu.claims_db import init_full_schema
            conn = sqlite3.connect(str(db_path))
            init_full_schema(conn)
            # Manually insert an expired row + a fresh one
            conn.execute(
                "INSERT INTO ambient_hot_sessions VALUES (?, ?)",
                ("expired-session", _t.time() - 60))
            conn.execute(
                "INSERT INTO ambient_hot_sessions VALUES (?, ?)",
                ("fresh-session", _t.time() + 600))
            conn.commit()
            conn.close()
            with patch.dict("os.environ", {"KAHZAABU_DB": str(db_path)}):
                with hooks._state_lock:
                    hooks._session_hits.clear()
                # Trigger lazy GC by querying the expired session
                self.assertFalse(hooks._is_session_hot("expired-session"))
                # Confirm the expired row was GC'd
                conn = sqlite3.connect(str(db_path))
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM ambient_hot_sessions "
                    "WHERE session_id = 'expired-session'"
                ).fetchone()[0]
                conn.close()
                self.assertEqual(cnt, 0,
                    "Lazy GC must drop expired rows on read — "
                    "otherwise the table grows unboundedly over time")
        finally:
            db_path.unlink(missing_ok=True)


# ───────────────────────────────────────────────────────────────────
# TTL refresh on the corpus vocab cache (Concern 2 follow-up)
# ───────────────────────────────────────────────────────────────────

class FollowupVocabTTL(unittest.TestCase):
    """Long-lived hermes daemon must pick up new pipeline-emitted
    topics without restart. We do this with a TTL-based rebuild —
    test that aging the cache-build timestamp triggers a rebuild
    on the next call."""

    def setUp(self):
        hooks._clear_followup_pattern_cache()

    def test_env_var_overrides_default_ttl(self):
        """KAHZAABU_FOLLOWUP_TTL_SECONDS=N must be honoured at next
        read — lazy reader, so a runtime env change takes effect
        without restart."""
        with patch.dict("os.environ",
                          {"KAHZAABU_FOLLOWUP_TTL_SECONDS": "120"}):
            self.assertEqual(hooks._followup_ttl_seconds(), 120)
        # Unset reverts to default
        import os as _os
        _os.environ.pop("KAHZAABU_FOLLOWUP_TTL_SECONDS", None)
        self.assertEqual(hooks._followup_ttl_seconds(),
                           hooks._FOLLOWUP_CACHE_TTL_DEFAULT_SECONDS)

    def test_malformed_env_var_falls_back_to_default(self):
        """A typo or negative value in KAHZAABU_FOLLOWUP_TTL_SECONDS
        must not crash the hook — fall back to default."""
        for bad in ("not-an-int", "-1", "0", ""):
            with patch.dict("os.environ",
                              {"KAHZAABU_FOLLOWUP_TTL_SECONDS": bad}):
                self.assertEqual(
                    hooks._followup_ttl_seconds(),
                    hooks._FOLLOWUP_CACHE_TTL_DEFAULT_SECONDS,
                    f"Malformed env value {bad!r} must fall back "
                    "to the default TTL, not crash or use a bad value")

    def test_cache_rebuilt_after_ttl_expires(self):
        # First call builds the cache
        pat1 = hooks._followup_pattern()
        built1 = hooks._followup_built_at
        self.assertIsNotNone(built1)

        # Same call within TTL — same object, no rebuild
        pat2 = hooks._followup_pattern()
        self.assertIs(pat1, pat2,
            "Within TTL the pattern object must be reused — no rebuild")

        # Age the build-timestamp past the TTL (whatever the current
        # env-derived value is — defaults to 6h, but _followup_ttl_seconds
        # is the authoritative reader).
        with hooks._state_lock:
            hooks._followup_built_at = (
                built1 - hooks._followup_ttl_seconds() - 1
            )
        # Next call must rebuild — different timestamp at minimum
        pat3 = hooks._followup_pattern()
        self.assertGreater(hooks._followup_built_at, built1,
            "After TTL, _followup_pattern must rebuild and update the "
            "built-at timestamp — otherwise long-lived hermes daemons "
            "would never pick up new corpus topics")


# ───────────────────────────────────────────────────────────────────
# DB-missing hint only on STRONG matches (Concern 3 follow-up)
# ───────────────────────────────────────────────────────────────────

class DBMissingHintGating(unittest.TestCase):
    """The inline 'run setup' hint goes into the user's chat context.
    It should only fire when the user clearly asked about Maldivian
    politics (strong-keyword match), not when they happened to mention
    a Maldivian anchor in passing (co-occurrence) or are doing a
    loose follow-up in an already-hot session (sticky)."""

    def setUp(self):
        hooks._reset_db_missing_warning()
        hooks._clear_sticky_state()

    def test_strong_match_with_no_db_injects_hint(self):
        """User explicitly asks about Maldivian politics; we owe them
        the setup hint."""
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            r = hooks.on_pre_llm_call(
                user_message="What did Muizzu announce today?",
                session_id="strong-1")
        self.assertIsInstance(r, dict)
        self.assertIn("setup", r["context"].lower())

    def test_sticky_match_with_no_db_stays_silent(self):
        """In an already-hot session, a loose follow-up is a sticky
        match. With no DB the hint should NOT re-fire — even if it
        never fired for the original strong match (because of some
        prior call ordering), the user is mid-conversation and we
        don't want to interrupt."""
        # Force the in-memory state hot but DB missing
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            hooks._mark_session_hot("sticky-1")  # no-ops persistence
            # Reset the warned flag so we know the test is exercising
            # the gating, not the one-time flag
            hooks._reset_db_missing_warning()
            r = hooks.on_pre_llm_call(
                user_message="what about the housing scheme?",
                session_id="sticky-1")
        self.assertIsNone(r,
            "Sticky + no-DB must stay silent — we already had a chance "
            "to inject the hint on the original strong match")

    def test_warned_flag_only_set_after_strong_match(self):
        """The one-time flag should only be consumed by a STRONG match.
        A STICKY match (loose follow-up in an already-hot session) must
        not consume the one-time hint slot — the strong-match user
        deserves it. (The cooccurrence path was removed; sticky is the
        only non-strong code path now.)"""
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            # Force a sticky-only condition: mark hot in-memory but
            # don't trigger via the normal path (which would inject
            # the hint on the strong match itself).
            hooks._mark_session_hot("sticky-only")
            hooks._reset_db_missing_warning()
            hooks.on_pre_llm_call(
                user_message="what about the housing scheme?",
                session_id="sticky-only")
            self.assertFalse(hooks._db_missing_warned,
                "Sticky-only + no-DB must not consume the one-time "
                "hint slot — strong-match users deserve it")
            # Now a strong match in a NEW session — flag should be set
            r = hooks.on_pre_llm_call(
                user_message="What did Muizzu do?",
                session_id="strong-after")
            self.assertIsNotNone(r,
                "Strong match must still inject the hint after "
                "sticky-only path happened first")
            self.assertTrue(hooks._db_missing_warned)


# ───────────────────────────────────────────────────────────────────
# Match-strength classifier (Concern 3 follow-up — internal API)
# ───────────────────────────────────────────────────────────────────

class MatchClassifier(unittest.TestCase):
    """_classify_match exposes the prefilter's WHY for callers that
    need to route on match strength (e.g. the DB-missing hint)."""

    def setUp(self):
        hooks._clear_sticky_state()

    def test_strong_keyword_returns_match_strong(self):
        self.assertEqual(
            hooks._classify_match("What did Muizzu announce?",
                                    session_id="cl1"),
            hooks.MATCH_STRONG)

    def test_irrelevant_returns_match_none(self):
        self.assertEqual(
            hooks._classify_match("How do I write a Python decorator?",
                                    session_id="cl3"),
            hooks.MATCH_NONE)

    def test_sticky_returns_match_sticky(self):
        sid = "cl4"
        hooks._mark_session_hot(sid)  # bypass strong-match path
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            self.assertEqual(
                hooks._classify_match("what about housing?",
                                        session_id=sid),
                hooks.MATCH_STICKY)


# ───────────────────────────────────────────────────────────────────
# Critical-keyword invariants (Concern 1 follow-up)
# ───────────────────────────────────────────────────────────────────

class StrongKeywordInvariants(unittest.TestCase):
    """The ambient-hook prefilter has no weak-anchor / co-occurrence
    fallback (removed 2026-05-22). All ambient injection routes
    through the strong-keyword regex. If a future refactor strips
    a critical stem like "maldiv" or "muizzu" by accident, ambient
    coverage silently degrades.

    These tests are the safety net: a structural assertion that the
    compiled pattern contains specific load-bearing tokens, plus
    positive matches that fail loudly with named-message error
    messages if anything drops out."""

    # Each entry: (token_substring, test_message, why_it_matters)
    CRITICAL_KEYWORDS = [
        ("muizzu",   "What did Muizzu announce yesterday?",
                     "the President's name — kahzaabu's namesake speaker"),
        ("kahzaabu", "Tell me about kahzaabu's archive",
                     "the project name itself"),
        ("maldiv",   "How does the Maldives Constitution define citizenship?",
                     "country stem matches Maldiv / Maldives / Maldivian"),
        ("majlis",   "What's happening at the Majlis?",
                     "the parliament — primary political institution"),
        ("JSC",      "Are the JSC amendments lawful?",
                     "Judicial Service Commission — load-bearing in many "
                     "fact-checks about judicial independence"),
        ("atoll",    "Reclamation work in the southern atolls",
                     "almost-unique Maldivian English geography term"),
        ("raajje",   "The Raajje vs Maldives naming convention",
                     "Dhivehi-language country name — appears in DV-EN "
                     "comparison contexts"),
    ]

    def test_compiled_pattern_contains_each_critical_stem(self):
        """Structural assertion: the compiled regex's source must
        contain each critical token. If a refactor strips one, this
        fails with a clear named-token error message."""
        pat_src = hooks._AMBIENT_KEYWORDS.pattern.lower()
        for tok, _msg, reason in self.CRITICAL_KEYWORDS:
            with self.subTest(token=tok):
                self.assertIn(tok.lower(), pat_src,
                    f"Critical strong-keyword stem '{tok}' is missing "
                    f"from _AMBIENT_KEYWORDS regex source. {reason}. "
                    "Removing this silently degrades ambient context "
                    "injection for an important class of user queries.")

    def test_each_critical_keyword_actually_matches(self):
        """Behavioural assertion: a real message containing each
        keyword must produce MATCH_STRONG. Structural test above
        catches token-level drops; this catches subtler issues like
        word-boundary regressions or accidental ordering changes."""
        hooks._clear_sticky_state()
        for tok, msg, reason in self.CRITICAL_KEYWORDS:
            with self.subTest(token=tok, msg=msg):
                self.assertEqual(
                    hooks._classify_match(msg, session_id=f"cv-{tok}"),
                    hooks.MATCH_STRONG,
                    f"Critical keyword '{tok}' failed to produce "
                    f"MATCH_STRONG on the test message {msg!r}. "
                    f"{reason}. This is the safety net the previous "
                    "test won't catch — e.g., a word-boundary change "
                    "that doesn't strip the token but breaks matching.")


# ───────────────────────────────────────────────────────────────────
# Manifest declares the hook
# ───────────────────────────────────────────────────────────────────

class ManifestDeclaresHook(unittest.TestCase):
    """plugin.yaml MUST list pre_llm_call under hooks; otherwise hermes
    won't dispatch the event to us and the file we built does nothing."""

    def test_pre_llm_call_in_manifest(self):
        import yaml
        m = yaml.safe_load(
            (ROOT / "hermes-plugin" / "plugin.yaml").read_text())
        self.assertIn("pre_llm_call", m.get("hooks") or [],
            "plugin.yaml.hooks must include 'pre_llm_call' for the "
            "ambient hook to receive events — silent regression "
            "otherwise (file builds OK, hook never fires)")

    def test_optional_env_declares_disable_var(self):
        import yaml
        m = yaml.safe_load(
            (ROOT / "hermes-plugin" / "plugin.yaml").read_text())
        names = [e["name"] for e in m.get("optional_env") or []]
        self.assertIn("KAHZAABU_AMBIENT_DISABLE", names,
            "optional_env must document the opt-out var so the "
            "setup wizard surfaces it")
        self.assertIn("KAHZAABU_AMBIENT_PLATFORMS", names,
            "optional_env must document the platform allowlist var "
            "so users can scope the hook without disabling it")
        self.assertIn("KAHZAABU_FOLLOWUP_TTL_SECONDS", names,
            "optional_env must document the TTL override var so "
            "operators discoverable it via `hermes config`")


if __name__ == "__main__":
    unittest.main()
