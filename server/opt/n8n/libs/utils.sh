#!/bin/bash

# ==============================================================================
# Cloud Template Utilities Library
# ==============================================================================

# --- Context Setup ---

# Detect if we are running in local development mode
# If GEMINI_LOCAL_DEV is set, potentially destructive or system-level commands
# (apt, ufw, systemctl) will be skipped or mocked.
IS_LOCAL="${GEMINI_LOCAL_DEV:-0}"

# Base directory for return data. Default to /tmp/
# Can be overridden by env var for local testing to avoid permission issues.
RETURN_DATA_DIR="${GEMINI_RETURN_DIR:-/tmp}"

# --- Logging & Output ---

log_info() {
    echo "[INFO] $1"
}

log_warn() {
    echo "[WARN] $1" >&2
}

log_error() {
    echo "[ERROR] $1" >&2
}

log_step() {
    echo "--------------------------------------------------------------------------------"
    echo ">>> STEP: $1"
    echo "--------------------------------------------------------------------------------"
}

# --- System Utilities ---

# Usage: utils_wait_for_apt
# Waits for other package managers to finish and forcefully clears locks if necessary
utils_wait_for_apt() {
    if [ "$IS_LOCAL" -eq 1 ]; then return 0; fi
    log_info "Ensuring APT is available and unlocked..."
    
    # Forcefully stop and disable potentially interfering services
    log_info "Stopping apt-daily and unattended-upgrades services..."
    systemctl stop apt-daily.service apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer 2>/dev/null || true
    systemctl disable apt-daily.service apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer 2>/dev/null || true
    systemctl stop unattended-upgrades.service 2>/dev/null || true
    systemctl disable unattended-upgrades.service 2>/dev/null || true

    local -i timeout=60 # 60 * 5s = 300s = 5 minutes
    local -i count=0
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
          || fuser /var/lib/dpkg/lock >/dev/null 2>&1 \
          || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
          || pgrep -x "apt" >/dev/null 2>&1 \
          || pgrep -x "apt-get" >/dev/null 2>&1 \
          || pgrep -x "dpkg" >/dev/null 2>&1; do
        
        count+=1
        if [ "$count" -ge "$timeout" ]; then
            log_warn "APT still locked after 5 minutes. Forcefully clearing locks..."
            
            # Kill processes holding the locks
            fuser -k /var/lib/dpkg/lock-frontend 2>/dev/null || true
            fuser -k /var/lib/dpkg/lock 2>/dev/null || true
            fuser -k /var/lib/apt/lists/lock 2>/dev/null || true
            pkill -9 apt 2>/dev/null || true
            pkill -9 apt-get 2>/dev/null || true
            pkill -9 dpkg 2>/dev/null || true
            
            # Remove lock files
            rm -f /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock
            
            # Repair dpkg database
            log_info "Repairing dpkg configuration..."
            dpkg --configure -a
            break
        fi
        log_info "Waiting for APT lock... ($count/$timeout)"
        sleep 5
    done
    # Small settle time
    sleep 2
    log_info "APT is ready."
}

