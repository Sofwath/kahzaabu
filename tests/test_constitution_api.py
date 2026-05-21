# SPDX-License-Identifier: Apache-2.0
"""Tests for the constitution web API (V2 UI polish slice).

Spins up FastAPI via TestClient against the live SQLite DB (which
already has the 301 articles imported). If the DB doesn't have the
articles, every test is skipped with a clear reason.
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


def _has_articles() -> bool:
    if not DEFAULT_DB.exists():
        return False
    try:
        with sqlite3.connect(DEFAULT_DB) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM constitution_articles").fetchone()[0]
            return n > 0
    except sqlite3.OperationalError:
        return False


@unittest.skipUnless(_has_articles(),
                      "constitution_articles not imported in live DB")
class ConstitutionAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_list_articles_default(self):
        r = self.c.get("/api/constitution/articles?limit=10")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["limit"], 10)
        self.assertGreater(body["total"], 200)  # corpus has 301
        self.assertEqual(len(body["items"]), 10)
        for item in body["items"]:
            self.assertIn("article_no", item)
            self.assertIn("title", item)
            self.assertIn("body", item)

    def test_list_articles_paged(self):
        r1 = self.c.get("/api/constitution/articles?limit=5&offset=0")
        r2 = self.c.get("/api/constitution/articles?limit=5&offset=5")
        ids1 = [i["article_no"] for i in r1.json()["items"]]
        ids2 = [i["article_no"] for i in r2.json()["items"]]
        self.assertEqual(set(ids1) & set(ids2), set())

    def test_get_single_article(self):
        r = self.c.get("/api/constitution/1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["article_no"], 1)
        self.assertIn("title", body)
        self.assertIn("body", body)

    def test_get_missing_article(self):
        r = self.c.get("/api/constitution/999")
        self.assertEqual(r.status_code, 404)

    def test_search_returns_hits(self):
        r = self.c.get("/api/constitution/search?q=religion&limit=3")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["query"], "religion")
        self.assertIsInstance(body["items"], list)
        # The constitution mentions religion in at least the
        # state-religion article — expect non-empty hits.
        self.assertGreater(len(body["items"]), 0)

    def test_search_empty_query_rejected(self):
        r = self.c.get("/api/constitution/search?q=")
        self.assertEqual(r.status_code, 422)  # min_length=1 → validation error


class ConstitutionPageRouteTests(unittest.TestCase):
    """Static page routes resolve even on a fresh DB."""
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_constitution_page_route(self):
        r = self.c.get("/constitution")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Constitution of the Republic of Maldives",
                       r.text)

    def test_factcheck_detail_page_route(self):
        # /factcheck/{id} serves the static page regardless of whether
        # the id exists — the page itself fetches the manifest on load.
        r = self.c.get("/factcheck/1")
        self.assertEqual(r.status_code, 200)
        # The Truth-O-Meter ladder labels MUST be in the page; JS uses
        # them to render the active rung.
        self.assertIn("MOSTLY_TRUE", r.text)
        self.assertIn("PANTS_ON_FIRE", r.text)
        # Page links to the reproducibility manifest endpoint.
        self.assertIn("/api/reproducibility/", r.text)


class StaticPageJSShadowingTests(unittest.TestCase):
    """Regression guard for the const-shadow-of-api-export bug.

    When an inline <script> declares a top-level `const FOO = ...`
    that matches a global function-declaration in /static/js/api.js,
    classic-script lexical binding rules throw
        SyntaxError: Identifier 'FOO' has already been declared
    on parse, which silently aborts the whole inline block — every
    `fetchJSON(...)` call inside it never runs. The page loads, but
    no data ever appears.

    Caught and fixed in factcheck.html + constitution.html where
    `const el = ...` collided with api.js's `function el(){}`. This
    test pins the invariant going forward: no static page may
    redeclare any of the api.js exports as `const` or `let`.

    Function declarations (`function el(){...}`) are still allowed —
    they silently override in classic scripts without throwing.
    """

    API_EXPORTS = ("fetchJSON", "el", "escapeHtml", "catClass",
                    "catBadgeClass", "fmtDate", "qs", "setNavActive")

    def test_no_static_page_const_shadows_api_export(self):
        import re
        from pathlib import Path
        static = (Path(__file__).resolve().parents[1]
                  / "kahzaabu" / "web" / "static")
        offenders = []
        for page in sorted(static.glob("*.html")):
            text = page.read_text()
            for name in self.API_EXPORTS:
                # Top-of-line const/let with this name. Anchored to
                # start-of-line so nested declarations aren't flagged.
                pat = re.compile(
                    rf"^\s*(const|let)\s+{name}\s*=", re.MULTILINE)
                if pat.search(text):
                    offenders.append(f"{page.name}: const/let {name}")
        self.assertEqual(offenders, [],
            "Inline <script> in a static page redeclares an api.js "
            "export with const/let — this throws SyntaxError on parse "
            "in classic scripts and aborts data loading. Use the "
            "global from api.js or rename your local. Offenders:\n  "
            + "\n  ".join(offenders))


class LawsPageTests(unittest.TestCase):
    """ADR 0012 — /laws is a static link-out page. It MUST NOT make
    any backend HTTP requests to mvlaw.gov.mv. The page is purely
    deep-links + a client-side Google site-search redirect.
    """
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_laws_page_route(self):
        r = self.c.get("/laws")
        self.assertEqual(r.status_code, 200)

    def test_laws_page_lists_all_five_canonical_sections(self):
        """The 5 canonical mvlaw.gov.mv sections must be enumerated
        in the SECTIONS JS const so the renderer can pick them up."""
        r = self.c.get("/laws")
        body = r.text
        for path in ("/constitution.php", "/ganoon_main.php",
                      "/cancelganoon.php", "/gavaid_main.php",
                      "/publications.php"):
            self.assertIn(path, body,
                           f"section path missing from SECTIONS: {path}")
        # And the canonical host constant must be present so the
        # paths actually resolve to URLs at render time.
        self.assertIn('CANONICAL_HOST = "old.mvlaw.gov.mv"', body)

    def test_laws_page_links_open_new_tab_with_safe_rel(self):
        """target=_blank links must carry rel='noopener noreferrer'
        — otherwise the new tab can hijack the opener (CWE-1022).
        Tiles are rendered client-side from SECTIONS, but the tile
        renderer hard-codes rel='noopener noreferrer' and the static
        anchors on the page (intro + footer attribution) must do the
        same."""
        r = self.c.get("/laws")
        import re
        # Static target=_blank anchors in the HTML source.
        opens = re.findall(
            r'<a [^>]*target="_blank"[^>]*>', r.text)
        self.assertGreaterEqual(len(opens), 2,
            "expected at least the intro + robots.txt static links")
        for tag in opens:
            self.assertIn("noopener", tag,
                           f"missing noopener: {tag[:120]}")
            self.assertIn("noreferrer", tag,
                           f"missing noreferrer: {tag[:120]}")
        # The tile renderer must assign rel='noopener noreferrer'.
        self.assertIn('tile.rel = "noopener noreferrer"', r.text)

    def test_search_engines_default_to_duckduckgo_with_google_opt_in(self):
        """Concern: Google logs the search query. Default the
        search-engine selector to DuckDuckGo and require the user
        to opt in to Google explicitly."""
        r = self.c.get("/laws")
        body = r.text
        # SEARCH_ENGINES has both options + DDG is the radio default.
        self.assertIn("SEARCH_ENGINES", body)
        self.assertIn("duckduckgo.com", body)
        self.assertIn("www.google.com/search", body)
        # The DDG radio must be `checked` and Google must NOT be.
        import re
        ddg_radio = re.search(
            r'<input[^>]*name="engine"[^>]*value="ddg"[^>]*>', body)
        self.assertIsNotNone(ddg_radio, "ddg radio missing")
        self.assertIn("checked", ddg_radio.group(0),
                       "ddg radio is not the default")
        google_radio = re.search(
            r'<input[^>]*name="engine"[^>]*value="google"[^>]*>', body)
        self.assertIsNotNone(google_radio, "google radio missing")
        self.assertNotIn("checked", google_radio.group(0),
                          "google radio must NOT be the default")

    def test_ddg_url_does_not_override_safesearch(self):
        """Concern: the earlier `kp=-2` DDG parameter was undocumented
        and could break silently. We removed it; the user's own
        safesearch setting wins."""
        r = self.c.get("/laws")
        body = r.text
        import re
        # Find the DDG URL builder line.
        m = re.search(r'ddg:\s*q\s*=>\s*`([^`]+)`', body)
        self.assertIsNotNone(m, "DDG URL builder not found")
        ddg_template = m.group(1)
        self.assertNotIn("kp=", ddg_template,
                          "kp= parameter must not be set — user's "
                          "DDG safesearch preference is theirs to control")

    def test_search_query_privacy_disclaimer_visible(self):
        """Concern: even the DDG default still routes the typed query
        through a third party. The page must surface this clearly so
        users can choose to click tiles directly for full privacy."""
        r = self.c.get("/laws")
        body = r.text.lower()
        # Some plain-language privacy disclosure must be visible above
        # the search box. We check for two phrases so a future
        # copy-edit doesn't silently lose the disclosure.
        self.assertIn("privacy note", body,
                       "missing 'Privacy note:' label on the search box")
        self.assertTrue(
            "100% private" in body or "fully private" in body,
            "missing the 'click tiles directly for full privacy' callout")

    def test_url_registry_centralised(self):
        """Concern: URL drift across the page. SECTIONS is the single
        source of truth; hard-coded https://old.mvlaw.gov.mv paths
        elsewhere in the file would re-introduce drift."""
        from pathlib import Path
        page = (Path(__file__).resolve().parents[1]
                / "kahzaabu" / "web" / "static" / "laws.html").read_text()
        import re
        # Allowed hardcoded mvlaw URLs (outside SECTIONS):
        #   - the homepage in the intro paragraph (https://old.mvlaw.gov.mv/)
        #   - the robots.txt link in the explainer
        #   - SECTIONS array entries themselves
        # We assert that section-specific .php paths only appear inside
        # the SECTIONS array, not scattered across the HTML.
        section_paths = ("/constitution.php", "/ganoon_main.php",
                          "/cancelganoon.php", "/gavaid_main.php",
                          "/publications.php")
        for path in section_paths:
            # `path` should appear ONCE — inside the SECTIONS data.
            count = page.count(path)
            self.assertEqual(
                count, 1,
                f"path {path} appears {count}× — should be in SECTIONS only")

    def test_laws_page_carries_adr_attribution(self):
        """The link-out rationale must be visible to users —
        otherwise they'll assume we just forgot to import the laws."""
        r = self.c.get("/laws")
        body = r.text.lower()
        # Either ADR 0012 or the EU directive reference must be on
        # the page so users understand WHY we link out.
        self.assertTrue(
            "adr 0012" in body or "directive 2019/790" in body,
            "page missing ADR / EU 2019/790 attribution")

    def test_no_backend_api_under_laws(self):
        """ADR 0012 forbids server-side fetches from mvlaw.gov.mv.
        Sanity-check: no /api/laws/* endpoint should exist."""
        r = self.c.get("/api/laws/search?q=test")
        self.assertIn(r.status_code, (404, 405))


class TruthScoreLadderVizTests(unittest.TestCase):
    """The /api/viz/truth-score-ladder endpoint always returns the
    6 rungs in canonical order, even when some have zero counts."""
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_returns_six_rungs_in_canonical_order(self):
        r = self.c.get("/api/viz/truth-score-ladder")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["labels"],
                          ["TRUE", "MOSTLY_TRUE", "HALF_TRUE",
                           "MOSTLY_FALSE", "FALSE", "PANTS_ON_FIRE"])
        self.assertEqual(len(body["values"]), 6)
        for v in body["values"]:
            self.assertIsInstance(v, int)
            self.assertGreaterEqual(v, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
