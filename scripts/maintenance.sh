#!/usr/bin/env bash
# Daily housekeeping (run by the backup service right after the backup).
# Removes the raw Excel rows of import batches that were finished long ago: they are only needed to
# download the error list right after an import. Documents, stock and the batch summary are kept, and
# duplicate detection works from the documents themselves, so nothing else changes.
set -euo pipefail
KEEP="${IMPORT_ROWS_KEEP_DAYS:-180}"
if [ "$KEEP" -le 0 ]; then echo "maintenance: IMPORT_ROWS_KEEP_DAYS<=0, nothing to do"; exit 0; fi
psql -v ON_ERROR_STOP=1 -q <<SQL
\echo purging raw import rows older than ${KEEP} days
WITH old AS (
    SELECT batch_id FROM import_batches
    WHERE status IN ('CONFIRMED','CANCELLED','FAILED') AND uploaded_at < now() - make_interval(days => ${KEEP})
)
DELETE FROM import_rows r USING old WHERE r.batch_id = old.batch_id;
VACUUM (ANALYZE) import_rows;
ANALYZE stock_transactions;
SQL
echo "maintenance done at $(date)"
