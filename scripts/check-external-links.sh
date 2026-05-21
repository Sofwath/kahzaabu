#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Link-rot detector for the /laws page tiles (ADR 0012).
#
# /laws is a link-out page to old.mvlaw.gov.mv. The 6 tile URLs are
# hardcoded JS data in kahzaabu/web/static/laws.html. If the AGO
# renames any of them (e.g. ganoon_main.php → laws.php), the page
# silently 404s in the user's new tab and the kahzaabu test suite
# can't see it — the JS is just constructing strings.
#
# This script does HEAD requests against the live mvlaw URLs and
# reports any that aren't 200. Operator-initiated maintenance, NOT
# wired into CI by default (would make the suite flaky on transient
# upstream outages).
#
# Usage:
#   scripts/check-external-links.sh
#   scripts/check-external-links.sh --quiet     # exit 0 if all OK, 1 if any 404

set -euo pipefail

QUIET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet|-q) QUIET=1; shift ;;
    --help|-h)  sed -n '4,16p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAGE="$ROOT/kahzaabu/web/static/laws.html"
if [[ ! -f "$PAGE" ]]; then
  echo "missing $PAGE" >&2; exit 2
fi

# Parse the SECTIONS array's `path:` entries from the laws.html JS.
# Each line looks like:
#   { dv: "...", en: "...", path: "/foo.php", desc: "..." },
# We extract everything after `path: "` up to the next `"`.
# bash 3.2 (macOS) doesn't have `mapfile`; use a portable read loop.
PATHS=()
while IFS= read -r p; do
  [[ -n "$p" ]] && PATHS+=("$p")
done < <(grep -oE 'path:[[:space:]]*"/[^"]*"' "$PAGE" \
           | sed -E 's/.*path:[[:space:]]*"//' | sed -E 's/"$//')

# CANONICAL_HOST comes from the same file.
HOST=$(grep -oE 'CANONICAL_HOST\s*=\s*"[^"]+"' "$PAGE" \
        | sed -E 's/.*"([^"]+)".*/\1/')

if [[ -z "$HOST" || ${#PATHS[@]} -eq 0 ]]; then
  echo "could not parse CANONICAL_HOST / SECTIONS from $PAGE" >&2
  exit 2
fi

[[ $QUIET -eq 0 ]] && echo "Probing $HOST tile URLs from $PAGE …"
bad=0
for path in "${PATHS[@]}"; do
  url="https://${HOST}${path}"
  # HEAD avoids pulling the full body. Some servers reject HEAD; if so,
  # fall back to a Range:0-0 GET that fetches a single byte.
  code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 12 -I "$url" || echo "000")
  if [[ "$code" == "405" || "$code" == "501" || "$code" == "000" ]]; then
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 12 \
             -H "Range: bytes=0-0" "$url" || echo "000")
  fi
  if [[ "$code" == "200" || "$code" == "206" ]]; then
    [[ $QUIET -eq 0 ]] && printf "  ✓ %s  %s\n" "$code" "$url"
  else
    bad=$((bad + 1))
    printf "  ✗ %s  %s\n" "$code" "$url"
  fi
done

if [[ $bad -gt 0 ]]; then
  echo
  echo "❌ $bad of ${#PATHS[@]} tile URL(s) returned non-OK."
  echo "   /laws will surface a 404 for those tiles. Update the SECTIONS"
  echo "   array in kahzaabu/web/static/laws.html (search for"
  echo "   'CANONICAL_HOST' to find the registry block)."
  exit 1
fi

[[ $QUIET -eq 0 ]] && echo "✓ all ${#PATHS[@]} tile URL(s) reachable."
exit 0
