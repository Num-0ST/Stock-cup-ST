#!/usr/bin/env bash
# Consistent PostgreSQL backup (+ uploaded import files) with verification and retention.
#   env: PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE   BACKUP_DIR (default ./backups)
#        UPLOAD_DIR (optional, tar'd if it exists)     KEEP_DAYS (default 14)
set -euo pipefail
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DB="${PGDATABASE:-wms}"
mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/${DB}-${STAMP}.dump"
TMP="$OUT.partial"

# -Fc: compressed custom format; one snapshot-consistent transaction (no table locks that block the app)
pg_dump -Fc --no-owner --no-privileges -f "$TMP" "$DB"
# verify the archive is readable and contains data before we trust it
pg_restore --list "$TMP" > /dev/null
test "$(pg_restore --list "$TMP" | grep -c 'TABLE DATA')" -ge 20 || { echo "backup looks incomplete" >&2; rm -f "$TMP"; exit 1; }
mv "$TMP" "$OUT"
( cd "$BACKUP_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )

if [ -n "${UPLOAD_DIR:-}" ] && [ -d "$UPLOAD_DIR" ]; then
  tar -czf "$BACKUP_DIR/${DB}-${STAMP}-uploads.tar.gz" -C "$UPLOAD_DIR" .
fi

# retention
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name "${DB}-*.dump" -o -name "${DB}-*.dump.sha256" -o -name "${DB}-*-uploads.tar.gz" \) -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
