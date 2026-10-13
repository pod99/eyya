#!/bin/bash
set -e
DEPLOY_TEMPLATE="n8n"
ARTIFACTS_DIR="/opt/${DEPLOY_TEMPLATE}/artifacts"
RETURN_FILE="$ARTIFACTS_DIR/return.txt"
if [ ! -f "$RETURN_FILE" ]; then
    echo "ERROR: Return file not found at $RETURN_FILE"
    exit 1
fi
if ! command -v curl >/dev/null; then
    apt-get update && apt-get install -y curl
fi
url=$(grep '^url=' "$RETURN_FILE" | cut -d'=' -f2)
domain=$(echo "$url" | sed -E 's/https?:\/\/([^/]+).*/\1/')
# Load Shared Library
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit

if [ -f "libs/utils.sh" ]; then
    source "libs/utils.sh"
elif [ -f "../../libs/utils.sh" ]; then
    source "../../libs/utils.sh"
else
    echo "ERROR: Shared library utils.sh not found."
    exit 1
fi

utils_ensure_connectivity
CURL_OPTS="-s -L --max-time 20"
if ! utils_is_real_domain "$domain"; then
    CURL_OPTS="$CURL_OPTS -k"
fi
# Ensure we test against localhost if running in the test environment
url_local=$(echo "$url" | sed -E 's|https?://[^:]+|http://127.0.0.1|')
echo "Verifying n8n URL: $url_local"
for i in {1..30}; do
    if curl $CURL_OPTS "$url_local" | grep -qi 'n8n'; then
        echo "n8n is running."
        N8N_OK=true
        break
    fi
    echo "Waiting for n8n... ($i/30)"
    sleep 2
done
if [ "$N8N_OK" != "true" ]; then
    echo "ERROR: n8n verification failed."
    exit 1
fi
echo "Verification successful."
