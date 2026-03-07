# Hetzner Deployment Design

**Date:** 2026-03-06
**Status:** Approved
**Goal:** Deploy scout-engine to a Hetzner VPS as a POC for cloud-hosted browser automation

## Context

scout-engine is a FastAPI service that executes browser automation workflows via
botasaurus-driver. It currently runs locally. The goal is to deploy it to a cheap
cloud server so workflows can be triggered remotely and run on dedicated
infrastructure rather than the user's machine.

### Decision Record

The following alternatives were evaluated before arriving at this design:

| Option | Why it was rejected |
|---|---|
| **Vercel (serverless)** | Cannot run Chromium — no persistent process, read-only FS, 300s timeout |
| **Vercel + local Docker runner** | Over-engineered for a POC; three components when one suffices |
| **Railway ($5/mo)** | Usage-based pricing; always-on service costs ~$30/mo, not $5 |
| **Render (free tier)** | Service sleeps after 15min inactivity; kills cron/scheduler |
| **Fly.io (free tier)** | 256MB RAM too tight for Chromium |
| **Docker on Hetzner** | Unnecessary layer; VPS is already isolated |

**Winner: Bare-metal deploy on Hetzner CX22** — fixed cost ($4.75/mo), 2 vCPU,
4GB RAM, unlimited usage, direct install without Docker overhead.

## Architecture

```
┌─ User's Machine ──────────────────────────┐
│                                           │
│  Claude Code + scout-engine plugin (MCP)   │
│  • /export, /sync, /run, /schedule        │
│  • MCP tools for workflow authoring       │
│                                           │
└──────────────────┬────────────────────────┘
                   │ HTTPS + API key auth
┌──────────────────▼────────────────────────┐
│  Hetzner CX22  ($4.75/mo)                 │
│  Ubuntu 22.04 LTS                         │
│                                           │
│  ┌─ scout-engine (systemd service) ─────┐  │
│  │  FastAPI server                     │  │
│  │  • Workflow CRUD endpoints          │  │
│  │  • Execution management             │  │
│  │  • APScheduler (cron)               │  │
│  │  • botasaurus-driver + Chromium     │  │
│  └─────────────────────────────────────┘  │
│                                           │
│  ┌─ PostgreSQL ────────────────────────┐  │
│  │  Workflows, executions, schedules   │  │
│  └─────────────────────────────────────┘  │
│                                           │
│  ┌─ Caddy (reverse proxy) ─────────────┐  │
│  │  Auto TLS via Let's Encrypt         │  │
│  │  Proxies HTTPS → localhost:8000     │  │
│  └─────────────────────────────────────┘  │
└───────────────────────────────────────────┘
```

## Components

### 1. Hetzner VPS (CX22)

- **OS:** Ubuntu 22.04 LTS
- **Specs:** 2 vCPU (Intel), 4GB RAM, 40GB SSD, 20TB bandwidth
- **Cost:** ~$4.75/mo fixed (no usage metering)
- **Capacity:** 4GB RAM comfortably runs one Chromium instance at a time;
  sequential workflow execution handles hundreds of runs per day

### 2. scout-engine (systemd service)

The existing FastAPI application, deployed directly (no Docker):

```bash
# Install runtime
apt install chromium-browser python3.11
curl -LsSf https://astral.sh/uv/install.sh | sh

# Deploy code
git clone <repo> /opt/scout-engine
cd /opt/scout-engine && uv sync

# Run as systemd service
systemctl enable --now scout-engine
```

Systemd unit file at `/etc/systemd/system/scout-engine.service`:

```ini
[Unit]
Description=scout-engine workflow execution service
After=network.target postgresql.service

[Service]
Type=exec
User=scout
WorkingDirectory=/opt/scout-engine
ExecStart=/opt/scout-engine/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
Environment=DATABASE_URL=postgresql+asyncpg://scout:PASSWORD@localhost/scout
Environment=DEBUG=false

[Install]
WantedBy=multi-user.target
```

### 3. PostgreSQL

Installed via apt, local-only access:

```bash
apt install postgresql
sudo -u postgres createuser scout
sudo -u postgres createdb -O scout scout
```

No external exposure. Connection via Unix socket or localhost.

### 4. Caddy (reverse proxy + TLS)

Caddy auto-provisions Let's Encrypt certificates with zero configuration:

```
# /etc/caddy/Caddyfile
scout.yourdomain.com {
    reverse_proxy localhost:8000
}
```

Requires a domain name pointed at the VPS IP. Alternatively, use the raw IP
with a self-signed cert for POC testing.

### 5. Authentication

API key authentication middleware added to FastAPI:

- Server reads `API_KEY` from environment
- All endpoints (except `/api/health`) require `Authorization: Bearer <key>`
- Plugin stores the key locally in its config
- Simple, sufficient for single-user POC

## Security Model

| Layer | Protection |
|---|---|
| **Hetzner VPS** | Isolated machine; not your personal computer |
| **Caddy + TLS** | Encrypted in transit; no plaintext API keys on the wire |
| **API key auth** | Only authorized clients can trigger workflows |
| **PostgreSQL local-only** | DB not exposed to the internet |
| **UFW firewall** | Only ports 22 (SSH), 80, 443 open |
| **Dedicated `scout` user** | Service runs as unprivileged user, not root |
| **Scout schema validation** | Workflows can only contain defined browser actions |

## What Already Exists vs. What Needs Building

### Already built (no changes needed):
- FastAPI app with all API endpoints
- Pydantic schema validation
- SQLAlchemy models + Alembic migrations
- Executor with botasaurus-driver
- APScheduler integration
- Execution tracking, pause/resume, cancellation
- CDP browser session exposure

### Needs building:
1. **API key auth middleware** — FastAPI dependency that checks Bearer token
2. **Systemd unit file** — `scout-engine.service`
3. **Caddy configuration** — `Caddyfile`
4. **Server setup script** — Automated provisioning (apt packages, user creation,
   uv install, DB setup, migrations, firewall)
5. **Plugin remote commands** — `/sync`, `/run`, `/schedule`, `/status` that talk
   to the remote API instead of local execution
6. **Deployment script** — `git pull && uv sync && systemctl restart scout-engine`

## Future Phases (documented, not built)

- **Phase 2: Docker sandboxing** — When untrusted users run workflows, execute
  each workflow in a disposable Docker container on the same VPS
- **Phase 3: Multi-runner** — Cloud runner (Hetzner) + local runner (user's
  machine) both polling a job queue, executing where capacity is available
- **Phase 4: Scale up** — Upgrade to CX32 (4 vCPU, 8GB RAM, ~$8/mo) for
  concurrent workflow execution
