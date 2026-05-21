#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Restore the kahzaabu SQLite database from a dated backup.
#
# Default behaviour is conservative: refuses to overwrite a live DB
# unless --force is given, and writes the restored DB to a side path
# the user must then move into place. See ADR 0009.
#
# Usage:
#   scripts/restore.sh                       # restore latest backup
#   scripts/restore.sh 2026-05-21            # restore that date
#   scripts/restore.sh --list                # list available backups
#   scripts/restore.sh --target /tmp/x.db    # custom output path
#   scripts/restore.sh 2026-05-21 --force    # overwrite data/kahzaabu.db
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$ROOT/data/backups"
LIVE_DB="$ROOT/data/kahzaabu.db"

DATE=""
TARGET=""
FORCE=0
LIST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list|-l)    LIST=1; shift ;;
    --target)     TARGET="$2"; shift 2 ;;
    --force)      FORCE=1; shift ;;
    --help|-h)    sed -n '5,16p' "$0"; exit 0 ;;
    *)            DATE="$1"; shift ;;
  esac
done

if [[ $LIST -eq 1 ]]; then
  if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "(no backups yet — $BACKUP_DIR does not exist)" >&2
    exit 0
  fi
  echo "Available backups in $BACKUP_DIR:"
  ls -1tr "$BACKUP_DIR"/*.sql.gz 2>/dev/null | while read -r f; do
    echo "  $(du -h "$f" | awk '{print $1}')  $(basename "$f" .sql.gz)"
  done
  exit 0
fi

if [[ -z "$DATE" ]]; then
  # latest by mtime
  SRC=$(ls -1tr "$BACKUP_DIR"/*.sql.gz 2>/dev/null | tail -1 || true)
  if [[ -z "$SRC" ]]; then
    echo "no backups found in $BACKUP_DIR" >&2
    exit 1
  fi
else
  SRC="$BACKUP_DIR/$DATE.sql.gz"
  if [[ ! -f "$SRC" ]]; then
    echo "no backup for date $DATE (expected: $SRC)" >&2
    echo "use --list to see what's available" >&2
    exit 1
  fi
fi

# Default target: refuse to clobber the live DB unless --force.
if [[ -z "$TARGET" ]]; then
  if [[ $FORCE -eq 1 ]]; then
    TARGET="$LIVE_DB"
  else
    TARGET="$ROOT/data/kahzaabu-restored-$(date +%Y%m%d-%H%M%S).db"
  fi
fi

if [[ -f "$TARGET" && "$TARGET" == "$LIVE_DB" && $FORCE -ne 1 ]]; then
  echo "target $TARGET exists and --force not given; aborting" >&2
  exit 1
fi

echo "Restoring $SRC → $TARGET"
[[ -f "$TARGET" ]] && rm -f "$TARGET"
gunzip -c "$SRC" | sqlite3 "$TARGET"
echo "  done"

# Sanity-check: row counts on a couple of stable tables.
ROWS_ARTICLES=$(sqlite3 "$TARGET" "SELECT COUNT(*) FROM articles" 2>/dev/null || echo "?")
ROWS_CLAIMS=$(sqlite3 "$TARGET" "SELECT COUNT(*) FROM claims" 2>/dev/null || echo "?")
echo "Sanity check: articles=$ROWS_ARTICLES  claims=$ROWS_CLAIMS"

if [[ "$TARGET" != "$LIVE_DB" ]]; then
  echo ""
  echo "Restored to a side path. To put it live:"
  echo "  mv \"$LIVE_DB\" \"$LIVE_DB.before-restore-$(date +%Y%m%d-%H%M%S)\""
  echo "  mv \"$TARGET\" \"$LIVE_DB\""
fi
