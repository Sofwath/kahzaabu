#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Nightly backup of the kahzaabu SQLite database.
#
# Strategy: sqlite3 .dump | gzip → data/backups/YYYY-MM-DD.sql.gz.
# Using .dump (not file copy) so backups are robust to WAL state and
# trivially restorable on any SQLite version. See ADR 0009.
#
# Retention: 30 days locally. Off-machine sync is the operator's
# responsibility (rclone / rsync / cloud bucket).
#
# Usage:
#   scripts/backup.sh                  # backup → data/backups/$(date +%F).sql.gz
#   scripts/backup.sh --quiet          # suppress stdout (for cron use)
#   scripts/backup.sh --retention 90   # keep 90 days instead of default 30
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="$ROOT/data/kahzaabu.db"
BACKUP_DIR="$ROOT/data/backups"
RETENTION_DAYS=30
QUIET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet)        QUIET=1; shift ;;
    --retention)    RETENTION_DAYS="$2"; shift 2 ;;
    --help|-h)      sed -n '5,16p' "$0"; exit 0 ;;
    *)              echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

log() { [[ $QUIET -eq 0 ]] && echo "$@" || true; }

if [[ ! -f "$DB" ]]; then
  echo "kahzaabu DB not found at: $DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
DATE="$(date +%F)"
OUT="$BACKUP_DIR/$DATE.sql.gz"

log "Backing up $DB → $OUT"
# .dump emits portable SQL; gzip cuts ~85% off a 900 MB DB.
sqlite3 "$DB" .dump | gzip > "$OUT"
SIZE=$(du -h "$OUT" | awk '{print $1}')
log "  done — $SIZE"

# Retention: prune anything older than RETENTION_DAYS days.
log "Pruning backups older than ${RETENTION_DAYS} days from $BACKUP_DIR"
PRUNED=$(find "$BACKUP_DIR" -maxdepth 1 -name '*.sql.gz' \
                            -type f -mtime "+${RETENTION_DAYS}" \
                            -print -delete | wc -l | tr -d ' ')
log "  pruned $PRUNED file(s)"

# Print a current inventory for sanity (newest first).
if [[ $QUIET -eq 0 ]]; then
  log ""
  log "Current backups:"
  ls -1tr "$BACKUP_DIR"/*.sql.gz 2>/dev/null | tail -10 | while read -r f; do
    log "  $(du -h "$f" | awk '{print $1}')  $(basename "$f")"
  done
fi
