#!/bin/sh
set -eu
cd /opt/eyya-dashboard
flock -n /tmp/eyya-cdek-sync.lock docker exec   -e CDEK_CLIENT_ID='REPLACE_WITH_PRIVATE_CDEK_CLIENT_ID'   -e CDEK_CLIENT_SECRET='REPLACE_WITH_PRIVATE_CDEK_CLIENT_SECRET'   -e CDEK_SYNC_CONCURRENCY=4   -e CDEK_SYNC_STALE_HOURS=6   eyya-dashboard node scripts/sync-cdek.js
