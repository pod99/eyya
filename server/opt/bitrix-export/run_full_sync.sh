#!/usr/bin/env bash
set -euo pipefail

cd /opt/bitrix-export
mkdir -p /var/log/bitrix-export

lock_file=/tmp/bitrix-dashboard-sync.lock
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "[$(date -Is)] another sync is running"
  exit 0
fi

log_file="/var/log/bitrix-export/full-sync-$(date +%F).log"
echo "[$(date -Is)] full sync started" >> "$log_file"

for entity in statuses users products companies contacts leads deals activities tasks stage_history; do
  echo "[$(date -Is)] sync $entity" >> "$log_file"
  docker compose run --rm -e SYNC_ONLY="$entity" sync >> "$log_file" 2>&1
done

docker exec -i bitrix-export-db-1 psql -U bitrix -d bitrix_export -v ON_ERROR_STOP=1 -f /dev/stdin < /opt/bitrix-export/quality_views.sql >> "$log_file" 2>&1

echo "[$(date -Is)] ff sync after bitrix" >> "$log_file"
/opt/eyya-dashboard/sync-ff.sh >> "$log_file" 2>&1 || echo "[$(date -Is)] ff sync failed" >> "$log_file"
echo "[$(date -Is)] cdek sync after ff" >> "$log_file"
/opt/eyya-dashboard/sync-cdek.sh >> "$log_file" 2>&1 || echo "[$(date -Is)] cdek sync failed" >> "$log_file"
echo "[$(date -Is)] full sync finished" >> "$log_file"
