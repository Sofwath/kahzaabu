# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the public-sector entity registry (ADR 0011).

Covers:
  - registry loader: shape validation, duplicate detection,
    entity_type enum enforcement
  - URL → entity match: exact hostname, subdomain, www-prefix,
    case-insensitive, scheme-less
  - non-match: similar but distinct domains
  - YAML ↔ JSON parity: the human-editable YAML and the
    machine-loaded JSON must declare the same entities
  - schema migration: fact_check_evidence.authoritative_entity_id
    column exists and is populated by insert_evidence

All offline, no LLM, no network.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kahzaabu import registry


class TestRegistryShape(unittest.TestCase):
    def test_loads_default_registry(self):
        registry.load_registry.cache_clear()
        data = registry.load_registry()
        self.assertIn("entities", data)
        self.assertGreater(len(data["entities"]), 20)

    def test_every_entity_has_required_fields(self):
        registry.load_registry.cache_clear()
        for ent in registry.load_registry()["entities"]:
            self.assertIn("entity_id", ent)
            self.assertIn("official_name", ent)
            self.assertIn("entity_type", ent)
            # domain is optional (HPA / MFDA have null)

    def test_entity_ids_unique(self):
        registry.load_registry.cache_clear()
        ids = [e["entity_id"]
               for e in registry.load_registry()["entities"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_domains_unique_when_present(self):
        registry.load_registry.cache_clear()
        domains = [e["domain"]
                   for e in registry.load_registry()["entities"]
                   if e.get("domain")]
        self.assertEqual(len(domains), len(set(domains)))

    def test_entity_types_in_taxonomy(self):
        registry.load_registry.cache_clear()
        for ent in registry.load_registry()["entities"]:
            self.assertIn(ent["entity_type"], registry.ENTITY_TYPES)

    def test_load_rejects_bad_entity_type(self):
        registry.load_registry.cache_clear()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text(json.dumps({"entities": [{
                "entity_id": "x", "official_name": "X",
                "entity_type": "WHATEVER_NOT_A_REAL_TYPE",
            }]}))
            with self.assertRaises(ValueError):
                registry.load_registry(p)

    def test_load_rejects_duplicate_entity_id(self):
        registry.load_registry.cache_clear()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dup.json"
            p.write_text(json.dumps({"entities": [
                {"entity_id": "x", "official_name": "A",
                 "entity_type": "ministry"},
                {"entity_id": "x", "official_name": "B",
                 "entity_type": "ministry"},
            ]}))
            with self.assertRaises(ValueError):
                registry.load_registry(p)


class TestUrlMatching(unittest.TestCase):
    def setUp(self):
        registry.load_registry.cache_clear()

    def test_exact_domain(self):
        ent = registry.entity_for_url("https://presidency.gov.mv/news/123")
        self.assertIsNotNone(ent)
        self.assertEqual(ent["entity_id"], "presidency")

    def test_subdomain_match(self):
        ent = registry.entity_for_url("https://news.presidency.gov.mv/x")
        self.assertEqual(ent["entity_id"], "presidency")

    def test_www_prefix_stripped(self):
        ent = registry.entity_for_url("https://www.foreign.gov.mv/")
        self.assertEqual(ent["entity_id"], "foreign")

    def test_case_insensitive(self):
        ent = registry.entity_for_url("HTTPS://WWW.MIRA.GOV.MV/")
        self.assertEqual(ent["entity_id"], "mira")

    def test_bare_hostname_no_scheme(self):
        ent = registry.entity_for_url("presidency.gov.mv/news/x")
        self.assertEqual(ent["entity_id"], "presidency")

    def test_similar_but_distinct_domain_no_match(self):
        """presidency-fake.gov.mv must NOT match presidency.gov.mv."""
        self.assertIsNone(
            registry.entity_for_url("https://presidency-fake.gov.mv/")
        )

    def test_non_registered_domain_no_match(self):
        self.assertIsNone(
            registry.entity_for_url("https://example.com/article"))
        self.assertIsNone(
            registry.entity_for_url("https://twitter.com/x"))

    def test_empty_or_invalid_url(self):
        self.assertIsNone(registry.entity_for_url(""))
        self.assertIsNone(registry.entity_for_url(None))  # type: ignore[arg-type]

    def test_is_authoritative_boolean(self):
        self.assertTrue(
            registry.is_authoritative("https://stelco.com.mv/announce"))
        self.assertFalse(
            registry.is_authoritative("https://blog.example.com"))


class TestRegistryParity(unittest.TestCase):
    """The YAML (source of truth for contributors) and JSON (machine-
    loaded twin) must declare the same entities. If a contributor edits
    one without the other, this test surfaces the drift.

    Parser is intentionally small — the YAML we ship is flow-style only,
    one entity per line. If someone introduces block-style or aliases,
    this test will fail and the JSON regeneration path needs revisiting
    (likely time to bring pyyaml in).
    """

    YAML_PATH = ROOT / "data" / "registry" / "maldives_public_sector.yaml"
    JSON_PATH = ROOT / "data" / "registry" / "maldives_public_sector.json"

    @staticmethod
    def _parse_minimal_yaml(text: str) -> list[dict]:
        """Parse the supplied flow-style YAML without pyyaml. Recognises
        `{key: value, key: "value", key: null}` per line. Fails if the
        file uses block-style — by design."""
        out: list[dict] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("- {") or not line.endswith("}"):
                continue
            body = line[len("- {"):-1]
            ent: dict = {}
            # Split on commas not inside quotes.
            parts = re.findall(
                r'(\w+):\s*("(?:[^"\\]|\\.)*"|[^,}]+)',
                body,
            )
            for k, v in parts:
                v = v.strip()
                if v.startswith('"') and v.endswith('"'):
                    v = v[1:-1]
                elif v == "null":
                    v = None
                ent[k] = v
            out.append(ent)
        return out

    def test_yaml_and_json_in_sync(self):
        self.assertTrue(self.YAML_PATH.exists(),
                         f"missing canonical YAML: {self.YAML_PATH}")
        self.assertTrue(self.JSON_PATH.exists(),
                         f"missing JSON twin: {self.JSON_PATH}")
        yaml_entities = self._parse_minimal_yaml(self.YAML_PATH.read_text())
        json_data = json.loads(self.JSON_PATH.read_text())
        json_entities = json_data["entities"]

        # Compare on a per-entity_id basis.
        yaml_by_id = {e["entity_id"]: e for e in yaml_entities}
        json_by_id = {e["entity_id"]: e for e in json_entities}
        self.assertEqual(set(yaml_by_id), set(json_by_id),
                          "entity_id sets diverge between YAML and JSON")
        for eid in yaml_by_id:
            y = yaml_by_id[eid]
            j = json_by_id[eid]
            self.assertEqual(y.get("official_name"), j.get("official_name"),
                              f"{eid}: official_name divergence")
            self.assertEqual(y.get("domain"), j.get("domain"),
                              f"{eid}: domain divergence")
            self.assertEqual(y.get("entity_type"), j.get("entity_type"),
                              f"{eid}: entity_type divergence")


class TestSchemaMigration(unittest.TestCase):
    """The Slice 11.5 ALTER must run cleanly on a fresh DB and on a
    pre-existing one. insert_evidence must populate the new column."""

    def setUp(self):
        from kahzaabu import claims_db
        self.claims_db = claims_db
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys=ON")
        claims_db.init_claims_schema(self.conn)
        # Need a fact_check to attach evidence to.
        self.conn.execute(
            "INSERT INTO fact_checks "
            "(category, claim, claim_date, topic, source_article_ids, "
            " created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("LIE", "test claim", "2024-01-01", "test", "[]",
             "2026-05-21T00:00:00Z"),
        )
        self.fc_id = self.conn.execute(
            "SELECT id FROM fact_checks LIMIT 1").fetchone()[0]

    def tearDown(self):
        self.conn.close()

    def test_column_exists(self):
        cols = [r[1] for r in self.conn.execute(
            "PRAGMA table_info(fact_check_evidence)").fetchall()]
        self.assertIn("authoritative_entity_id", cols)

    def test_authoritative_url_auto_tagged(self):
        registry.load_registry.cache_clear()
        eid = self.claims_db.insert_evidence(
            self.conn, self.fc_id,
            url="https://presidency.gov.mv/news/x",
            relevance="confirms",
        )
        row = self.conn.execute(
            "SELECT authoritative_entity_id FROM fact_check_evidence "
            "WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(row[0], "presidency")

    def test_non_registered_url_left_null(self):
        eid = self.claims_db.insert_evidence(
            self.conn, self.fc_id,
            url="https://example.com/blog",
            relevance="context",
        )
        row = self.conn.execute(
            "SELECT authoritative_entity_id FROM fact_check_evidence "
            "WHERE id = ?", (eid,)).fetchone()
        self.assertIsNone(row[0])

    def test_no_url_left_null(self):
        eid = self.claims_db.insert_evidence(
            self.conn, self.fc_id,
            url=None,
            relevance="no_relevant_info",
        )
        row = self.conn.execute(
            "SELECT authoritative_entity_id FROM fact_check_evidence "
            "WHERE id = ?", (eid,)).fetchone()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
