#!/bin/bash
set -e
cd /opt/bitrix-export
LOG=/opt/bitrix-export/full-load.log
echo "=== START $(date) ===" | tee -a $LOG

docker compose run --rm sync python -c "
import os, psycopg2
conn = psycopg2.connect(os.environ[\"DATABASE_URL\"])
for f in [\"schema.sql\"]:
    with open(f) as fh: sql = fh.read()
    with conn.cursor() as c: c.execute(sql)
conn.commit(); conn.close()
print(\"schema applied\")
" 2>&1 | tee -a $LOG

for entity in activities tasks stage_history; do
  echo "=== SYNC $entity $(date) ===" | tee -a $LOG
  docker compose run --rm -e SYNC_ONLY=$entity sync 2>&1 | tee -a $LOG
done

echo "=== APPLY DATASETS $(date) ===" | tee -a $LOG
docker exec -i bitrix-export-db-1 psql -U bitrix -d bitrix_export < /opt/bitrix-export/sync/datasets.sql 2>&1 | tee -a $LOG

echo "=== DONE $(date) ===" | tee -a $LOG
docker exec bitrix-export-db-1 psql -U bitrix -d bitrix_export -c "
SELECT relname, n_live_tup FROM pg_stat_user_tables WHERE relname LIKE 'bitrix_%' ORDER BY n_live_tup DESC;
" 2>&1 | tee -a $LOG
