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


class NoExternalCDNScriptsTests(unittest.TestCase):
    """Kahzaabu's UI must work fully offline / behind firewalls /
    in zero-third-party-fetch deployments. ADR 0012 establishes
    the same posture for content (link-out instead of import);
    this test extends it to runtime JavaScript.

    Every <script src="..."> reference in any static HTML page must
    point to a local /static/... path. External CDN URLs are banned;
    vendor the library into kahzaabu/web/static/js/ and add it to
    NOTICE.md instead.
    """
    def test_no_static_html_loads_a_cdn_script(self):
        import re
        from pathlib import Path
        static = (Path(__file__).resolve().parents[1]
                  / "kahzaabu" / "web" / "static")
        offenders = []
        # External-script regex: src= followed by a URL with a scheme.
        pat = re.compile(
            r'<script[^>]*\bsrc=["\'](https?://[^"\']+)["\']',
            re.IGNORECASE)
        for page in sorted(static.glob("*.html")):
            for m in pat.finditer(page.read_text()):
                offenders.append(f"{page.name}: {m.group(1)}")
        self.assertEqual(offenders, [],
            "Static page loads JS from an external URL. Vendor the "
            "library to kahzaabu/web/static/js/ and update the "
            "NOTICE.md. Offenders:\n  " + "\n  ".join(offenders))

    def test_vendored_libraries_are_present(self):
        """The NOTICE-listed vendored libraries actually exist on
        disk. If someone removes a file without updating the HTML
        page that references it, the test catches the dead link."""
        from pathlib import Path
        js_dir = (Path(__file__).resolve().parents[1]
                  / "kahzaabu" / "web" / "static" / "js")
        for fn in ("chart.umd.min.js", "marked.min.js", "api.js",
                    "charts.js"):
            f = js_dir / fn
            self.assertTrue(f.exists(), f"missing static JS: {fn}")
            # Sanity: not an HTML 404 page accidentally saved.
            body = f.read_text()[:500].lower()
            self.assertNotIn("<!doctype html", body,
                              f"{fn} appears to be HTML, not JS")


