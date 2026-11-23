#!/usr/bin/env bash
# Used by the compose `backup` service: run backup.sh every day at BACKUP_HOUR (server time, default 02).
set -uo pipefail
HOUR="${BACKUP_HOUR:-02}"
while true; do
  now=$(date +%s); next=$(date -d "today ${HOUR}:00" +%s)
  [ "$next" -le "$now" ] && next=$(date -d "tomorrow ${HOUR}:00" +%s)
  sleep $((next - now))
  /scripts/backup.sh || echo "BACKUP FAILED at $(date)" >&2
  /scripts/maintenance.sh || echo "MAINTENANCE FAILED at $(date)" >&2
done
