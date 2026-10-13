#!/bin/sh
set -eu
cd /opt/eyya-dashboard
flock -n /tmp/eyya-ff-sync.lock docker exec \
  -e FF_PLATFORM_TOKEN='REPLACE_WITH_PRIVATE_FF_PLATFORM_TOKEN' \
  -e FF_SYNC_CONCURRENCY=3 \
  -e FF_SYNC_STATUSES='sended,in_work,not_work' \
  eyya-dashboard node scripts/sync-ff-platform.js "$@"
