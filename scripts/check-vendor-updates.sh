#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Check the npm registry for newer versions of the JavaScript libraries
# kahzaabu vendors under kahzaabu/web/static/js/.
#
# Vendored libs aren't picked up by Dependabot or other lockfile scanners
# (they're not in package.json — kahzaabu has no package.json at all).
# This script gives the maintainer a manual heads-up that an upgrade is
# available, without auto-updating (which would risk breaking JS APIs).
#
# Usage:
#   scripts/check-vendor-updates.sh
#   scripts/check-vendor-updates.sh --quiet     # exit 0 if up-to-date, 1 if drift
#
# Run cadence suggestion: monthly, or whenever a CVE advisory for
# Chart.js or marked is published. The NOTICE.md documents the refresh
# command for each lib.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTICE="$ROOT/kahzaabu/web/static/js/NOTICE.md"
QUIET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet|-q) QUIET=1; shift ;;
    --help|-h)
      sed -n '4,20p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$NOTICE" ]]; then
  echo "missing NOTICE: $NOTICE" >&2; exit 2
fi

# Parse NOTICE.md's pin table. Lines look like:
#   | `chart.umd.min.js` | Chart.js | 4.4.0 | MIT | https://... |
# Parallel arrays — bash associative-array keys can't contain dots
# on some bash versions, so `chart.js` (the real npm package name)
# would break with declare -A.
PKGS=(chart.js marked)
ROWS=('`chart.umd.min.js`' '`marked.min.js`')

PINNED=()
for row in "${ROWS[@]}"; do
  v=$(grep -F "$row" "$NOTICE" \
        | awk -F'|' '{print $4}' | tr -d ' ' | head -1)
  PINNED+=("$v")
done

# Verify each pin was found.
for i in "${!PKGS[@]}"; do
  if [[ -z "${PINNED[$i]}" ]]; then
    echo "could not parse pinned version for ${PKGS[$i]} from $NOTICE" >&2
    exit 2
  fi
done

drift=0
for i in "${!PKGS[@]}"; do
  pkg="${PKGS[$i]}"
  pinned="${PINNED[$i]}"
  # npm's public registry exposes the latest version on the dist-tags
  # endpoint. Returns plain JSON with no auth needed.
  latest=$(curl -sSfL --max-time 10 \
            "https://registry.npmjs.org/-/package/$pkg/dist-tags" \
          | python3 -c 'import sys,json; print(json.load(sys.stdin).get("latest",""))') \
          || latest=""

  if [[ -z "$latest" ]]; then
    echo "⚠️  $pkg: could not fetch latest version from npm" >&2
    drift=2; continue
  fi

  if [[ "$pinned" == "$latest" ]]; then
    [[ $QUIET -eq 0 ]] && printf "  ✓ %-12s pinned=%-10s latest=%s (up to date)\n" \
        "$pkg" "$pinned" "$latest"
  else
    drift=1
    printf "  ⚠ %-12s pinned=%-10s latest=%s (UPDATE AVAILABLE)\n" \
        "$pkg" "$pinned" "$latest"
  fi
done

if [[ $drift -ne 0 ]]; then
  [[ $QUIET -eq 0 ]] && {
    echo
    echo "To update a library:"
    echo "  1. See kahzaabu/web/static/js/NOTICE.md for the curl recipe"
    echo "  2. Test in a browser — major-version bumps often break APIs"
    echo "  3. Bump the version in NOTICE.md"
    echo "  4. Run ./scripts/test.sh + commit"
  }
  exit 1
fi

[[ $QUIET -eq 0 ]] && echo "✓ all vendored libs at latest version"
exit 0
