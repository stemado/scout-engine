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
