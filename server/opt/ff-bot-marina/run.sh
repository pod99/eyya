#!/usr/bin/env bash
set -euo pipefail
cd /opt/ff-bot-marina
. /opt/ff-bot/.venv/bin/activate
set -a
. /opt/ff-bot-marina/.env
set +a
echo "[$(date -Is)] ff-bot-marina launcher starting" >> /opt/ff-bot-marina/bot.log
exec flock -w 60 /tmp/ff-bot-marina.lock python3 -u /opt/ff-bot-marina/main.py >> /opt/ff-bot-marina/bot.log 2>&1
