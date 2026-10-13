#!/usr/bin/env bash
set -euo pipefail

cd /opt/bitrix-export
mkdir -p /var/log/bitrix-export

log_file="/var/log/bitrix-export/tracking-cdek-sync-$(date +%F).log"
wrapper_lock="/tmp/bitrix-tracking-cdek-sync.lock"
bitrix_lock="/tmp/bitrix-dashboard-sync.lock"

echo "[$(date -Is)] tracking+cdek sync requested" >> "$log_file"

exec 8>"$wrapper_lock"
if ! flock -n 8; then
  echo "[$(date -Is)] tracking+cdek sync already running, skip" >> "$log_file"
  exit 0
fi

exec 9>"$bitrix_lock"
if ! flock -n 9; then
  echo "[$(date -Is)] another bitrix sync is running, skip tracking+cdek; owner sync will run cdek after bitrix" >> "$log_file"
  exit 0
fi

echo "[$(date -Is)] bitrix tracking sync started" >> "$log_file"
for entity in leads deals; do
  echo "[$(date -Is)] sync $entity" >> "$log_file"
  docker compose run --rm -e SYNC_ONLY="$entity" sync >> "$log_file" 2>&1
done

echo "[$(date -Is)] refresh quality views" >> "$log_file"
docker exec -i bitrix-export-db-1 psql -U bitrix -d bitrix_export -v ON_ERROR_STOP=1 -f /dev/stdin < /opt/bitrix-export/quality_views.sql >> "$log_file" 2>&1

echo "[$(date -Is)] cdek sync after bitrix tracking" >> "$log_file"
for attempt in 1 2 3 4; do
  if /opt/eyya-dashboard/sync-cdek.sh >> "$log_file" 2>&1; then
    echo "[$(date -Is)] normalize cdek combos after cdek sync" >> "$log_file"
    if docker ps --format '{{.Names}}' | grep -qx 'eyya-cdek-combo-normalizer'; then
      docker exec eyya-cdek-combo-normalizer node /app/scripts/normalize-cdek-combos.js >> "$log_file" 2>&1
    else
      echo "[$(date -Is)] cdek combo normalizer container is not running, skip one-shot normalize" >> "$log_file"
    fi
    echo "[$(date -Is)] tracking+cdek sync finished" >> "$log_file"
    exit 0
  fi

  rc=$?
  if [ "$attempt" -eq 4 ]; then
    echo "[$(date -Is)] cdek sync failed rc=$rc after $attempt attempts" >> "$log_file"
    exit "$rc"
  fi

  delay=$((attempt * 30))
  echo "[$(date -Is)] cdek sync failed rc=$rc, retry $((attempt + 1))/4 in ${delay}s" >> "$log_file"
  sleep "$delay"
done