class HTMLCacheHeaderTests(unittest.TestCase):
    """HTML pages contain inline <script> blocks. When we ship a JS
    fix, users with the old HTML cached in their browser still see
    the broken version. Force the browser to always revalidate HTML.
    Static JS/CSS remains cacheable (URLs don't change between
    deploys). API responses are caching-policy-neutral (endpoint-
    specific).
    """
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)

    def test_html_pages_have_no_store(self):
        for path in ("/", "/lies", "/factcheck/1", "/constitution",
                      "/laws", "/contradictions", "/manifesto",
                      "/browse"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
            cc = r.headers.get("cache-control", "")
            self.assertIn("no-store", cc,
                f"{path} returns cache-control={cc!r} — must be "
                "no-store so HTML+inline JS fix-shipping works.")

    def test_static_js_remains_cacheable(self):
        """We do NOT want to bust the cache on stable static assets —
        only the HTML pages."""
        r = self.c.get("/static/js/api.js")
        self.assertEqual(r.status_code, 200)
        cc = r.headers.get("cache-control", "")
        self.assertNotIn("no-store", cc,
            "Static JS should not carry no-store; the cache is fine.")

    def test_api_responses_not_no_store(self):
        """API endpoints set their own caching policy (or none).
        The HTML middleware must not bleed into JSON responses."""
        r = self.c.get("/api/stats")
        self.assertEqual(r.status_code, 200)
        cc = r.headers.get("cache-control", "")
        self.assertNotIn("no-store", cc,
            "/api/stats should not carry no-store — that's HTML-only.")


class StaticPageJSShadowingTests(unittest.TestCase):
    """Regression guards for inline-<script> ↔ api.js name clashes.

    Two distinct cases, two distinct severities:

    HARD FAIL — `const`/`let NAME = …` at top level of an inline
    <script> where `NAME` is also defined in /static/js/api.js.
    Classic-script lexical binding throws
        SyntaxError: Identifier 'NAME' has already been declared
    on parse, which silently aborts the whole inline block. Every
    fetchJSON(...) inside it never runs. The page renders the
    static placeholder forever.

    KNOWN-OK BUT TRACKED — `function NAME(...){...}` at top level
    of an inline <script> where `NAME` is also defined in api.js.
    Function declarations can re-declare in classic scripts and
    silently override the global. Currently safe, but creates a
    maintenance landmine: a future change to api.js's NAME has no
    effect on pages that silently override. We enumerate these so
    they don't grow silently — the test fails if a NEW override
    appears beyond the documented set.
    """

    API_EXPORTS = ("fetchJSON", "el", "escapeHtml", "catClass",
                    "catBadgeClass", "fmtDate", "qs", "setNavActive")

    # Currently-acceptable function-declaration overrides.
    # contradictions.html declares its own `el` + `fmtDate` because
    # the page wants slightly different behaviours for the contradiction
    # cards. If the list needs to grow, add it here with a comment
    # explaining why the override is intentional.
    ALLOWED_FUNCTION_OVERRIDES = frozenset({
        ("contradictions.html", "el"),
        ("contradictions.html", "fmtDate"),
    })

    @staticmethod
    def _static_dir():
        from pathlib import Path
        return (Path(__file__).resolve().parents[1]
                / "kahzaabu" / "web" / "static")

    def test_no_static_page_const_shadows_api_export(self):
        import re
        offenders = []
        for page in sorted(self._static_dir().glob("*.html")):
            text = page.read_text()
            for name in self.API_EXPORTS:
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

    def test_function_overrides_match_allowed_set(self):
        """Function-declaration overrides of api.js exports are
        tolerated (no SyntaxError) but tracked. Adding a new one
        without updating ALLOWED_FUNCTION_OVERRIDES fails the test —
        forces the author to think about why they're shadowing."""
        import re
        found = set()
        for page in sorted(self._static_dir().glob("*.html")):
            text = page.read_text()
            for name in self.API_EXPORTS:
                pat = re.compile(
                    rf"^\s*function\s+{name}\s*\(", re.MULTILINE)
                if pat.search(text):
                    found.add((page.name, name))

        new = found - self.ALLOWED_FUNCTION_OVERRIDES
        removed = self.ALLOWED_FUNCTION_OVERRIDES - found
        self.assertFalse(new,
            "New function-declaration override of an api.js export "
            "found. This silently overrides the global; future "
            "changes to api.js will not propagate to this page. If "
            "intentional, add to ALLOWED_FUNCTION_OVERRIDES. New:\n  "
            + "\n  ".join(f"{p}: function {n}()" for p, n in new))
        self.assertFalse(removed,
            "ALLOWED_FUNCTION_OVERRIDES is stale — these entries no "
            "longer appear in any page; remove them:\n  "
            + "\n  ".join(f"{p}: function {n}()" for p, n in removed))


class StaticPageDataLoadSmokeTests(unittest.TestCase):
    """End-to-end "page renders data" smoke for every V2 surface.

    The StaticPageJSShadowingTests guard catches the *specific*
    parse-time bug that broke /factcheck/{id}. But pages can have
    other data-loading bugs that don't throw SyntaxError — wrong
    API path, response-shape mismatch, missing element selector.

    This test loads each page's HTML via TestClient, scans the
    inline <script> for `fetchJSON("/api/...")` URLs, and verifies
    that EVERY such URL the page depends on actually returns 200
    against the same TestClient. If a page tries to fetch an API
    that doesn't exist (or has been renamed), this surfaces it.
    """

    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(app)
        cls.tested_pages = []

    @staticmethod
    def _sub_template(url: str) -> str:
        """Replace JS template-literal `${...}` placeholders with
        safe test values.

        Heuristics:
          - placeholder containing 'id' or 'n'  → `1` (integer endpoints)
          - placeholder containing 'query', 'q', 'search' → 'religion'
            (FTS endpoints — 'religion' has hits in our corpus)
          - encodeURIComponent(...) wrappers around `${...}` → strip
          - anything else → `1` (safest default — numeric paths)
        """
        import re
        # Strip encodeURIComponent(... ${X} ...) outer call → just ${X}
        url = re.sub(r'encodeURIComponent\(([^)]+)\)', r'\1', url)
        def repl(m):
            inner = m.group(1).lower()
            if 'query' in inner or 'search' in inner or inner.strip() in ('q',):
                return 'religion'
            return '1'
        return re.sub(r'\$\{([^}]+)\}', repl, url)

    def _extract_api_urls(self, html: str) -> list[str]:
        """Pull every kahzaabu API URL referenced by `fetchJSON(...)`
        or `fetch(...)` in the inline <script> blocks.

        Skips calls that pass a `method: "POST" | "PUT" | "DELETE" |
        "PATCH"` option — those endpoints can't be smoke-tested via
        a default GET (would return 405)."""
        import re
        # Find every fetch / fetchJSON call. DOTALL so multiline
        # template literals match.
        call_pat = re.compile(
            r'fetch(?:JSON)?\(\s*'                # fetch( or fetchJSON(
            r'([`"\'])(/api/[^`"\']+?)\1'        # URL
            r'(\s*,\s*\{[^}]*method:\s*'         # optional { method: "X" }
            r'["\'](?:POST|PUT|DELETE|PATCH)["\'][^}]*\})?',
            re.DOTALL,
        )
        out: list[str] = []
        for m in call_pat.finditer(html):
            if m.group(3):
                # Has an explicit non-GET method; skip — can't smoke
                # via TestClient.get().
                continue
            out.append(self._sub_template(m.group(2)))
        return out

    def _smoke_page(self, path: str, expected_min_apis: int = 1):
        page = self.c.get(path)
        self.assertEqual(page.status_code, 200,
                          f"{path} → HTTP {page.status_code}")
        urls = self._extract_api_urls(page.text)
        self.assertGreaterEqual(
            len(urls), expected_min_apis,
            f"{path} declares < {expected_min_apis} fetchJSON calls "
            f"— expected the page to be data-driven. Found: {urls}")
        # Every API the page depends on must respond with 200.
        for u in urls:
            r = self.c.get(u)
            self.assertEqual(
                r.status_code, 200,
                f"{path} fetches {u} but it returns {r.status_code}")
        self.tested_pages.append((path, len(urls)))

    def test_factcheck_detail_data_loads(self):
        self._smoke_page("/factcheck/1", expected_min_apis=2)

    def test_constitution_page_data_loads(self):
        self._smoke_page("/constitution", expected_min_apis=1)

    def test_lies_page_data_loads(self):
        self._smoke_page("/lies", expected_min_apis=1)

    def test_contradictions_page_data_loads(self):
        self._smoke_page("/contradictions", expected_min_apis=1)

    def test_dashboard_data_loads(self):
        self._smoke_page("/", expected_min_apis=3)

    def test_manifesto_page_data_loads(self):
        self._smoke_page("/manifesto", expected_min_apis=1)

    def test_browse_page_data_loads(self):
        self._smoke_page("/browse", expected_min_apis=1)


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
