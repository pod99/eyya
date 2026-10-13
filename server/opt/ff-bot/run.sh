#!/usr/bin/env bash
set -euo pipefail
cd /opt/ff-bot
. /opt/ff-bot/.venv/bin/activate
set -a
. /opt/ff-bot/.env
set +a
echo "[$(date '+%F %T')] ff-bot launcher starting" >> /opt/ff-bot/bot.log
exec flock -w 60 /tmp/ff-bot.lock python3 -u /opt/ff-bot/main.py >> /opt/ff-bot/bot.log 2>&1
