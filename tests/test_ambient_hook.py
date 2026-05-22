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

    def test_defensive_no_db_returns_none(self):
        """If the DB is missing the hook must return None — never
        propagate an exception into the hermes turn."""
        with patch.object(hooks, "_resolve_db_path", return_value=None):
            r = hooks.on_pre_llm_call(
                user_message="What did Muizzu announce yesterday?")
            self.assertIsNone(r)

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
        """Verify the TTL is wired. We patch the TTL constant down
        and then manually-age the session entry."""
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
        call would short-circuit (see _warn_db_missing_once)."""
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
            # would short-circuit inside _warn_db_missing_once.
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


if __name__ == "__main__":
    unittest.main()
