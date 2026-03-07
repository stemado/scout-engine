# Hetzner Deployment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy scout-engine to a Hetzner CX22 VPS with API key auth, TLS, and systemd management — turning the local-only POC into a remotely accessible service.

**Architecture:** FastAPI app runs as a systemd service on Ubuntu 22.04 behind Caddy (auto-TLS reverse proxy). PostgreSQL installed locally. All endpoints protected by Bearer token auth except `/api/health`. Firewall allows only SSH, HTTP, HTTPS.

**Tech Stack:** FastAPI, Caddy, PostgreSQL, systemd, UFW, uv

**Design doc:** `docs/plans/2026-03-06-hetzner-deployment-design.md`

### Adversarial Review Fixes Applied

This plan addresses five issues found during adversarial review:

| # | Issue | Fix |
|---|---|---|
| 1 | `apt install chromium-browser` installs a snap on Ubuntu 22.04 — snap sandbox conflicts with systemd | Install Google Chrome from Google's official apt repo (deb, not snap). botasaurus-driver discovers it via `shutil.which("google-chrome-stable")` |
| 2 | uv installed as root at `/root/.local/bin/` — inaccessible to the `scout` user | Install uv system-wide to `/usr/local/bin/uv` using `--install-dir` flag |
| 3 | PostgreSQL default `pg_hba.conf` uses `peer` auth — password auth over TCP may be rejected | Script configures `pg_hba.conf` to allow `scram-sha-256` for localhost TCP and verifies the connection before continuing |
| 4 | No TLS without a domain — API key sent in plaintext | Caddy self-signed TLS as default fallback; domain-based Let's Encrypt as upgrade path |
| 5 | Credentials echoed to stdout — lost if SSH disconnects | Credentials written to `/root/scout-credentials.txt` (chmod 600) in addition to `.env` |

---

## Task 1: Add API key auth setting to config

**Files:**
- Modify: `app/config.py`

**Step 1: Add `api_key` field to Settings**

```python
# In class Settings, add after webhook_url:
api_key: str = ""
```

**Step 2: Add to `.env.example`**

Append to `.env.example`:

```
# API key for remote access (required in production)
API_KEY=
```

**Step 3: Commit**

```bash
git add app/config.py .env.example
git commit -m "feat: add api_key setting for remote auth"
```

---

## Task 2: Add API key auth dependency

**Files:**
- Create: `app/auth.py`
- Test: `tests/test_auth.py`

**Step 1: Write the failing tests**

Create `tests/test_auth.py`:

```python
"""Tests for API key authentication."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_requires_no_auth(client):
    """Health endpoint should always be accessible."""
    resp = await client.get("/api/health")
    assert resp.status_code == 200


async def test_protected_endpoint_rejects_no_key(client, monkeypatch):
    """When API_KEY is set, requests without a key get 401."""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    resp = await client.get("/api/workflows")
    assert resp.status_code == 401
    assert "Missing" in resp.json()["detail"]


async def test_protected_endpoint_rejects_wrong_key(client, monkeypatch):
    """Wrong key gets 403."""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    resp = await client.get(
        "/api/workflows", headers={"Authorization": "Bearer wrong-key"}
    )
    assert resp.status_code == 403
    assert "Invalid" in resp.json()["detail"]


async def test_protected_endpoint_accepts_correct_key(client, monkeypatch):
    """Correct key passes through."""
    monkeypatch.setattr(settings, "api_key", "test-secret-key")
    resp = await client.get(
        "/api/workflows", headers={"Authorization": "Bearer test-secret-key"}
    )
    assert resp.status_code == 200


async def test_auth_disabled_when_no_key_configured(client, monkeypatch):
    """When API_KEY is empty, all requests pass (local dev mode)."""
    monkeypatch.setattr(settings, "api_key", "")
    resp = await client.get("/api/workflows")
    assert resp.status_code == 200
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL (auth module doesn't exist yet)

**Step 3: Write the auth middleware**

Create `app/auth.py`:

```python
"""API key authentication middleware."""

from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.config import settings

# Paths that never require authentication
PUBLIC_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token when API_KEY is set."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip auth when no key is configured (local dev)
        if not settings.api_key:
            return await call_next(request)

        # Skip auth for public paths
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization header",
            )

        # Expect "Bearer <key>"
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0] != "Bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Bearer token",
            )

        if parts[1] != settings.api_key:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid API key",
            )

        return await call_next(request)
