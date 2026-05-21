#!/usr/bin/env bash
# scripts/ci-dry-run.sh — execute the .github/workflows/test.yml steps
# locally against a CLEAN copy of the repo, so you can validate the
# workflow before it ever runs on a real GitHub remote.
#
# Strategy: git worktree add a sibling checkout, work entirely inside
# that tree, leave the live working copy untouched. Removes the
# worktree on success; on failure leaves it for inspection.
#
# Run:
#     ./scripts/ci-dry-run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKTREE="$(mktemp -d -t kahzaabu-ci-dryrun-XXXXXX)"
trap 'echo; echo "Worktree preserved at: $WORKTREE  (delete with: git -C $REPO_ROOT worktree remove --force $WORKTREE)"' ERR

# Validates the CURRENT HEAD COMMIT, NOT your working tree. The whole
# point is to mirror CI, which runs against the committed code that
# would actually land on the remote. If you're iterating on the
# workflow file itself, commit your changes first (git commit --amend
# is fine) then re-run.
echo "──────────────────────────────────────────────────────────────"
echo " ci-dry-run.sh — validating HEAD ($(git -C "${REPO_ROOT}" rev-parse --short HEAD))"
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
    echo " ⚠ working tree has uncommitted changes — they will be IGNORED"
    echo "   Commit first if you want to test them. Continuing in 3s..."
    sleep 3
fi
echo "──────────────────────────────────────────────────────────────"

echo "→ Setting up worktree at $WORKTREE"
git -C "${REPO_ROOT}" worktree add "$WORKTREE" HEAD >/dev/null
cd "$WORKTREE"

echo "→ Step 1: create venv"
python3 -m venv .venv
. .venv/bin/activate

echo "→ Step 2: install editable"
python -m pip install --quiet --upgrade pip
pip install --quiet -e . 2>&1 | tail -5

echo "→ Step 3: stage hermes-stub plugin path"
mkdir -p hermes-stub/plugins
ln -sf "$(pwd)/hermes-plugin" hermes-stub/plugins/kahzaabu
touch hermes-stub/plugins/__init__.py

echo "→ Step 4: bootstrap DB"
# Constitution import requires the source txt — which is committed
mkdir -p data
python -c "
import sqlite3
from kahzaabu import claims_db, db
from pathlib import Path
p = Path('data/kahzaabu.db')
conn = sqlite3.connect(str(p))
db.init_db(conn)
claims_db.init_claims_schema(conn)
conn.close()
print(f'Created {p} ({p.stat().st_size} bytes)')

# Also import the constitution so lookup tests have data
import sqlite3 as s
from kahzaabu.constitution import import_constitution
conn = s.connect(str(p))
n = import_constitution(conn)
print(f'Imported {n} constitution articles')
conn.close()
"

echo "→ Step 5: run unit tests"
# Safe append even if PYTHONPATH is unset (set -u would trip on bare $PYTHONPATH)
export PYTHONPATH="$(pwd)/hermes-stub${PYTHONPATH:+:$PYTHONPATH}"
python -m unittest discover tests/ -v 2>&1 | tail -8

echo "→ Step 6: stale-name check"
if grep -rln "test_system\.py" \
       --exclude-dir=.git --exclude-dir=.venv \
       --exclude=test.sh --exclude=test.yml --exclude=ci-dry-run.sh \
       . 2>/dev/null; then
    echo "❌ found stale references"
    exit 1
fi
echo "  ✓ clean"

echo
echo "✅ CI workflow steps all pass in a fresh worktree."
echo
echo "Cleaning up..."
cd "${REPO_ROOT}"
git worktree remove --force "$WORKTREE"
echo "Worktree removed: $WORKTREE"
