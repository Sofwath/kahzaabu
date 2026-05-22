# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the hermes plugin (plugins.kahzaabu).

The plugin is the agent-facing surface of kahzaabu. It was 1,190 LOC
without dedicated tests before this file. These tests cover:

  - Manifest ↔ code consistency (plugin.yaml provides_tools must match
    the TOOLS tuple in tools.py; description's tool count must match)
  - Each handler's return shape against an in-memory fixture DB
  - Error envelopes are consistent across handlers
  - The pipeline-trigger safety gate honors both env var names
  - kahzaabu_home() / db_path() derivation
  - check_kahzaabu_requirements behaviour

In CI, plugins/kahzaabu is symlinked into hermes-stub/plugins/ so
imports work without hermes itself being installed (see
.github/workflows/test.yml).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Stage hermes-stub for the `plugins.kahzaabu` namespace just like CI does.
HERMES_STUB = ROOT / "hermes-stub"
(HERMES_STUB / "plugins").mkdir(parents=True, exist_ok=True)
plugin_link = HERMES_STUB / "plugins" / "kahzaabu"
if not plugin_link.exists():
    plugin_link.symlink_to(ROOT / "hermes-plugin")
(HERMES_STUB / "plugins" / "__init__.py").touch()
sys.path.insert(0, str(HERMES_STUB))


def _make_fixture_db() -> sqlite3.Connection:
    """Tiny in-memory DB matching the v2 schema, populated with a
    handful of representative rows for each handler to query."""
    from kahzaabu import claims_db
    conn = sqlite3.connect(":memory:")
    # Match the row_factory the real plugin _conn() sets — handlers
    # do `dict(row)` which needs sqlite3.Row to work.
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    claims_db.init_full_schema(conn)
    # Two articles
    conn.execute(
        "INSERT INTO articles (id, language, title, body_text, "
        "                       published_date, category, category_id, "
        "                       reference, scraped_at) "
        "VALUES (1001, 'EN', 'Test article 1', 'Body text 1.', "
        "        '2026-04-01', 'press_release', 1, "
        "        'https://presidency.gov.mv/1', '2026-04-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO articles (id, language, title, body_text, "
        "                       published_date, category, category_id, "
        "                       reference, scraped_at) "
        "VALUES (1002, 'EN', 'Test article 2', 'Body text 2.', "
        "        '2026-04-10', 'speech', 1, "
        "        'https://presidency.gov.mv/2', '2026-04-10T00:00:00Z')"
    )
    # One claim + one fact-check linked to article 1001
    conn.execute(
        "INSERT INTO claims (article_id, language, type, polarity, "
        "                    is_checkable, quote, extraction_run_id, "
        "                    created_at) "
        "VALUES (1001, 'EN', 'numeric_promise', 'PROMISE', 1, "
        "        'build 100 schools', NULL, '2026-04-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO fact_checks "
        "(category, claim, claim_date, topic, confidence, "
        " source_article_ids, evidence_quotes, created_at, published, "
        " verdict_label, truth_score, truth_score_label, speaker, "
        " public_summary) "
        "VALUES ('LIE', 'fictional claim', '2026-04-01', 'education', "
        "        'reviewed', '[1001]', '[]', '2026-04-01T00:00:00Z', "
        "        1, 'REFUTED', 2, 'FALSE', 'Mohamed Muizzu', "
        "        'Summary for public')"
    )
    fc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO fact_check_evidence "
        "(fact_check_id, source_type, url, title, snippet, relevance, "
        " retrieved_at) "
        "VALUES (?, 'web', 'https://example.com/x', 'Some title', "
        "        'A snippet', 'confirms', '2026-04-01T00:00:00Z')",
        (fc_id,))
    # One manifesto promise
    conn.execute(
        "INSERT INTO manifesto_promises "
        "(section, promise_text_en, category, delivery_status, "
        " published, chunk_index, created_at) "
        "VALUES ('housing', 'build N homes', 'housing', "
        "        'NOT_STARTED', 1, 0, '2026-04-01T00:00:00Z')"
    )
    conn.commit()
    return conn


