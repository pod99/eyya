#!/usr/bin/env bash
set -euo pipefail

# ===== CONFIG =====
APP_DIR="/opt/ff-bot"
APP_FILE="main.py"
PY_BIN="python3"
PIP_BIN="pip3"
LOG_FILE="$APP_DIR/bot.log"
PID_FILE="$APP_DIR/bot.pid"

# Укажи токен тут или передай снаружи: FF_TOKEN=... ./deploy_and_run.sh
: "${FF_TOKEN:=}"

if [[ -z "${FF_TOKEN}" ]]; then
  echo "ERROR: FF_TOKEN is empty. Run like:"
  echo "FF_TOKEN='your-token' bash deploy_and_run.sh"
  exit 1
fi

# ===== INSTALL PYTHON + PIP =====
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-pip
elif command -v yum >/dev/null 2>&1; then
  sudo yum install -y python3 python3-pip
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3 python3-pip
elif command -v brew >/dev/null 2>&1; then
  brew install python
else
  echo "WARNING: package manager not detected, assuming python already installed"
fi

# ===== PREPARE APP DIR =====
sudo mkdir -p "$APP_DIR"
sudo chown "$(id -u)":"$(id -g)" "$APP_DIR"

if [[ ! -f "$APP_DIR/$APP_FILE" ]]; then
  echo "ERROR: $APP_DIR/$APP_FILE not found. Copy your main.py first."
  exit 1
fi

# ===== INSTALL PYTHON DEPS =====
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install requests openpyxl

# ===== WATCHDOG LOOP =====
cat > "$APP_DIR/run_forever.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/ff-bot"
LOG_FILE="$APP_DIR/bot.log"
PID_FILE="$APP_DIR/bot.pid"
cd "$APP_DIR"

while true; do
  echo "[$(date '+%F %T')] starting bot..." | tee -a "$LOG_FILE"
  FF_TOKEN="$FF_TOKEN" python3 main.py >> "$LOG_FILE" 2>&1 || true
  echo "[$(date '+%F %T')] bot stopped, restarting in 3s..." | tee -a "$LOG_FILE"
  sleep 3
done
EOF

chmod +x "$APP_DIR/run_forever.sh"

# stop old watcher if exists
if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" || true
    sleep 1
  fi
fi

# start new watcher in background
nohup env FF_TOKEN="$FF_TOKEN" "$APP_DIR/run_forever.sh" >/dev/null 2>&1 &
echo $! > "$PID_FILE"

echo "Started. PID: $(cat "$PID_FILE")"
echo "Logs: tail -f $LOG_FILE"