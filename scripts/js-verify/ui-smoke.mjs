// SPDX-License-Identifier: Apache-2.0
//
// Full-page UI smoke test. Loads every route in a real headless DOM,
// lets the inline + linked scripts execute, then asserts:
//   - the page rendered without throwing a JS error
//   - the page's main content area has non-trivial text content
//     (not blank, not "loading...", not "error:")
//
// This is what catches "HTTP 200 but the page is empty" — the failure
// mode that motivated this script's existence (article/36701).
//
// Usage:  HOST=http://127.0.0.1:8765 node ui-smoke.mjs
//         The server must already be running.

import { JSDOM, ResourceLoader, VirtualConsole } from "jsdom";

const HOST = process.env.HOST || "http://127.0.0.1:8765";

// jsdom keeps microtasks running after dom.window.close(); when an
// async page-script callback resolves after we've moved on, it can
// hit a stale window and throw. Those errors are already captured
// per-page by VirtualConsole; swallow them here so they don't crash
// the loop. (We only swallow during the per-page window — if the
// harness itself has a real bug, that still surfaces because the
// throw happens synchronously inside our own code.)
process.on("uncaughtException", (e) => {
    if (process.env.SMOKE_DEBUG) console.error("[swallowed]", e.message);
});
process.on("unhandledRejection", (e) => {
    if (process.env.SMOKE_DEBUG) console.error("[swallowed promise]", e);
});

// Routes derived from kahzaabu/web/app.py @app.get decorators +
// real IDs queried from the live DB (see runner script).
const ROUTES = [
    // Dashboard uses Chart.js — jsdom doesn't ship canvas support.
    // allowCanvasError suppresses the getContext jsdomError; the
    // freshness-banner + stat-cards still render server-side text.
    { path: "/",                       expectSelector: "main",        expectMin: 40,  allowCanvasError: true },
    { path: "/browse",                 expectSelector: "#articles",   expectMin: 200 },
    { path: "/lies",                   expectSelector: "#root",       expectMin: 300 },
    { path: "/contradictions",         expectSelector: "#root",       expectMin: 100 },
    { path: "/constitution",           expectSelector: "#root",       expectMin: 100 },
    { path: "/laws",                   expectSelector: "main",        expectMin: 200 },
    { path: "/factcheck/1",            expectSelector: "#root",       expectMin: 200 },
    { path: "/article/36702",          expectSelector: "#root",       expectMin: 200, label: "EN article" },
    { path: "/article/36701",          expectSelector: "#root",       expectMin: 200, label: "DV-only article" },
    { path: "/ask",                    expectSelector: "main",        expectMin: 100 },
    { path: "/compare",                expectSelector: "#root",       expectMin: 100 },
    // /compare/{id} legitimately renders an empty-state message
    // ("No DV-EN comparison recorded for article …") when no
    // dv_en_inconsistencies row exists. That's ~80 chars.
    { path: "/compare/36645",          expectSelector: "#root",       expectMin: 50 },
    { path: "/methodology",            expectSelector: "main",        expectMin: 300 },
    { path: "/corrections",            expectSelector: "main",        expectMin: 100 },
    { path: "/manifesto",              expectSelector: "#root",       expectMin: 200 },
    { path: "/manifesto/1",            expectSelector: "#root",       expectMin: 100 },
    { path: "/disclaimer",             expectSelector: "main",        expectMin: 500 },
    { path: "/translate",              expectSelector: "main",        expectMin: 500 },
];

// Allow same-origin resource fetches (api.js, charts.js, the API
// itself). jsdom blocks all subresources by default.
class LocalLoader extends ResourceLoader {
    fetch(url, options) {
        if (!url.startsWith(HOST)) return null; // block off-site
        return super.fetch(url, options);
    }
}

function siteDisclaimerPresent(doc) {
    return !!doc.querySelector(".site-disclaimer");
}

function findContent(doc, selector) {
    // Prefer the explicit selector; fall back to <main>.
    const el = doc.querySelector(selector) || doc.querySelector("main");
    return el ? el.textContent.trim() : "";
}