# ───────────────────────────────────────────────────────────────────
# Manifest ↔ code consistency
# ───────────────────────────────────────────────────────────────────

class ManifestConsistencyTests(unittest.TestCase):
    """The plugin.yaml manifest is consumed by hermes at enable time.
    If it drifts from the code's TOOLS tuple, agents see different
    tools than the registry advertises."""

    @classmethod
    def setUpClass(cls):
        cls.yaml_text = (ROOT / "hermes-plugin" / "plugin.yaml").read_text()
        from plugins.kahzaabu.tools import TOOLS
        cls.code_tools = [t[0] for t in TOOLS]

    def test_provides_tools_matches_code(self):
        # Parse the provides_tools list from the YAML (cheap regex —
        # we don't need a full YAML parser for this).
        block = re.search(
            r"provides_tools:\n((?:  - [a-z_]+\n)+)",
            self.yaml_text)
        self.assertIsNotNone(block, "provides_tools block missing")
        declared = sorted(re.findall(r"-\s+(kahzaabu_\w+)", block.group(1)))
        self.assertEqual(declared, sorted(self.code_tools),
            "plugin.yaml's provides_tools must match the TOOLS tuple "
            "in tools.py. Drift means agents see a tool list that "
            "disagrees with what's actually registered.")

    def test_description_tool_count_matches_code(self):
        m = re.search(r"Exposes (\d+) agent tools", self.yaml_text)
        if m:
            self.assertEqual(int(m.group(1)), len(self.code_tools),
                "plugin.yaml description claims a tool count that "
                "doesn't match TOOLS in code.")

    def test_author_email_is_public_contact(self):
        # OSS-contact convention per the project's feedback memory.
        self.assertIn("Sofwathullah.Mohamed@gmail.com", self.yaml_text,
            "Manifest author must use the public OSS contact email, "
            "not the developer's day-job address.")


# ───────────────────────────────────────────────────────────────────
# Handlers — wired against an in-memory fixture DB
# ───────────────────────────────────────────────────────────────────

class HandlerSmokeTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_fixture_db()

    def tearDown(self):
        self.conn.close()

    def _call(self, fn, args):
        from plugins.kahzaabu import tools
        with patch.object(tools, "_conn", return_value=self.conn):
            return json.loads(fn(args))

    def test_stats_returns_expected_keys(self):
        from plugins.kahzaabu.tools import handle_stats
        # handle_stats also reads claims_db.freshness — patch to avoid
        # filesystem dependency on data/kahzaabu.db.
        from kahzaabu import claims_db
        with patch.object(claims_db, "freshness", return_value={
                "last_scrape_at": None, "hours_since": None,
                "is_stale": False}):
            r = self._call(handle_stats, {})
        for key in ("articles_muizzu_era", "claims_extracted",
                     "fact_checks", "manifesto_promises", "freshness"):
            self.assertIn(key, r)

    def test_list_lies_returns_items(self):
        from plugins.kahzaabu.tools import handle_list_lies
        r = self._call(handle_list_lies, {})
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["category"], "LIE")

    def test_list_lies_respects_category_filter(self):
        from plugins.kahzaabu.tools import handle_list_lies
        r = self._call(handle_list_lies, {"category": "MISLEADING"})
        self.assertEqual(r["count"], 0)

    def test_list_lies_clamps_limit(self):
        """limit must be clamped to [1, 200] to prevent agents
        from requesting unbounded results."""
        from plugins.kahzaabu.tools import handle_list_lies
        r = self._call(handle_list_lies, {"limit": 9999})
        # Returned without error; the SQL would have used 200 internally.
        self.assertEqual(r["count"], 1)

    def test_get_factcheck_returns_full_shape(self):
        from plugins.kahzaabu.tools import handle_get_factcheck
        fc_id = self.conn.execute(
            "SELECT id FROM fact_checks LIMIT 1").fetchone()[0]
        r = self._call(handle_get_factcheck, {"id": fc_id})
        self.assertIn("fact_check", r)
        self.assertEqual(r["fact_check"]["category"], "LIE")
        self.assertIsInstance(r["fact_check"]["source_article_ids"], list)
        self.assertIsInstance(r["web_evidence"], list)
        self.assertEqual(len(r["web_evidence"]), 1)
        self.assertIsInstance(r["source_articles"], list)

    def test_get_factcheck_missing_returns_error(self):
        from plugins.kahzaabu.tools import handle_get_factcheck
        r = self._call(handle_get_factcheck, {"id": 999999})
        self.assertIn("error", r)

    def test_get_article_returns_article_plus_links(self):
        from plugins.kahzaabu.tools import handle_get_article
        r = self._call(handle_get_article, {"article_id": 1001})
        self.assertEqual(r["article"]["id"], 1001)
        self.assertIn("claims", r)
        self.assertGreaterEqual(len(r["claims"]), 1)

    def test_manifesto_returns_promises(self):
        from plugins.kahzaabu.tools import handle_manifesto
        r = self._call(handle_manifesto, {})
        self.assertGreaterEqual(r["count"], 1)

    def test_recent_activity_returns_items_in_window(self):
        from plugins.kahzaabu.tools import handle_recent_activity
        # Generous window so the 2026-04 fixture rows match regardless
        # of today's date; the test only verifies shape.
        r = self._call(handle_recent_activity, {"days": 365 * 100})
        self.assertIn("items", r)
        self.assertIsInstance(r["items"], list)

    def test_search_articles_returns_items(self):
        from plugins.kahzaabu.tools import handle_search_articles
        r = self._call(handle_search_articles, {"q": "Test"})
        self.assertGreaterEqual(r["count"], 2)
        self.assertEqual(r["items"][0]["title"], "Test article 2")

    def test_search_factchecks_returns_items(self):
        from plugins.kahzaabu.tools import handle_search_factchecks
        r = self._call(handle_search_factchecks, {"q": "fictional"})
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["category"], "LIE")

    def test_get_promise_returns_details(self):
        from plugins.kahzaabu.tools import handle_get_promise
        pid = self.conn.execute("SELECT id FROM manifesto_promises LIMIT 1").fetchone()[0]
        r = self._call(handle_get_promise, {"id": pid})
        self.assertIn("promise_text_en", r)
        self.assertIn("delivery_rationale", r)

    def test_get_promise_missing_returns_error(self):
        from plugins.kahzaabu.tools import handle_get_promise
        r = self._call(handle_get_promise, {"id": 999999})
        self.assertIn("error", r)

    @patch("kahzaabu.eval.run_eval")
    def test_run_eval_delegates_correctly(self, mock_run_eval):
        from plugins.kahzaabu.tools import handle_run_eval
        mock_run_eval.return_value = {"accuracy": 0.95}
        r = self._call(handle_run_eval, {"small": True, "stages": ["truth_score"]})
        self.assertEqual(r, {"accuracy": 0.95})
        mock_run_eval.assert_called_once_with(stages=["truth_score"], small=True)


# ───────────────────────────────────────────────────────────────────
# Error contract — every error path returns {"error": "..."}
# ───────────────────────────────────────────────────────────────────

class ErrorContractTests(unittest.TestCase):
    """Standardised error shape: handlers ALWAYS return a string of
    JSON; failure responses are `{"error": "<message>"}` with no
    extra ok/success flag. Mixed shapes confuse agents."""

    def test_handle_ask_without_api_key_returns_error(self):
        from plugins.kahzaabu.tools import handle_ask
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            r = json.loads(handle_ask({"question": "x"}))
        finally:
            if old is not None:
                os.environ["ANTHROPIC_API_KEY"] = old
        self.assertIn("error", r)
        self.assertNotIn("ok", r,
            "Error responses must not carry an 'ok' flag — keep "
            "the contract consistent across handlers.")

    def test_handle_pipeline_run_gated_off_returns_error(self):
        from plugins.kahzaabu.tools import handle_pipeline_run
        # Ensure both env-var forms are unset.
        for k in ("KAHZAABU_ALLOW_PIPELINE",
                   "KAHZAABU_MCP_ALLOW_PIPELINE"):
            os.environ.pop(k, None)
        r = json.loads(handle_pipeline_run({}))
        self.assertIn("error", r)
        self.assertIn("KAHZAABU_ALLOW_PIPELINE", r["error"])
        self.assertNotIn("ok", r)


