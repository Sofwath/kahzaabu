# SPDX-License-Identifier: Apache-2.0
"""V2 Slice 12 tests — reproducibility, audit, transparency, metrics.

Each test is offline. The audit + transparency tests spin up an
in-memory SQLite with a tiny seeded corpus so we don't depend on
the live DB. Reproducibility tests use the same fixture.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import audit, claims_db, db as db_module, reproducibility, transparency


# ───────────────────────────────────────────────────────────────────
# Fixture: tiny seeded DB matching the V2 schema
# ───────────────────────────────────────────────────────────────────

def _seed_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    db_module.init_db(conn)        # articles + scrape_runs + extraction_runs
    claims_db.init_claims_schema(conn)  # V2 enrichment tables + migrations
    # Seed a couple of articles, claims, runs, fact_checks.
    conn.execute(
        "INSERT INTO articles (id, language, title, body_text, published_date,"
        " category, category_id, reference, scraped_at) "
        "VALUES (1001, 'EN', 'Article A', 'body', '2025-01-01', "
        "'press_release', 1, 'https://presidency.gov.mv/news/1', "
        "'2025-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO articles (id, language, title, body_text, published_date,"
        " category, category_id, reference, scraped_at) "
        "VALUES (1002, 'EN', 'Article B', 'body', '2025-06-01', "
        "'press_release', 1, 'https://presidency.gov.mv/news/2', "
        "'2025-06-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO extraction_runs (started_at, status) "
        "VALUES ('2025-01-02T00:00:00Z', 'completed')"
    )
    ext_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO claims (article_id, language, type, polarity, "
        "                    is_checkable, quote, extraction_run_id, "
        "                    created_at) "
        "VALUES (1001, 'EN', 'numeric_promise', 'PROMISE', 1, "
        "        'build 40,000 housing units', ?, "
        "        '2025-01-02T00:00:00Z')",
        (ext_id,)
    )
    conn.execute(
        "INSERT INTO curation_runs (started_at, finished_at, "
        "                            tokens_in, tokens_out, cost_usd, "
        "                            status) "
        "VALUES ('2025-02-01T00:00:00Z', '2025-02-01T00:01:00Z', "
        "        1500, 600, 0.034, 'completed')"
    )
    cur_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Two fact_checks across 2024 and 2025 to give the audit something to test.
    for fc in [
        ("LIE",         "2024-03-15", "infrastructure"),
        ("MISLEADING",  "2025-04-10", "infrastructure"),
        ("LIE",         "2025-09-22", "economy"),
        ("BROKEN_DEADLINE", "2025-11-05", "infrastructure"),
    ]:
        conn.execute(
            "INSERT INTO fact_checks (category, claim, claim_date, "
            "                          topic, confidence, source_article_ids,"
            "                          created_at, published, "
            "                          curation_run_id, verdict_label, "
            "                          truth_score, truth_score_label, "
            "                          speaker) "
            "VALUES (?, 'test claim', ?, ?, 'reviewed', '[1001]', ?, 1, "
            "        ?, 'REFUTED', 2, 'FALSE', 'Mohamed Muizzu')",
            (fc[0], fc[1], fc[2], fc[1] + "T00:00:00Z", cur_id)
        )
    # Verifier evidence — one authoritative URL, one not.
    fc_id = conn.execute(
        "SELECT id FROM fact_checks LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO verification_runs (started_at, status, cost_usd) "
        "VALUES ('2025-02-02T00:00:00Z', 'completed', 0.03)"
    )
    vr_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    claims_db.insert_evidence(
        conn, fc_id,
        url="https://presidency.gov.mv/announce/x",
        relevance="confirms", verification_run_id=vr_id,
    )
    claims_db.insert_evidence(
        conn, fc_id,
        url="https://example.com/blog",
        relevance="context", verification_run_id=vr_id,
    )
    conn.commit()
    return conn


# ───────────────────────────────────────────────────────────────────
# Reproducibility manifest
# ───────────────────────────────────────────────────────────────────

class ReproducibilityTests(unittest.TestCase):
    def setUp(self):
        self.conn = _seed_db()
        self.fc_id = self.conn.execute(
            "SELECT id FROM fact_checks LIMIT 1").fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_returns_none_for_missing(self):
        self.assertIsNone(reproducibility.get_manifest(self.conn, 999999))

    def test_returns_manifest_for_existing(self):
        m = reproducibility.get_manifest(self.conn, self.fc_id)
        self.assertIsNotNone(m)
        self.assertEqual(m["fact_check_id"], self.fc_id)
        self.assertIn("verdict_label", m)
        self.assertIn("supporting_claims", m)
        self.assertIn("verification_evidence", m)
        self.assertIn("_schema_version", m)

    def test_produced_by_joins_curation_run(self):
        m = reproducibility.get_manifest(self.conn, self.fc_id)
        self.assertIsNotNone(m["produced_by"])
        self.assertIn("curation_run_id", m["produced_by"])
        self.assertEqual(m["produced_by"]["cost_usd"], 0.034)

    def test_evidence_carries_authoritative_tag(self):
        m = reproducibility.get_manifest(self.conn, self.fc_id)
        ev = m["verification_evidence"]
        self.assertEqual(len(ev), 2)
        auth = [e for e in ev if e["authoritative_entity_id"]]
        self.assertEqual(len(auth), 1)
        self.assertEqual(auth[0]["authoritative_entity_id"], "presidency")

    def test_json_serialisable(self):
        body = reproducibility.get_manifest_json(self.conn, self.fc_id)
        parsed = json.loads(body)
        self.assertEqual(parsed["fact_check_id"], self.fc_id)

    def test_current_git_sha_returns_str_or_none(self):
        sha = reproducibility.current_git_sha()
        # In CI fresh-clones or a non-git tarball this can be None;
        # accept either but reject anything else.
        self.assertTrue(sha is None or isinstance(sha, str))
        if sha:
            self.assertGreaterEqual(len(sha), 7)  # short or full SHA


# ───────────────────────────────────────────────────────────────────
# Audit (chi-squared + markdown)
# ───────────────────────────────────────────────────────────────────

class ChiSquaredTests(unittest.TestCase):
    def test_independent_distribution_p_value_high(self):
        # Perfectly proportional — chi² should be 0, p should be 1.
        data = {
            "A": {"x": 10, "y": 10},
            "B": {"x": 10, "y": 10},
        }
        stat, df = audit.chi_squared_stat(data)
        self.assertAlmostEqual(stat, 0.0, places=6)
        self.assertEqual(df, 1)
        p = audit.chi_squared_p_value(stat, df)
        self.assertAlmostEqual(p, 1.0, places=3)

    def test_strongly_dependent_distribution_p_value_low(self):
        data = {
            "A": {"x": 100, "y": 0},
            "B": {"x": 0,   "y": 100},
        }
        stat, df = audit.chi_squared_stat(data)
        self.assertGreater(stat, 10.0)
        p = audit.chi_squared_p_value(stat, df)
        self.assertLess(p, 0.01)

    def test_known_value_2x2(self):
        # Known reference: chi² with df=1 at p=0.05 → critical value 3.841.
        # Our Wilson-Hilferty approximator should land near that.
        crit = audit._chi2_critical_005(1)
        self.assertAlmostEqual(crit, 3.841, delta=0.2)

    def test_empty_table(self):
        stat, df = audit.chi_squared_stat({})
        self.assertEqual((stat, df), (0.0, 0))


class AuditReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = _seed_db()

    def tearDown(self):
        self.conn.close()

    def test_category_by_year_returns_dict(self):
        d = audit.category_by_year(self.conn)
        self.assertIn("LIE", d)
        self.assertIn("2025", d["LIE"])

    def test_speaker_distribution_single_subject(self):
        sd = audit.speaker_distribution(self.conn)
        self.assertEqual(len(sd), 1)
        self.assertEqual(sd[0][0], "Mohamed Muizzu")
        self.assertEqual(sd[0][1], 4)

    def test_authoritative_source_coverage(self):
        cov = audit.authoritative_source_coverage(self.conn)
        self.assertEqual(cov["total_evidence_rows"], 2)
        self.assertEqual(cov["authoritative_rows"], 1)
        self.assertAlmostEqual(cov["primary_source_rate"], 0.5)
        self.assertEqual(cov["by_entity"]["presidency"], 1)

    def test_render_includes_key_sections(self):
        md = audit.render_audit_report(self.conn)
        for header in (
            "# Kahzaabu — bias / fairness audit",
            "## Category distribution by year",
            "## Category distribution by topic",
            "## AVeriTeC verdict-label distribution",
            "## Truth-O-Meter ladder distribution",
            "## Speaker concentration",
            "## Authoritative external-source coverage",
            "chi-squared:",
        ):
            self.assertIn(header, md, f"missing: {header}")


# ───────────────────────────────────────────────────────────────────
# Transparency report
# ───────────────────────────────────────────────────────────────────

class TransparencyReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = _seed_db()

    def tearDown(self):
        self.conn.close()

    def test_window_filtering(self):
        # All 4 seeded fact_checks were created in 2024-2025.
        md = transparency.render_report(
            self.conn, since="2025-01-01", until="2025-12-31")
        self.assertIn("# Kahzaabu — transparency report", md)
        # 3 fact-checks in 2025
        self.assertIn("Total: **3**", md)

    def test_zero_window(self):
        md = transparency.render_report(
            self.conn, since="2030-01-01", until="2030-12-31")
        self.assertIn("Total: **0**", md)
        self.assertIn("No published fact-checks", md)

    def test_llm_spend_in_window(self):
        spend = transparency._llm_spend_in_window(
            self.conn, since="2025-01-01", until="2025-12-31")
        # curation_runs has cost_usd=0.034; verification_runs has 0.03
        total = sum(spend.values())
        self.assertAlmostEqual(total, 0.064, places=3)


# ───────────────────────────────────────────────────────────────────
# Prometheus metrics — verify the module imports cleanly + helpers
# don't crash + /metrics endpoint payload is well-formed.
# ───────────────────────────────────────────────────────────────────

class MetricsTests(unittest.TestCase):
    def test_module_imports(self):
        from kahzaabu.web import metrics
        self.assertTrue(metrics.prometheus_available())

    def test_helpers_dont_crash(self):
        from kahzaabu.web import metrics
        metrics.record_api_request(
            path="/test", method="GET", status=200, duration_s=0.1)
        metrics.record_pipeline_run(
            stage="test", status="ok", duration_s=1.5)
        metrics.record_llm_call(
            stage="test", model="haiku-test",
            tokens_in=100, tokens_out=50, cost_usd=0.001)
        metrics.record_fact_check_published(
            category="LIE", verdict_label="REFUTED")

    def test_payload_is_text_prometheus(self):
        from kahzaabu.web import metrics
        body, ctype = metrics.render_metrics_payload()
        self.assertIn("text/plain", ctype)
        # Should include at least one of our defined metric names
        body_str = body.decode("utf-8")
        self.assertIn("kahzaabu_", body_str)


# ───────────────────────────────────────────────────────────────────
# Schema migration check
# ───────────────────────────────────────────────────────────────────

class GitShaColumnTests(unittest.TestCase):
    def test_column_added(self):
        conn = _seed_db()
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(fact_checks)").fetchall()]
        self.assertIn("git_sha_at_publication", cols)

    def test_stamp_git_sha_idempotent(self):
        conn = _seed_db()
        fc_id = conn.execute(
            "SELECT id FROM fact_checks LIMIT 1").fetchone()[0]
        with patch.object(reproducibility, "current_git_sha",
                            return_value="deadbeef"):
            r1 = reproducibility.stamp_git_sha(conn, fc_id)
            r2 = reproducibility.stamp_git_sha(conn, fc_id)
        self.assertEqual(r1, "deadbeef")
        self.assertEqual(r2, "deadbeef")  # second call returns existing


if __name__ == "__main__":
    unittest.main(verbosity=2)
