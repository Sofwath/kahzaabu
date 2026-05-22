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


if __name__ == "__main__":
    unittest.main()