# Usage: utils_check_connectivity
utils_check_connectivity() {
    if [ "$IS_LOCAL" -eq 1 ]; then return 0; fi
    
    log_info "Checking network configuration..."
    
    local max_retries=10
    local retry_count=0
    local ip_addr=""

    while [ $retry_count -lt $max_retries ]; do
        # Check if we have a global IPv4 address
        ip_addr=$(ip -4 addr show scope global | grep inet | awk '{print $2}' | head -n 1)
        if [ -n "$ip_addr" ]; then
            log_info "Guest IP found: $ip_addr"
            break
        fi
        
        log_warn "No global IPv4 address found yet. Interfaces state (attempt $((retry_count + 1))/$max_retries):"
        ip addr
        retry_count=$((retry_count + 1))
        sleep 5
    done

    if [ -z "$ip_addr" ]; then
        log_error "Failed to find a global IPv4 address after $max_retries attempts."
        return 1
    fi

    # Check for default gateway
    retry_count=0
    while [ $retry_count -lt $max_retries ]; do
        if ip route | grep -q "default via"; then
            log_info "Default gateway found."
            break
        fi
        log_warn "No default gateway found yet. Routing table (attempt $((retry_count + 1))/$max_retries):"
        ip route
        retry_count=$((retry_count + 1))
        sleep 5
    done

    if ! ip route | grep -q "default via"; then
        log_error "Failed to find a default gateway after $max_retries attempts."
        return 1
    fi

    log_info "Checking internet connectivity and DNS..."
    retry_count=0
    while [ $retry_count -lt $max_retries ]; do
        if curl -s --connect-timeout 5 http://archive.ubuntu.com/ubuntu/ >/dev/null 2>&1; then
            log_info "Network connectivity and DNS are OK."
            return 0
        fi
        log_warn "Connectivity check failed (attempt $((retry_count + 1))/$max_retries). Could not reach http://archive.ubuntu.com/ubuntu/"
        retry_count=$((retry_count + 1))
        sleep 5
    done

    log_error "Network diagnostics after failure:"
    echo "---[ /etc/resolv.conf ]---"
    cat /etc/resolv.conf || true
    echo "---[ Routing Table ]---"
    ip r || true
    echo "---[ Curl Details ]---"
    curl -vvv --connect-timeout 5 http://archive.ubuntu.com/ubuntu/ || true
    
    return 1
}

# Usage: utils_ensure_connectivity
# Checks for connectivity and exits with 1 if it fails.
utils_ensure_connectivity() {
    if ! utils_check_connectivity; then
        log_error "Network connectivity or DNS is not available. Exiting."
        exit 1
    fi
}