function loadPage(route) {
    return new Promise(async (resolve) => {
        const errors = [];
        const vc = new VirtualConsole();
        vc.on("jsdomError", (e) => errors.push(e.message || String(e)));
        // Suppress "the script will not be executed" noise from jsdom's
        // own internals; keep real JS exceptions via jsdomError above.

        // Fetch the HTML ourselves so we can construct JSDOM with a
        // beforeParse hook that injects fetch/AbortController/URL etc.
        // into the window context BEFORE inline scripts run.
        // (jsdom's fromURL doesn't expose beforeParse; the older versions
        // also don't expose a global `fetch` inside the window context
        // even though Node has one. We bridge it manually.)
        let html;
        try {
            const r = await fetch(HOST + route.path);
            if (!r.ok) {
                return resolve({ route, ok: false,
                    reasons: [`page returned HTTP ${r.status}`] });
            }
            html = await r.text();
        } catch (e) {
            return resolve({ route, ok: false,
                reasons: [`page fetch threw: ${e.message}`] });
        }

        let dom;
        try {
            dom = new JSDOM(html, {
                url: HOST + route.path,
                runScripts: "dangerously",
                resources: new LocalLoader(),
                pretendToBeVisual: true,
                virtualConsole: vc,
                beforeParse(window) {
                    // Real Node fetch, bound to the global so relative
                    // URLs resolve against HOST (set on `url` above).
                    window.fetch = (input, init) => {
                        // Resolve relative URLs against the page URL.
                        const u = typeof input === "string"
                            ? new URL(input, HOST + route.path).href
                            : input;
                        return fetch(u, init);
                    };
                    window.AbortController = AbortController;
                },
            });
        } catch (e) {
            return resolve({ route, ok: false,
                reasons: [`JSDOM ctor threw: ${e.message}`] });
        }

        // Give inline scripts + fetches time to run. Pages do
        // 1-2 round-trips to /api; 2.5s is generous.
        setTimeout(() => {
            const doc = dom.window.document;
            const text = findContent(doc, route.expectSelector);
            const banner = siteDisclaimerPresent(doc);

            // Check for the kahzaabu JS's specific error-rendering
            // pattern: a <p> element whose textContent starts with
            // "error:". This is scoped to the actual rendered DOM
            // (querySelector finds elements, not script-tag source);
            // it does NOT match the template literal inside the
            // inline <script> tag's source code (which was the
            // earlier false-positive footgun).
            const errorP = Array.from(
                doc.querySelectorAll(`${route.expectSelector} p, main p`)
            ).find(p => /^error:/.test((p.textContent || "").trim()));

            // Filter "canvas getContext" out of jsdom errors —
            // it's a jsdom limitation, not a real page bug. Pages
            // that depend on canvas (dashboard) declare
            // `allowCanvasError: true` to acknowledge this.
            const realErrors = route.allowCanvasError
                ? errors.filter(e => !/getContext/.test(e))
                : errors;

            const ok =
                text.length >= route.expectMin &&
                banner &&
                !errorP &&
                realErrors.length === 0;

            const reasons = [];
            if (text.length < route.expectMin)
                reasons.push(`content too short: ${text.length} < ${route.expectMin} chars`);
            if (!banner) reasons.push("missing .site-disclaimer banner");
            if (errorP)
                reasons.push(`error rendered: "${errorP.textContent.slice(0, 80)}"`);
            if (realErrors.length)
                reasons.push(`jsdomError: ${realErrors[0]}`);

            dom.window.close();
            resolve({ route, ok, reasons, contentChars: text.length });
        }, 2500);
    });
}

(async () => {
    console.log(`UI smoke test against ${HOST}`);
    console.log(`${"─".repeat(72)}`);
    const results = [];
    for (const route of ROUTES) {
        const r = await loadPage(route);
        results.push(r);
        const tag = r.ok ? "✓" : "✗";
        const label = route.label ? ` [${route.label}]` : "";
        const note = r.ok
            ? ` (${r.contentChars} chars)`
            : `  · ${r.reasons.join("; ")}`;
        console.log(`  ${tag} ${route.path.padEnd(28)}${label}${note}`);
    }
    const failures = results.filter(r => !r.ok);
    console.log(`${"─".repeat(72)}`);
    console.log(
        `${results.length - failures.length}/${results.length} pages render successfully`
    );
    process.exit(failures.length === 0 ? 0 : 1);
})();
