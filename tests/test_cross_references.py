# SPDX-License-Identifier: Apache-2.0
"""Tests for the Slice-13 cross-reference wiring.

Three integration points are exercised:

  1. /api/reproducibility/{id}.json now includes resolved
     `authoritative_entity` per evidence row + a deduped
     top-level `authoritative_entities` list.
  2. /api/constitution/{n}/citing-factchecks reverse-lookup
     returns fact-checks whose claim/topic match a seed
     extracted from the constitution article's title.
  3. The factcheck.html static page references both new
     fields so an end-user sees the "Verified against
     authoritative sources" section instead of just a tooltip.

These tests were added 2026-05-22 after an end-user flagged
that the UI wasn't surfacing the constitution + authoritative-
entity infrastructure even though the data was already in the
DB. The bug surface here was "data exists, API doesn't return
it, UI can't display it" — covering all three layers.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from kahzaabu.web.app import app
from kahzaabu.web.db_dep import DEFAULT_DB


def _has_tagged_factcheck():
    """Return a fact_check_id that has at least one evidence row
    with authoritative_entity_id set, or None."""
    if not DEFAULT_DB.exists():
        return None
    try:
        with sqlite3.connect(DEFAULT_DB) as conn:
            r = conn.execute(
                "SELECT fact_check_id FROM fact_check_evidence "
                "WHERE authoritative_entity_id IS NOT NULL "
                "LIMIT 1"
            ).fetchone()
            return r[0] if r else None
    except sqlite3.OperationalError:
        return None


TAGGED_FC = _has_tagged_factcheck()


def _clear_overrides():
    """Defensive: clear any leftover dependency overrides from prior
    tests in the suite. test_contradictions_api used to install a
    get_db override without tearing it down (now fixed), so without
    this guard we'd hit a stale in-memory DB that doesn't have any
    of the rows we're testing against."""
    app.dependency_overrides.clear()


@unittest.skipUnless(TAGGED_FC, "no authoritative-tagged fact-checks in DB")
class AuthoritativeEntityWiring(unittest.TestCase):
    """The reproducibility manifest must resolve entity IDs against
    the public-sector registry (ADR 0011), not just return the raw
    ID string. Without resolution, the UI can't render the entity
    name/domain/type."""

    @classmethod
    def setUpClass(cls):
        _clear_overrides()
        cls.c = TestClient(app)
        cls.resp = cls.c.get(f"/api/reproducibility/{TAGGED_FC}.json").json()

    def test_top_level_authoritative_entities_present(self):
        self.assertIn("authoritative_entities", self.resp,
            "manifest must include a top-level deduped entity list "
            "so the UI can render 'Verified against X, Y, Z' "
            "without re-grouping client-side")
        self.assertIsInstance(self.resp["authoritative_entities"], list)
        self.assertGreater(len(self.resp["authoritative_entities"]), 0,
            f"fact-check {TAGGED_FC} was selected because it has "
            "tagged evidence; the resolved list must not be empty")

    def test_entity_resolved_to_registry_fields(self):
        entity = self.resp["authoritative_entities"][0]
        # All four registry fields must be resolved — otherwise
        # the UI card has nothing to render.
        for key in ("id", "name", "domain", "type"):
            self.assertIn(key, entity,
                f"resolved entity missing '{key}' — UI card "
                "needs all four fields to render")
            self.assertIsInstance(entity[key], str)
            self.assertGreater(len(entity[key]), 0)

    def test_per_evidence_row_also_resolved(self):
        """Each evidence row that has an authoritative_entity_id
        must also carry a resolved `authoritative_entity` dict,
        so the per-row badge can show the entity name."""
        ev = self.resp["verification_evidence"]
        tagged_rows = [r for r in ev if r.get("authoritative_entity_id")]
        self.assertGreater(len(tagged_rows), 0,
            "test fixture invariant: fact-check was selected for "
            "having tagged evidence")
        for row in tagged_rows:
            self.assertIsNotNone(row.get("authoritative_entity"),
                "row has authoritative_entity_id but resolved "
                "entity is None — registry lookup failed")


@unittest.skipUnless(DEFAULT_DB.exists(),
                     "no live DB; can't reverse-lookup")
class CitingFactchecksReverseLookup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _clear_overrides()
        cls.c = TestClient(app)

    def test_returns_404_for_missing_article(self):
        r = self.c.get("/api/constitution/9999/citing-factchecks")
        self.assertEqual(r.status_code, 404)

    def test_returns_shape_for_valid_article(self):
        r = self.c.get("/api/constitution/1/citing-factchecks?limit=5")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("article_no", body)
        self.assertIn("items", body)
        self.assertEqual(body["article_no"], 1)
        self.assertIsInstance(body["items"], list)
        # Items (if any) have the expected per-row keys.
        for fc in body["items"]:
            for key in ("id", "category", "claim", "verdict_label"):
                self.assertIn(key, fc,
                    f"citing-factcheck item missing '{key}' — "
                    "the UI list relies on these to render")

    def test_limit_is_honoured(self):
        r = self.c.get("/api/constitution/26/citing-factchecks?limit=2")
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(r.json()["items"]), 2)


class StaticPageReferences(unittest.TestCase):
    """Pin that the static HTML pages actually consume the new fields.
    If a future refactor renames `authoritative_entities` in the API
    but the static page keeps referencing the old name, this catches
    that quickly without needing a full headless-DOM run."""

    def test_factcheck_page_renders_auth_entities_section(self):
        page = (ROOT / "kahzaabu" / "web" / "static" / "factcheck.html").read_text()
        self.assertIn("authoritative_entities", page,
            "factcheck.html must reference the API's "
            "`authoritative_entities` field to render the new "
            "'Verified against authoritative sources' section")
        self.assertIn("auth-entity-card", page,
            "factcheck.html must use the .auth-entity-card class "
            "(styled in the page's <style> block) — otherwise the "
            "section renders unstyled")

    def test_article_page_does_constitution_crossref(self):
        page = (ROOT / "kahzaabu" / "web" / "static" / "article.html").read_text()
        self.assertIn("/api/constitution/search", page,
            "article.html must call /api/constitution/search to "
            "surface constitutional context — without this call "
            "the page has no cross-reference at all")

    def test_constitution_page_does_reverse_lookup(self):
        page = (ROOT / "kahzaabu" / "web" / "static" / "constitution.html").read_text()
        self.assertIn("citing-factchecks", page,
            "constitution.html must call the citing-factchecks "
            "endpoint to surface reverse cross-references")
        self.assertIn("citing-fcs-head", page,
            "constitution.html's citing-factchecks list needs the "
            "head/list CSS classes to render correctly")


if __name__ == "__main__":
    unittest.main()