# Usage: utils_install_packages package1 package2 ...
utils_install_packages() {
    log_step "Installing required packages..."
    
    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Would install packages: $*"
        return 0
    fi

    # Check which packages are missing
    local missing_packages=()
    for pkg in "$@"; do
        if ! dpkg -s "$pkg" 2>/dev/null | grep -q "Status: install ok installed"; then
            missing_packages+=("$pkg")
        fi
    done

    if [ ${#missing_packages[@]} -eq 0 ]; then
        log_info "All required packages ($*) are already installed."
        return 0
    fi

    log_info "Missing packages: ${missing_packages[*]}"

    # Check connectivity before attempting to install
    if ! utils_check_connectivity; then
        log_error "Internet connectivity or DNS is not available. Cannot install missing packages."
        exit 1
    fi

    utils_wait_for_apt
    export DEBIAN_FRONTEND=noninteractive
    log_info "Updating package lists for missing packages..."
    apt-get -y update
    log_info "Installing apt-utils..."
    apt-get -y install apt-utils
    log_info "Starting installation of core missing packages: ${missing_packages[*]}..."
    # shellcheck disable=SC2068
    apt-get -y install ${missing_packages[@]} || { log_error "Failed to install required packages: ${missing_packages[*]}"; exit 1; }
    log_info "Finished installation of core missing packages: ${missing_packages[*]}"
}

utils_install_docker() {
    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Would install Docker and Docker Compose"
        return 0
    fi
    
    if command -v docker >/dev/null 2>&1; then
        log_info "Docker already installed."
        return 0
    fi

    log_step "Installing Docker (Manual Method)..."
    
    # Check connectivity before attempting to install
    if ! utils_check_connectivity; then
        log_error "Internet connectivity or DNS is not available. Cannot install Docker."
        exit 1
    fi

    log_info "Installing prerequisites..."
    utils_wait_for_apt
    apt-get update
    utils_wait_for_apt
    apt-get install -y ca-certificates curl gnupg || { log_error "Failed to install Docker prerequisites."; exit 1; }

    log_info "Setting up Docker GPG key..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes || { log_error "Failed to download Docker GPG key."; exit 1; }
    chmod a+r /etc/apt/keyrings/docker.gpg

    log_info "Adding Docker repository..."
    local codename
    codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
    echo \
      "deb [arch=\"$(dpkg --print-architecture)\" signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $codename stable" | \
      tee /etc/apt/sources.list.d/docker.list > /dev/null

    log_info "Installing Docker Engine..."
    utils_wait_for_apt
    apt-get update
    utils_wait_for_apt
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || { log_error "Failed to install Docker Engine."; exit 1; }

    log_info "Verifying Docker service..."
    systemctl enable --now docker
    docker --version
}

# Usage: utils_systemctl_enable_now service_name
utils_systemctl_enable_now() {
    local service="$1"
    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Would enable and start service: $service"
        return 0
    fi
    systemctl enable --now "$service"
}

utils_systemctl_restart() {
    local service="$1"
    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Would restart service: $service"
        return 0
    fi
    systemctl restart "$service"
}

# --- Network & Security ---

utils_get_public_ip() {
    if [ "$IS_LOCAL" -eq 1 ]; then
        echo "127.0.0.1"
    else
        hostname -I | awk '{print $1}'
    fi
}

utils_generate_password() {
    local length="${1:-16}"
    openssl rand -base64 12 | tr -dc 'A-Za-z0-9' | head -c "$length"
}

# Usage: utils_is_real_domain domain_name
# Returns 0 (true) if the domain name looks like a real public domain, 1 (false) otherwise.
utils_is_real_domain() {
    local domain="$1"
    if [ -z "$domain" ] || [[ "$domain" == "localhost" ]] || [[ ! "$domain" =~ \. ]]; then
        return 1
    fi
    # Check if it's an IP address
    if [[ "$domain" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        return 1
    fi
    return 0
}

# Usage: utils_setup_firewall port1 port2 ...
utils_setup_firewall() {
    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Would configure UFW for ports: $*"
        return 0
    fi
    
    # Always allow SSH to prevent lockouts
    ufw allow 22
    
    for port in "$@"; do
        ufw allow "$port"
    done
    ufw --force enable
}

# --- Nginx & SSL ---

utils_nginx_remove_default() {
    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Would remove default Nginx site"
        return 0
    fi
    log_info "Removing default Nginx site..."
    rm -f /etc/nginx/sites-enabled/default
    rm -f /etc/nginx/sites-available/default
    systemctl restart nginx
}

# Usage: utils_configure_nginx_proxy domain target_url [email] [app_name]
# target_url can be "http://127.0.0.1:8080" or just "8080" (assumes localhost)
utils_configure_nginx_proxy() {
    local domain="$1"
    local target="$2"
    local email="${3:-admin@example.com}"
    local app_name="${4:-app}" 
    
    # Normalize target
    if [[ "$target" =~ ^[0-9]+$ ]]; then
        target="http://127.0.0.1:$target"
    fi
    # Add http if missing
    if [[ ! "$target" =~ ^http ]]; then
        target="http://$target"
    fi

    local host_ip
    host_ip=$(utils_get_public_ip)
    local server_name="${domain:-$host_ip}"

    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Configuring Nginx Proxy:"
        log_info "  - App: $app_name"
        log_info "  - Server Name: $server_name"
        log_info "  - Target: $target"
        log_info "  - SSL: $([ -n "$domain" ] && echo "LetsEncrypt ($email)" || echo "Self-signed")"
        return 0
    fi

    # 1. Create HTTP Config (Port 80)
    cat <<NGINX >"/etc/nginx/sites-available/$app_name"
map \$http_upgrade \$connection_upgrade {
    default upgrade;
    '' close;
}

server {
    listen 80;
    server_name $server_name;
    client_max_body_size 100M;

    location / {
        proxy_pass $target;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600;
    }
}
NGINX

    ln -sf "/etc/nginx/sites-available/$app_name" "/etc/nginx/sites-enabled/$app_name"
    systemctl restart nginx

    # 2. SSL Setup
    if utils_is_real_domain "$domain"; then
        log_info "Attempting to obtain SSL certificate for $domain via Certbot..."
        if certbot --nginx --non-interactive --agree-tos -m "$email" -d "$domain"; then
            return 0
        fi
        log_warn "Certbot failed for $domain. Falling back to self-signed certificate."
    fi

    # Self-signed fallback
    mkdir -p /etc/ssl/private /etc/ssl/certs
        openssl req -new -x509 -days 365 -nodes \
            -subj "/CN=$server_name" \
            -out "/etc/ssl/certs/${app_name}-selfsigned.crt" \
            -keyout "/etc/ssl/private/${app_name}-selfsigned.key"
            
        cat <<SSL >"/etc/nginx/snippets/${app_name}-selfsigned.conf"
ssl_certificate /etc/ssl/certs/${app_name}-selfsigned.crt;
ssl_certificate_key /etc/ssl/private/${app_name}-selfsigned.key;
SSL

        cat <<NGINXSSL >"/etc/nginx/sites-available/${app_name}-ssl"
server {
    listen 443 ssl;
    server_name $server_name;
    include snippets/${app_name}-selfsigned.conf;
    client_max_body_size 100M;

    location / {
        proxy_pass $target;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600;
    }
}
NGINXSSL
        ln -sf "/etc/nginx/sites-available/${app_name}-ssl" "/etc/nginx/sites-enabled/${app_name}-ssl"
        systemctl restart nginx
}

# --- Docker & Compose ---

# Usage: utils_load_offline_images [directory]
# Looks for an 'images' directory and loads all .tar files.
utils_load_offline_images() {
    # Check if images have already been loaded in this session
    if [ "${_DOCKER_OFFLINE_IMAGES_LOADED:-0}" -eq 1 ]; then
        log_info "Offline images already loaded in this session. Skipping."
        return 0
    fi

    local img_dir="${1:-images}"
    if [ -d "$img_dir" ]; then
        log_info "Offline images found in $img_dir. Loading..."
        for img in "$img_dir"/*.tar; do
            [ -e "$img" ] || continue
            log_info "Loading image: $img"
            docker load -i "$img"
        done
        # Mark as loaded
        export _DOCKER_OFFLINE_IMAGES_LOADED=1
    else
        log_info "No offline images directory found at $img_dir."
    fi
}

utils_docker_compose_up() {
    # Try to load offline images first
    utils_load_offline_images

    local timeout="${COMPOSE_WAIT_TIMEOUT:-900}"
    local files_args=()
    if [ -n "${COMPOSE_FILES:-}" ]; then
        for f in $COMPOSE_FILES; do
            files_args+=("-f" "$f")
        done
    fi

    if [ "$IS_LOCAL" -eq 1 ]; then
        log_info "(Local) Starting Docker Compose..."
        docker compose "${files_args[@]}" up -d --wait "$@"
    else
        docker compose "${files_args[@]}" up -d --wait --wait-timeout "$timeout" "$@"
    fi
}

# Usage: utils_wait_for_healthy service_name [timeout_seconds]
# Waits for a docker service to report 'healthy' or 'running' (if no healthcheck).
utils_wait_for_healthy() {
    local service="$1"
    local timeout="${2:-300}"
    local start_time
    start_time=$(date +%s)
    
    log_info "Waiting for service '$service' to be healthy (timeout: ${timeout}s)..."
    
    while true; do
        local current_time
        current_time=$(date +%s)
        if (( current_time - start_time > timeout )); then
            log_error "Timeout waiting for service '$service' to be healthy."
            return 1
        fi
        
        local container_id
        container_id=$(docker compose ps -q "$service" 2>/dev/null || true)
        
        if [ -n "$container_id" ]; then
            local status
            status=$(docker inspect -f '{{.State.Status}}' "$container_id" 2>/dev/null || echo "unknown")
            local health
            health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || echo "none")
            
            if [ "$status" == "running" ]; then
                if [ "$health" == "healthy" ] || [ "$health" == "none" ]; then
                    log_info "Service '$service' is healthy."
                    return 0
                fi
            fi
        fi
        
        sleep 5
    done
}

# --- Return Data ---

# Usage: utils_init_return_dir template_name
utils_init_return_dir() {
    local name="$1"
    local dir
    
    if [ "$IS_LOCAL" -eq 1 ]; then
        dir="$RETURN_DATA_DIR/$name"
    else
        # Production: Use /opt/{name}/artifacts
        dir="/opt/$name/artifacts"
    fi
    
    mkdir -p "$dir"
    echo "$dir"
}

# Usage: utils_write_return_file dir_path content
utils_write_return_file() {
    local dir="$1"
    local content="$2"
    # shellcheck disable=SC2002
    echo "$content" > "$dir/return.txt"
}