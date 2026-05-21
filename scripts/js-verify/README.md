# Vendored-JS verifier

After an upgrade of any library under `kahzaabu/web/static/js/`, run
this to confirm the new version still works with kahzaabu's actual
call sites — not just that it loads.

```bash
cd scripts/js-verify
npm install --silent     # one-time; installs jsdom (~50MB devtime)
npm run verify           # exits 0 if all libs OK, 1 if any broke
```

What it checks:

- **marked.parse(text) → HTML string**, with a sample input containing
  headings, bold, and a `[link](/article/N)` reference. Asserts the
  exact HTML shape kahzaabu's `ask.html:158` depends on (the article-
  ID rewrite regex on the next line assumes `<a href="/article/N">`).

- **new Chart(ctx, config) + Chart.getChart(canvas)**, the two
  Chart.js entry points kahzaabu uses across `charts.js` and the
  dashboard. Uses a Proxy-based stub canvas context (jsdom doesn't
  ship a real 2D context).

Failures should block the upgrade commit. If you need to change the
expected shape (e.g. the markdown link rewrite), update both the
verifier and the consumer.

## Why a separate package.json

jsdom is a one-off devtime dependency only this script needs. Keeping
it scoped here:

- The Python project's `pyproject.toml` stays the only language
  manifest at the repo root
- No Node toolchain pollution for users who only run the Python
  pipeline / Hermes plugin
- `node_modules/` is gitignored under this directory (see `.gitignore`)

## When to run

| Event                                         | Run? |
|---|---|
| Vendored lib upgrade (e.g. `chart.js@X→Y`)     | **Yes** |
| Routine PR that doesn't touch `static/js/`     | No   |
| CI                                             | Optional — adds a Node dep to CI |
| Release prep                                   | Yes  |

If you wire this into CI, make sure the CI image has Node 20+ (Node's
global fetch is required) and budget ~50MB of disk for `node_modules`.