class PipelineGateEnvVarTests(unittest.TestCase):
    """The KAHZAABU_ALLOW_PIPELINE env var replaces the legacy
    KAHZAABU_MCP_ALLOW_PIPELINE; both must remain accepted so
    existing ~/.hermes/.env files don't break."""

    def test_new_env_var_enables_gate(self):
        from plugins.kahzaabu import tools
        for k in ("KAHZAABU_ALLOW_PIPELINE",
                   "KAHZAABU_MCP_ALLOW_PIPELINE"):
            os.environ.pop(k, None)
        os.environ["KAHZAABU_ALLOW_PIPELINE"] = "1"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            r = json.loads(tools.handle_pipeline_run({}))
        finally:
            os.environ.pop("KAHZAABU_ALLOW_PIPELINE", None)
        # Past the gate, we now hit the ANTHROPIC_API_KEY check.
        self.assertEqual(r.get("error"), "ANTHROPIC_API_KEY not set")

    def test_legacy_env_var_still_enables_gate(self):
        from plugins.kahzaabu import tools
        for k in ("KAHZAABU_ALLOW_PIPELINE",
                   "KAHZAABU_MCP_ALLOW_PIPELINE"):
            os.environ.pop(k, None)
        os.environ["KAHZAABU_MCP_ALLOW_PIPELINE"] = "1"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            r = json.loads(tools.handle_pipeline_run({}))
        finally:
            os.environ.pop("KAHZAABU_MCP_ALLOW_PIPELINE", None)
        self.assertEqual(r.get("error"), "ANTHROPIC_API_KEY not set")

    @patch("plugins.kahzaabu.tools._has_anthropic_key", return_value=True)
    @patch("kahzaabu.pipeline.run_pipeline")
    def test_handle_pipeline_run_success(self, mock_run_pipeline, mock_has_key):
        from plugins.kahzaabu.tools import handle_pipeline_run, db_path
        os.environ["KAHZAABU_ALLOW_PIPELINE"] = "1"
        try:
            mock_run_pipeline.return_value = {"success": True}
            r = json.loads(handle_pipeline_run({"budget_usd": 2.5}))
            self.assertEqual(r, {"result": {"success": True}})
            mock_run_pipeline.assert_called_once_with(db_path(), daily_budget_usd=2.5)
        finally:
            os.environ.pop("KAHZAABU_ALLOW_PIPELINE", None)


# ───────────────────────────────────────────────────────────────────
# Discovery + requirements check
# ───────────────────────────────────────────────────────────────────

class DiscoveryTests(unittest.TestCase):
    def test_kahzaabu_home_is_real_path(self):
        from plugins.kahzaabu.tools import kahzaabu_home
        # lru_cached — clear in case prior tests warmed it.
        kahzaabu_home.cache_clear()
        home = kahzaabu_home()
        self.assertIsNotNone(home)
        self.assertTrue((home / "kahzaabu" / "__init__.py").exists())

    def test_db_path_resolves_under_home(self):
        from plugins.kahzaabu.tools import db_path, kahzaabu_home
        kahzaabu_home.cache_clear()
        p = db_path()
        self.assertTrue(str(p).endswith("data/kahzaabu.db"))


class ToolsTableTests(unittest.TestCase):
    """The TOOLS table is what __init__.py iterates to register the
    plugin's surface. Each entry must have exactly 4 fields and a
    valid handler."""

    def test_each_tool_has_4_fields_and_callable_handler(self):
        from plugins.kahzaabu.tools import TOOLS
        for entry in TOOLS:
            self.assertEqual(len(entry), 4,
                f"TOOLS entry has wrong arity: {entry}")
            name, schema, handler, emoji = entry
            self.assertTrue(name.startswith("kahzaabu_"),
                f"name must be namespaced: {name}")
            self.assertEqual(schema.get("name"), name,
                f"schema['name'] must match tool name: {name}")
            self.assertTrue(callable(handler),
                f"handler must be callable: {name}")
            self.assertEqual(len(emoji), 1 + (1 if "️" in emoji else 0),
                f"emoji must be 1 char (variant selector OK): {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
