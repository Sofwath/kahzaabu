# Vendored third-party JavaScript

Kahzaabu vendors a small number of third-party browser libraries into
`kahzaabu/web/static/js/` rather than loading them from a CDN. The
trade-off:

- **Pro:** the UI works offline, behind firewalls that block CDNs, and
  in deployments where the operator doesn't want third-party network
  fetches from end-user browsers.
- **Pro:** consistent posture with ADR 0012 (no third-party content
  fetches at runtime).
- **Con:** the maintainer is responsible for periodic updates.

Each vendored asset retains its original copyright header in the
minified file. The summary below is informational; the canonical
license terms live in the file's leading comment.

| File                       | Library    | Version  | License | Upstream                              |
|---|---|---|---|---|
| `chart.umd.min.js`         | Chart.js   | 4.5.1    | MIT     | https://www.chartjs.org/              |
| `marked.min.js`            | marked     | 18.0.4   | MIT     | https://marked.js.org/                |

## Updating

```bash
# Replace VERSION below with the desired version.
curl -sSfL -o kahzaabu/web/static/js/chart.umd.min.js \
    "https://cdn.jsdelivr.net/npm/chart.js@VERSION/dist/chart.umd.min.js"

# marked: path varies by major version. v12 shipped at /marked.min.js;
# v13+ ships at /lib/marked.umd.min.js. If the v12-style URL 404s,
# fall back to /lib/marked.umd.min.js.
curl -sSfL -o kahzaabu/web/static/js/marked.min.js \
    "https://cdn.jsdelivr.net/npm/marked@VERSION/lib/marked.umd.min.js"
```

After updating, bump the version in this `NOTICE.md` and run the full
test suite (`./scripts/test.sh`). The `NoExternalCDNScriptsTests`
regression guard asserts no `<script src="https://...">` references
remain in any static HTML page.

## Checking for newer versions

Vendored libraries aren't picked up by Dependabot (the project has no
`package.json`). To check whether either library has a newer release:

```bash
./scripts/check-vendor-updates.sh
```

The script reads the pins from this NOTICE.md, queries the npm registry,
and reports drift. Exit 0 = all up-to-date, exit 1 = update available.
Run monthly, or whenever a CVE is announced for either library.

## License compliance

Both libraries are MIT-licensed, which permits redistribution under
Apache-2.0 (kahzaabu's project license) provided that:

1. The original copyright + permission notice is retained — present
   in the leading minified-file comment block of each vendored file.
2. This NOTICE.md exists and is shipped alongside the binaries —
   that's what you are reading.

If you add a new vendored library, document it in the table above and
make sure step (1) is satisfied before committing.
