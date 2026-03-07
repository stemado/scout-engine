#!/usr/bin/env bash
# Provision a fresh Ubuntu 22.04 VPS for scout-engine.
# Run as root: bash setup-server.sh
set -euo pipefail

echo "=== scout-engine server setup ==="

# --- System packages ---
apt-get update
apt-get install -y \
    python3.11 python3.11-venv \
    postgresql postgresql-contrib \
    debian-keyring debian-archive-keyring apt-transport-https \
    curl gnupg git

# --- Google Chrome (deb, NOT snap) ---
# Ubuntu 22.04's chromium-browser installs a snap which conflicts with
# systemd sandboxing. Google Chrome's official deb repo avoids this.
# botasaurus-driver discovers it via shutil.which("google-chrome-stable").
if ! command -v google-chrome-stable &>/dev/null; then
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
        http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list
    apt-get update
    apt-get install -y google-chrome-stable
fi
echo "Chrome: $(google-chrome-stable --version)"

# --- Caddy (reverse proxy with auto-TLS) ---
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy

# --- uv (Python package manager) — install system-wide ---
# The installer only supports ~/.local/bin, so install there then copy.
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    cp /root/.local/bin/uv /usr/local/bin/uv
    cp /root/.local/bin/uvx /usr/local/bin/uvx
fi
echo "uv: $(uv --version)"

# --- Create scout user ---
if ! id -u scout &>/dev/null; then
    useradd --system --create-home --shell /bin/bash scout
fi

# --- PostgreSQL setup ---
# Ensure PostgreSQL is running
systemctl enable --now postgresql

sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='scout'" | grep -q 1 || \
    sudo -u postgres createuser scout
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='scout'" | grep -q 1 || \
    sudo -u postgres createdb -O scout scout

# Set a password for the scout DB user (hex avoids URL-unsafe chars like / + =)
SCOUT_DB_PASS=$(openssl rand -hex 24)
sudo -u postgres psql -c "ALTER USER scout WITH PASSWORD '${SCOUT_DB_PASS}';"

# FIX: Ensure pg_hba.conf allows password auth for TCP connections.
# Ubuntu's default may use 'peer' for local TCP, which rejects passwords.
PG_HBA=$(sudo -u postgres psql -tc "SHOW hba_file;" | xargs)
if ! grep -q "host.*scout.*127.0.0.1.*scram-sha-256" "$PG_HBA" 2>/dev/null; then
    # Insert a rule for the scout user BEFORE any existing host rules
    sed -i "/^# IPv4 local connections:/a host    scout    scout    127.0.0.1/32    scram-sha-256" "$PG_HBA"
    systemctl reload postgresql
    echo "Added scram-sha-256 rule to pg_hba.conf for scout user"
fi

# Verify the database connection works before continuing
if ! PGPASSWORD="${SCOUT_DB_PASS}" psql -h 127.0.0.1 -U scout -d scout -c "SELECT 1;" &>/dev/null; then
    echo "ERROR: Cannot connect to PostgreSQL as scout via TCP. Check pg_hba.conf."
    echo "  File: ${PG_HBA}"
    exit 1
fi
echo "PostgreSQL connection verified."

# --- Deploy code ---
if [ ! -d /opt/scout-engine ]; then
    echo ""
    echo "ERROR: /opt/scout-engine does not exist."
    echo "Clone the repo first:"
    echo "  git clone <repo-url> /opt/scout-engine"
    echo "  chown -R scout:scout /opt/scout-engine"
    echo "Then re-run this script."
    exit 1
fi

# Install dependencies as scout user
sudo -u scout bash -c 'cd /opt/scout-engine && uv sync'

# Create directories
sudo -u scout mkdir -p /opt/scout-engine/downloads /opt/scout-engine/screenshots

# --- Generate API key ---
SCOUT_API_KEY=$(openssl rand -base64 32)

# --- Write environment file ---
cat > /opt/scout-engine/.env <<ENVEOF
DATABASE_URL=postgresql+asyncpg://scout:${SCOUT_DB_PASS}@localhost/scout
HOST=127.0.0.1
PORT=8000
BOTASAURUS_HEADLESS=true
API_KEY=${SCOUT_API_KEY}
DOWNLOAD_DIR=/opt/scout-engine/downloads
SCREENSHOT_DIR=/opt/scout-engine/screenshots
ENVEOF
chown scout:scout /opt/scout-engine/.env
chmod 600 /opt/scout-engine/.env

# --- Save credentials to file (survives SSH disconnects) ---
CRED_FILE="/root/scout-credentials.txt"
cat > "$CRED_FILE" <<CREDEOF
# scout-engine credentials — generated $(date -Iseconds)
# This file is readable only by root.

DATABASE_URL=postgresql+asyncpg://scout:${SCOUT_DB_PASS}@localhost/scout
DATABASE_PASSWORD=${SCOUT_DB_PASS}
API_KEY=${SCOUT_API_KEY}

# These values are also written to /opt/scout-engine/.env
CREDEOF
chmod 600 "$CRED_FILE"

echo ""
echo "Credentials saved to ${CRED_FILE} (root-only, chmod 600)."
echo "They are also in /opt/scout-engine/.env for the service."

# --- Run migrations ---
sudo -u scout bash -c 'cd /opt/scout-engine && .venv/bin/alembic upgrade head'

# --- Firewall ---
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- Self-signed TLS cert for IP-only access ---
# Caddy's `tls internal` fails for bare :443 (no hostname to issue cert for).
# Generate a self-signed cert with the server's public IP in the SAN.
SERVER_IP=$(curl -s4 https://ifconfig.me)
mkdir -p /etc/caddy/certs
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout /etc/caddy/certs/key.pem \
    -out /etc/caddy/certs/cert.pem \
    -days 365 -nodes \
    -subj "/CN=${SERVER_IP}" \
    -addext "subjectAltName=IP:${SERVER_IP}" 2>/dev/null
chown caddy:caddy /etc/caddy/certs/*.pem

# --- Caddy configuration ---
# If deploy/Caddyfile has a real domain, use it; otherwise use self-signed cert.
cat > /etc/caddy/Caddyfile <<CADDYEOF
:443 {
    tls /etc/caddy/certs/cert.pem /etc/caddy/certs/key.pem
    reverse_proxy localhost:8000
}
CADDYEOF

# --- Install and start services ---
cp /opt/scout-engine/deploy/scout-engine.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now scout-engine
systemctl restart caddy

# --- Verify ---
sleep 2
if curl -sf http://127.0.0.1:8000/api/health > /dev/null; then
    echo ""
    echo "=== Setup complete ==="
    echo ""
    echo "Service is running and healthy."
    echo "External URL: https://${SERVER_IP}"
    echo "Credentials:  cat ${CRED_FILE}"
else
    echo ""
    echo "WARNING: Health check failed. Check logs:"
    echo "  journalctl -u scout-engine -n 50"
    exit 1
fi
