#!/usr/bin/env bash
set -euo pipefail

DB_PATH="${1:-${DATABASE_PATH:-data/bot.db}}"
BACKUP_DIR="${2:-${BACKUP_DIR:-data/backups}}"

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database not found: $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BASENAME="$(basename "$DB_PATH")"
TARGET="$BACKUP_DIR/${BASENAME%.db}-$TIMESTAMP.sqlite3"

cp -a "$DB_PATH" "$TARGET"
echo "$TARGET"
