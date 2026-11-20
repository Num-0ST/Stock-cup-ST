#!/usr/bin/env bash
# Restore a backup made by backup.sh into an EMPTY database.
#   usage: scripts/restore.sh <file.dump> [target_db]      env: PGHOST PGPORT PGUSER PGPASSWORD
# Refuses to touch a database that already has tables unless FORCE=1 (then it is dropped and recreated).
set -euo pipefail
FILE="${1:?usage: restore.sh <file.dump> [target_db]}"
TARGET="${2:-${PGDATABASE:-wms}}"
[ -f "$FILE" ] || { echo "no such file: $FILE" >&2; exit 1; }
if [ -f "$FILE.sha256" ]; then ( cd "$(dirname "$FILE")" && sha256sum -c "$(basename "$FILE").sha256" ); fi
ADMIN_DB="${ADMIN_DB:-postgres}"
exists=$(psql -d "$ADMIN_DB" -Atc "SELECT 1 FROM pg_database WHERE datname='${TARGET}'")
if [ "$exists" = 1 ]; then
  n=$(psql -d "$TARGET" -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
  if [ "$n" -gt 0 ] && [ "${FORCE:-0}" != 1 ]; then
    echo "database '$TARGET' already has $n tables. Stop the API, then re-run with FORCE=1 to replace it." >&2; exit 2
  fi
  if [ "${FORCE:-0}" = 1 ]; then
    psql -d "$ADMIN_DB" -qc "DROP DATABASE \"$TARGET\" WITH (FORCE)"; exists=0
  fi
fi
[ "$exists" = 1 ] || psql -d "$ADMIN_DB" -qc "CREATE DATABASE \"$TARGET\""
pg_restore --no-owner --no-privileges --exit-on-error -d "$TARGET" "$FILE"
echo "restored $FILE into $TARGET"
psql -d "$TARGET" -Atc "SELECT 'products='||count(*) FROM products UNION ALL SELECT 'ledger_rows='||count(*) FROM stock_transactions"