```

**Step 4: Wire middleware into the app**

In `app/main.py`, add after the `app = FastAPI(...)` block:

```python
from app.auth import ApiKeyMiddleware

app.add_middleware(ApiKeyMiddleware)
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_auth.py -v`
Expected: All 5 pass

**Step 6: Run full test suite to check for regressions**

Run: `uv run pytest -v`
Expected: All existing tests still pass (API_KEY defaults to empty string = auth disabled)

**Step 7: Commit**

```bash
git add app/auth.py tests/test_auth.py app/main.py
git commit -m "feat: add API key auth middleware with public path bypass"
```

---

## Task 3: Create server provisioning script

**Files:**
- Create: `deploy/setup-server.sh`

**Step 1: Create the provisioning script**

Create `deploy/setup-server.sh`:

```bash
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
# FIX: Previous version installed to /root/.local/bin which the scout user
# cannot access. Installing to /usr/local/bin makes it available to all users.
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env INSTALLER_NO_MODIFY_PATH=1 sh -s -- --install-dir /usr/local/bin
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

# Set a password for the scout DB user
SCOUT_DB_PASS=$(openssl rand -base64 24)
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

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. cp deploy/scout-engine.service /etc/systemd/system/"
echo "  2. Edit deploy/Caddyfile — set your domain or IP"
echo "  3. cp deploy/Caddyfile /etc/caddy/Caddyfile"
echo "  4. systemctl daemon-reload"
echo "  5. systemctl enable --now scout-engine"
echo "  6. systemctl restart caddy"
echo ""
echo "Credentials: cat ${CRED_FILE}"
```

**Step 2: Commit**

```bash
git add deploy/setup-server.sh
git commit -m "feat: add server provisioning script for Hetzner VPS"
```

---

## Task 4: Create systemd unit file

**Files:**
- Create: `deploy/scout-engine.service`

**Step 1: Write the unit file**

Create `deploy/scout-engine.service`:

```ini
[Unit]
Description=scout-engine workflow execution service
After=network.target postgresql.service

[Service]
Type=exec
User=scout
Group=scout
WorkingDirectory=/opt/scout-engine
ExecStart=/opt/scout-engine/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# Load environment from .env file
EnvironmentFile=/opt/scout-engine/.env

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/opt/scout-engine/downloads /opt/scout-engine/screenshots
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

**Step 2: Commit**

```bash
git add deploy/scout-engine.service
git commit -m "feat: add systemd unit file for scout-engine"
```

---

## Task 5: Create Caddy configuration

**Files:**
- Create: `deploy/Caddyfile`
- Create: `deploy/Caddyfile.ip-only`

**Step 1: Write the domain-based Caddyfile**

Create `deploy/Caddyfile`:

```
# If you have a domain pointed at this server's IP, replace the hostname below.
# Caddy auto-provisions Let's Encrypt TLS certificates — zero config.
scout.yourdomain.com {
    reverse_proxy localhost:8000
}
```

**Step 2: Write the IP-only fallback Caddyfile**

Create `deploy/Caddyfile.ip-only`:

```
# Use this Caddyfile when you don't have a domain yet.
# Caddy generates a self-signed TLS certificate automatically.
# Your browser/curl will warn about the untrusted cert — that's expected.
# The connection is still encrypted, protecting the API key in transit.
#
# Usage:
#   cp deploy/Caddyfile.ip-only /etc/caddy/Caddyfile
#   systemctl restart caddy
#
# Connect with: curl -k https://YOUR_VPS_IP/api/health
# The -k flag tells curl to accept the self-signed cert.

:443 {
    tls internal
    reverse_proxy localhost:8000
}
```

**Step 3: Commit**

```bash
git add deploy/Caddyfile deploy/Caddyfile.ip-only
git commit -m "feat: add Caddyfile with self-signed TLS fallback for domainless POC"
```

---

## Task 6: Create deployment script

**Files:**
- Create: `deploy/deploy.sh`

**Step 1: Write the deployment script**

Create `deploy/deploy.sh`:

```bash
#!/usr/bin/env bash
# Deploy latest code to the server. Run from the server as root.
set -euo pipefail

cd /opt/scout-engine

echo "Pulling latest code..."
sudo -u scout git pull --ff-only

echo "Installing dependencies..."
sudo -u scout uv sync

echo "Running migrations..."
sudo -u scout .venv/bin/alembic upgrade head

echo "Restarting service..."
systemctl restart scout-engine

echo "Waiting for health check..."
sleep 2
if curl -sf http://127.0.0.1:8000/api/health > /dev/null; then
    echo "Deploy complete. Service is healthy."
else
    echo "WARNING: Health check failed. Check logs:"
    echo "  journalctl -u scout-engine -n 50"
    exit 1
fi
```

