#!/bin/bash
set -e

# Ensure working directory is this script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit

# Load Shared Library
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

log_step "Initializing n8n Deployment"

DOMAIN="$1"
HOST=$(utils_get_public_ip)
RETURN_DIR=$(utils_init_return_dir "n8n")

# --- Configuration ---
ENV_FILE="env/n8n.env"

# Create environment directory
mkdir -p env

# Generate passwords and create .env file
if [ ! -f "$ENV_FILE" ]; then
    N8N_USER="admin"
    N8N_PASSWORD=$(utils_generate_password 16)

    cat <<EOT > "$ENV_FILE"
N8N_BASIC_AUTH_ACTIVE=true
N8N_BASIC_AUTH_USER=$N8N_USER
N8N_BASIC_AUTH_PASSWORD=$N8N_PASSWORD
N8N_HOST=${DOMAIN:-$HOST}
N8N_PROTOCOL=https
EOT
fi

# Source the environment file to export variables
# shellcheck disable=SC1090
source "$ENV_FILE"

# Prepare volume for n8n data with correct permissions
mkdir -p volumes/n8n_data
chown -R 1000:1000 volumes/n8n_data

utils_install_docker
utils_install_packages ufw nginx certbot python3-certbot-nginx
utils_nginx_remove_default

# Configure networking and firewall
utils_setup_firewall 22 80 443
utils_configure_nginx_proxy "${DOMAIN:-$HOST}" "http://127.0.0.1:5678" "admin@${DOMAIN:-example.com}" "n8n"

# Start containers
utils_docker_compose_up

# Finalize and write return file
utils_write_return_file "$RETURN_DIR" "url=https://${DOMAIN:-$HOST}
login=$N8N_BASIC_AUTH_USER
password=$N8N_BASIC_AUTH_PASSWORD"

log_info "Deployment complete."
