#!/usr/bin/env bash
# scripts/test.sh — run the full local test suite.
#
# Runs the offline unit tests. The live-server integration check
# (tests/system_check.py) is NOT included here — it needs a running web
# server with an admin user, so it's invoked separately.
#
# Exit non-zero if anything fails. Suitable for git pre-push hooks or
# CI parity.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

if [[ ! -x .venv/bin/python ]]; then
    echo "❌ .venv/bin/python not found. Create the venv first:"
    echo "     python3 -m venv .venv && .venv/bin/pip install -e ."
    exit 1
fi

echo "→ unit tests"
.venv/bin/python -m unittest discover tests/ -v

echo
echo "→ no stale test_system.py references"
# Exclude this script and the CI workflow — both legitimately reference the
# old name as part of the check itself.
if grep -rln "test_system\.py" \
       --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.venv-mcp \
       --exclude-dir=__pycache__ --exclude-dir=node_modules \
       --exclude=test.sh --exclude=test.yml \
       . 2>/dev/null; then
    echo "❌ found references to the old name"
    exit 1
fi
echo "  ✓ clean"

echo
echo "✅ All checks passed."
echo
echo "Optional next step (needs live web server + admin user):"
echo "    .venv/bin/python tests/system_check.py"
