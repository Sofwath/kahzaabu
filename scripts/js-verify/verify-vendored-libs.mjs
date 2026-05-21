// SPDX-License-Identifier: Apache-2.0
//
// Verify that the JavaScript libraries vendored under
// kahzaabu/web/static/js/ still work with the exact call sites
// kahzaabu uses.
//
// Run after every vendored-lib upgrade:
//   cd scripts/js-verify && npm install --silent && node verify-vendored-libs.mjs
//
// Exit 0 = both libs work end-to-end with kahzaabu's APIs.
// Exit 1 = a call site is broken; commit blocked.
//
// jsdom is a one-shot devtime dependency (~50 MB installed). It's
// NOT in package.json at the repo root because the project has no
// other Node runtime needs. Keep it scoped to this script directory.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import pkg from "jsdom";
const { JSDOM, VirtualConsole } = pkg;

const HERE = dirname(fileURLToPath(import.meta.url));
const STATIC = resolve(HERE, "../../kahzaabu/web/static/js");

function makeDom() {
    const errors = [];
    const vc = new VirtualConsole();
    vc.on("error", e => errors.push(String(e.message || e)));
    vc.on("jsdomError", e => errors.push(`jsdom: ${e.message || e}`));
    const dom = new JSDOM(
        `<!DOCTYPE html><html><body><canvas id="c"></canvas></body></html>`,
        { runScripts: "dangerously", virtualConsole: vc,
          pretendToBeVisual: true },
    );
    return { dom, errors };
}

let failures = 0;
function check(name, ok, detail) {
    console.log(`${ok ? "✓" : "❌"}  ${name}`);
    if (detail) console.log(`    ${detail}`);
    if (!ok) failures++;
}

// ── marked.parse() — the only call site is ask.html:158
//    ansBox.innerHTML = marked.parse(t.ans);
{
    const { dom, errors } = makeDom();
    const lib = dom.window.document.createElement("script");
    lib.textContent = readFileSync(`${STATIC}/marked.min.js`, "utf8");
    dom.window.document.head.appendChild(lib);
    const test = dom.window.document.createElement("script");
    test.textContent = `
        window._result = marked.parse(
            "# Hello\\n\\nThis is **bold** and a [link](/article/30296).");
    `;
    dom.window.document.head.appendChild(test);

    const out = dom.window._result || "";
    const ok = errors.length === 0 &&
               out.includes("<h1>") &&
               out.includes("<strong>bold</strong>") &&
               out.includes(`<a href="/article/30296"`);
    check("marked.parse()  — ask.html:158",
          ok, ok ? null : `output: ${out.slice(0, 200)}\n    errors: ${errors.join(" / ")}`);
}

// ── Chart.js — call sites are charts.js (new Chart()) + index.html
//    (Chart.getChart()).
{
    const { dom, errors } = makeDom();
    // Stub the 2D context jsdom doesn't ship. Real rendering is
    // browser-only; we only verify the API surface kahzaabu uses.
    // The `canvas` property MUST return the real canvas element
    // — Chart.getChart() uses it as the registry key.
    const stub = dom.window.document.createElement("script");
    stub.textContent = `
        HTMLCanvasElement.prototype.getContext = function() {
            const realCanvas = this;
            return new Proxy({}, {
                get(_, key) {
                    if (key === "canvas") return realCanvas;
                    if (key === "measureText") return () => ({ width: 0 });
                    if (key === "getTransform") return () =>
                        ({ a:1, b:0, c:0, d:1, e:0, f:0 });
                    if (key === "createLinearGradient" ||
                        key === "createRadialGradient")
                        return () => ({ addColorStop: () => {} });
                    if (key === "getLineDash") return () => [];
                    if (key === "isPointInPath") return () => false;
                    if (key === "getImageData")
                        return () => ({ data: [] });
                    if (key === "createImageData")
                        return () => ({ data: [] });
                    // Default: any other method is a no-op.
                    return () => {};
                },
            });
        };
    `;
    dom.window.document.head.appendChild(stub);

    const lib = dom.window.document.createElement("script");
    lib.textContent = readFileSync(`${STATIC}/chart.umd.min.js`, "utf8");
    dom.window.document.head.appendChild(lib);

    const test = dom.window.document.createElement("script");
    test.textContent = `
        try {
            const canvas = document.getElementById("c");
            const chart = new Chart(canvas, {
                type: "bar",
                data: { labels: ["A","B"], datasets: [{ data: [1, 2] }] },
                options: { animation: false, responsive: false },
            });
            const fetched = Chart.getChart(canvas);
            window._match = (fetched === chart);
            window._type  = chart.config.type;
            window._has_update = typeof chart.update === "function";
        } catch (e) {
            window._err = String(e.message || e);
        }
    `;
    dom.window.document.head.appendChild(test);

    const ok = !dom.window._err &&
               dom.window._match === true &&
               dom.window._type === "bar" &&
               dom.window._has_update === true;
    check("Chart.js (new Chart + Chart.getChart)  — charts.js + index.html",
          ok,
          ok ? null
             : `err=${dom.window._err}  match=${dom.window._match}  type=${dom.window._type}`);
}

if (failures) {
    console.log(`\n❌  ${failures} verifier(s) failed. Do not commit the upgrade.`);
    process.exit(1);
}
console.log("\n✓  all vendored libraries verified against kahzaabu's call sites.");