**Step 2: Commit**

```bash
git add deploy/deploy.sh
git commit -m "feat: add deployment script with health check"
```

---

## Task 7: Update .env.example with all production fields

**Files:**
- Modify: `.env.example`

**Step 1: Update the example env file**

Replace contents of `.env.example`:

```
# PostgreSQL connection
DATABASE_URL=postgresql+asyncpg://scout:scout@localhost:5432/scout_engine

# Server
HOST=0.0.0.0
PORT=8000

# Botasaurus
BOTASAURUS_HEADLESS=true

# Webhook notifications (optional)
WEBHOOK_URL=

# API key for remote access (leave empty for local dev, required in production)
API_KEY=

# Directories
DOWNLOAD_DIR=./downloads
SCREENSHOT_DIR=./screenshots
```

**Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: update .env.example with all production fields"
```

---

## Task 8: Verify full test suite passes

**Step 1: Run the complete test suite**

Run: `uv run pytest -v`
Expected: All tests pass, no regressions from auth middleware (API_KEY defaults to empty = auth disabled)

**Step 2: Verify auth tests specifically**

Run: `uv run pytest tests/test_auth.py -v`
Expected: All 5 auth tests pass

---

## Task 9: Manual deployment to Hetzner

This task is done on the VPS, not locally. It is a checklist, not automated code.

**Prerequisites:**
- Hetzner CX22 VPS created with Ubuntu 22.04
- SSH access configured (ideally with key-based auth, not password)

**Step 1: SSH into the server**

```bash
ssh root@YOUR_VPS_IP
```

**Step 2: Clone the repo**

```bash
git clone YOUR_REPO_URL /opt/scout-engine
chown -R scout:scout /opt/scout-engine
```

Note: if the repo is private, configure a deploy key or personal access token first.

**Step 3: Run the provisioning script**

```bash
cd /opt/scout-engine
bash deploy/setup-server.sh
```

The script saves credentials to `/root/scout-credentials.txt`. Read them:

```bash
cat /root/scout-credentials.txt
```

**Step 4: Install the systemd unit**

```bash
cp deploy/scout-engine.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now scout-engine
```

**Step 5: Configure Caddy**

If you have a domain pointed at the VPS IP:
```bash
# Edit the domain in deploy/Caddyfile first
nano deploy/Caddyfile
cp deploy/Caddyfile /etc/caddy/Caddyfile
systemctl restart caddy
```

If you do NOT have a domain (POC with IP only):
```bash
cp deploy/Caddyfile.ip-only /etc/caddy/Caddyfile
systemctl restart caddy
```

**Step 6: Verify the deployment**

```bash
# Local health check (bypasses Caddy)
curl http://127.0.0.1:8000/api/health

# Through Caddy — if using self-signed TLS, add -k flag
# Replace YOUR_VPS_IP or scout.yourdomain.com as appropriate
curl -k https://YOUR_VPS_IP/api/health

# Auth check — should get 401 (no key)
curl -k https://YOUR_VPS_IP/api/workflows

# Auth check — should get 200 (with key from /root/scout-credentials.txt)
curl -k -H "Authorization: Bearer YOUR_API_KEY" https://YOUR_VPS_IP/api/workflows

# Check service logs
journalctl -u scout-engine -f
```

**Step 7: Test workflow upload remotely**

```bash
curl -k -X POST https://YOUR_VPS_IP/api/workflows \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "1.0",
    "name": "smoke-test",
    "description": "Verify remote deployment",
    "settings": {"headless": true, "on_error": "stop"},
    "steps": [
      {"order": 1, "name": "Open Example", "action": "navigate", "value": "https://example.com"}
    ]
  }'
```

Expected: 201 response with workflow ID.

**Step 8: Test workflow execution remotely**

```bash
# Use the workflow ID from step 7
curl -k -X POST https://YOUR_VPS_IP/api/workflows/WORKFLOW_ID/run \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Expected: 202 response with execution ID. Check execution status:

```bash
curl -k -H "Authorization: Bearer YOUR_API_KEY" \
  https://YOUR_VPS_IP/api/executions/EXECUTION_ID
```

Expected: `"status": "completed"` with step results.
