#!/bin/bash
# Deploy kahzaabu to a remote VPS.
#
# What it does:
#   1. rsync the project code to the server
#   2. rsync the SQLite DB (excluding -shm/-wal lock files)
#   3. ssh to install/refresh the venv if dependencies changed
#   4. restart the systemd service
#
# Prerequisites (one-time, manual on the server):
#   - ssh access as `sofwath` (or set REMOTE_USER)
#   - target directory exists at /srv/kahzaabu (writable by kahzaabu user)
#   - systemd unit installed and enabled (see kahzaabu-web.service)
#   - Caddy installed and configured (see Caddyfile)
#   - /etc/kahzaabu/env contains ANTHROPIC_API_KEY=..., KAHZAABU_PUBLIC_MODE=1
#
# Usage:
#   REMOTE=kahzaabu.example.com ./scripts/deploy.sh
#   REMOTE=user@host:/path ./scripts/deploy.sh

set -euo pipefail

REMOTE="${REMOTE:-kahzaabu.example.com}"
REMOTE_USER="${REMOTE_USER:-sofwath}"
REMOTE_PATH="${REMOTE_PATH:-/srv/kahzaabu}"

if [[ "$REMOTE" != *":"* ]]; then
    TARGET="${REMOTE_USER}@${REMOTE}:${REMOTE_PATH}"
else
    TARGET="$REMOTE"
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "==> Syncing code to ${TARGET}"
rsync -avz --delete \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='.git/' \
    --exclude='data/' \
    --exclude='*.pyc' \
    --exclude='/tmp/' \
    --include='/' \
    ./ "${TARGET}/"

echo "==> Syncing SQLite DB (excluding lock files)"
rsync -avz \
    --include='kahzaabu.db' \
    --exclude='*.db-shm' \
    --exclude='*.db-wal' \
    --exclude='*' \
    data/ "${TARGET}/data/"

echo "==> Installing / refreshing venv on remote"
ssh "${REMOTE_USER}@${REMOTE%%:*}" bash <<EOF
set -euo pipefail
cd ${REMOTE_PATH}
if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -e . --quiet
EOF

echo "==> Restarting systemd service"
ssh "${REMOTE_USER}@${REMOTE%%:*}" 'sudo systemctl restart kahzaabu-web'

echo "==> Verifying"
sleep 2
ssh "${REMOTE_USER}@${REMOTE%%:*}" 'sudo systemctl status kahzaabu-web --no-pager -l | head -10'

echo "✓ deployed"
echo "  open: https://${REMOTE%%:*}/"
